"""DM-only historical reciprocal trace with independently enforced airtime guards.

Only the selected trace transmits. "paths" reads received-DM history in SQLite.
History is indexed by contact display name upstream, NOT authenticated pubkey.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.commands.trace_command import TraceCommand
from modules.trace_runner import run_trace
from modules.models import MeshMessage

HEX_NODE = re.compile(r"^[0-9a-fA-F]{2}(?:[0-9a-fA-F]{2})?$")
PUBKEY = re.compile(r"^[0-9a-fA-F]{64}$")
HISTORY_PATH = re.compile(
    r"^([0-9a-fA-F]{2,6}(?:\s*,\s*[0-9a-fA-F]{2,6})*)"
    r"(?:\s*\(|\s*$)"
)


def parse_route(value, max_hops=5):
    """Accept only uniformly encoded 1-/2-byte inbound paths (not 3-byte).

    Reject rather than shorten routes that exceed the configured hop limit:
    sending a partial route would test a different destination.
    """
    match = HISTORY_PATH.match(str(value or "").strip())
    if not match:
        return None
    nodes = [part.strip().lower() for part in match.group(1).split(",")]
    if not nodes or len(nodes) > max_hops or not all(HEX_NODE.fullmatch(p) for p in nodes):
        return None
    if len({len(n) for n in nodes}) != 1:
        return None
    return tuple(nodes)


def human_wait(seconds):
    seconds = max(1, int(seconds + 0.999))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class AutotraceCommand(BaseCommand):
    name = "autotrace"
    keywords = ["autotrace"]
    description = "Trace a recently observed DM repeater path (DM only)"
    short_description = "Historical reciprocal trace with airtime cooldowns"
    usage = "autotrace [paths|1|2|3|NODE [paths|1|2|3]]"
    examples = ["autotrace paths", "autotrace", "autotrace 2", "autotrace Dee", "autotrace a1b2c3"]
    category = "meshcore_info"
    requires_dm = True
    # The standard cooldown would block the read-only 'paths' subcommand.
    # We enforce both user/global trace cooldowns explicitly before sending RF.
    cooldown_seconds = 0
    enabled_attr = "enabled"
    settings_schema = [
        {"key": "enabled", "label": "Enable AutoTrace", "type": "bool", "default": False},
        {"key": "per_user_cooldown_seconds", "label": "Per-user trace cooldown",
         "type": "int", "min": 0, "default": 900},
        {"key": "global_cooldown_seconds", "label": "Global trace cooldown",
         "type": "int", "min": 0, "default": 60},
        {"key": "maximum_hops", "label": "Maximum outbound hops",
         "type": "int", "min": 1, "max": 5, "default": 5},
        {"key": "max_route_age_hours", "label": "Historical route age",
         "type": "int", "min": 1, "max": 168, "default": 24},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value("Autotrace_Command", "enabled", fallback=False, value_type="bool")
        self.per_user_seconds = max(0, self._int("per_user_cooldown_seconds", 900))
        self.global_seconds = max(0, self._int("global_cooldown_seconds", 60))
        self.route_age_hours = max(1, min(168, self._int("max_route_age_hours", 24)))
        self.max_hops = max(1, min(5, self._int("maximum_hops", 5)))
        self._last_by_user = {}
        self._last_global = float("-inf")
        self._in_flight = False

    def _int(self, key, default):
        try:
            return int(self.get_config_value(
                "Autotrace_Command", key, fallback=default, value_type="int"
            ))
        except (ValueError, TypeError):
            return default

    def can_execute(self, message, skip_channel_check=False):
        return self.enabled and message.is_dm and super().can_execute(
            message, skip_channel_check=skip_channel_check
        )

    def get_help_text(self):
        return ("DM: autotrace [paths|1|2|3|NODE [paths|1|2|3]]. "
                "Other-node queries require admin. One RF attempt, cooldown.")

    def _identity(self, message):
        # A contact label is NOT a secure per-user rate-limit identity.
        key = str(getattr(message, "sender_pubkey", "") or "").strip().lower()
        return key if PUBKEY.fullmatch(key) else None

    def _history_label(self, message):
        label = str(message.sender_id or "").strip()
        if not label or label.casefold() == "unknown":
            return None
        try:
            anonymized = self.bot.config.getboolean(
                "Stats_Command", "anonymize_users", fallback=False
            )
        except (AttributeError, ValueError):
            anonymized = False
        return ("user_" + hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
                if anonymized else label)

    def _routes(self, message):
        label = self._history_label(message)
        if not label:
            return []
        manager = getattr(self.bot, "db_manager", None)
        if manager is None:
            return []
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=self.route_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        until = now.strftime("%Y-%m-%d %H:%M:%S")
        # Use receive timestamp created_at, NOT sender-controlled message timestamp.
        # Restrict to DM rows and the asking contact label; no arbitrary target.
        with manager.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_stats'"
            ).fetchone():
                return []
            rows = conn.execute(
                """SELECT path, created_at FROM message_stats
                   WHERE is_dm = 1 AND sender_id = ?
                     AND datetime(created_at) >= datetime(?)
                     AND datetime(created_at) <= datetime(?)
                   ORDER BY datetime(created_at) DESC, id DESC LIMIT 500""",
                (label, cutoff, until),
            ).fetchall()
        distinct = []
        seen = set()
        for raw, created in rows:
            nodes = parse_route(raw, max_hops=self.max_hops)
            if nodes is None or nodes in seen:
                continue
            seen.add(nodes)
            distinct.append((nodes, str(created)))
            if len(distinct) == 3:
                break
        return distinct

    def _resolve_target(self, query):
        """Lookup exactly one heard contact by name or 3-byte public key prefix.

        Never use fuzzy identity mapping when multiple full public keys match.
        """
        term = str(query or "").strip()
        if not term:
            return None, "AutoTrace: target name/prefix required"
        with self.bot.db_manager.connection() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='complete_contact_tracking'"
            ).fetchone() is None:
                return None, "AutoTrace: no contact database"
            rows = conn.execute(
                "SELECT name, public_key, role FROM complete_contact_tracking "
                "WHERE public_key IS NOT NULL"
            ).fetchall()
        contacts = [
            (str(name or ""), str(key).lower().strip(), str(role or "").lower())
            for name, key, role in rows if PUBKEY.fullmatch(str(key or "").strip())
        ]
        # Six hex digits represent 3 bytes of *identity*, not trace hash width.
        if re.fullmatch(r"[0-9a-fA-F]{6}", term):
            matched = [row for row in contacts if row[1].startswith(term.lower())]
        else:
            matched = [row for row in contacts if row[0].casefold() == term.casefold()]
            if not matched:
                matched = [row for row in contacts if term.casefold() in row[0].casefold()]
        distinct = {row[1]: row for row in matched}
        if not distinct:
            return None, "AutoTrace: target not found in heard contacts"
        if len(distinct) != 1:
            return None, "AutoTrace: ambiguous target; use 3-byte hex"
        return next(iter(distinct.values())), None

    def _target_routes(self, target):
        """Derive routes from actual recorded DM paths, never from position alone.

        Repeater: find a matching repeater hop and truncate the path at it,
        so the bot can trace back to *that* repeater. Reject hash collisions.
        Companion: use a received DM route under the contact's saved label.
        """
        name, key, role = target
        is_repeater = role in ("repeater", "roomserver")
        with self.bot.db_manager.connection() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(
                hours=self.route_age_hours
            )).strftime("%Y-%m-%d %H:%M:%S")
            until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if is_repeater:
                rows = conn.execute(
                    """SELECT path, created_at FROM message_stats
                       WHERE datetime(created_at) BETWEEN datetime(?) AND datetime(?)
                       ORDER BY datetime(created_at) DESC, id DESC LIMIT 2000""",
                    (cutoff, until),
                ).fetchall()
                others = [str(row[0] or "").lower() for row in conn.execute(
                    "SELECT public_key FROM complete_contact_tracking "
                    "WHERE role IN ('repeater', 'roomserver')"
                ).fetchall()]
            else:
                # Sender label in StatsCommand is not cryptographically bound
                # to key. The name must uniquely identify the heard contact.
                other_same_name = conn.execute(
                    "SELECT count(DISTINCT lower(public_key)) FROM complete_contact_tracking "
                    "WHERE lower(name)=lower(?)", (name,)
                ).fetchone()[0]
                if other_same_name != 1:
                    return []
                anonymized = self.bot.config.getboolean(
                    "Stats_Command", "anonymize_users", fallback=False
                )
                label = ("user_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
                         if anonymized else name)
                rows = conn.execute(
                    """SELECT path, created_at FROM message_stats
                       WHERE is_dm=1 AND sender_id=?
                         AND datetime(created_at) BETWEEN datetime(?) AND datetime(?)
                       ORDER BY datetime(created_at) DESC, id DESC LIMIT 500""",
                    (label, cutoff, until),
                ).fetchall()
                others = []
        routes, seen = [], set()
        for raw, created in rows:
            full_path = parse_route(raw, max_hops=self.max_hops)
            if full_path is None:
                continue
            if is_repeater:
                width = len(full_path[0])
                short = key[:width]
                # A 1-/2-byte route hash is not enough to disambiguate
                # unless no other heard repeater shares that prefix.
                if sum(1 for other in set(others) if other.startswith(short)) != 1:
                    continue
                indices = [i for i, node in enumerate(full_path) if node == short]
                if len(indices) != 1:
                    continue
                path = full_path[indices[0]:]
            else:
                path = full_path
            if path in seen:
                continue
            seen.add(path)
            routes.append((path, str(created)))
            if len(routes) >= 3:
                break
        return routes

    async def _reply(self, message, lines):
        limit = min(158, self.get_max_message_length(message))
        body = ""
        for line in lines:
            proposed = f"{body}\n{line}" if body else str(line)
            if len(proposed.encode("utf-8")) > limit:
                break
            body = proposed
        return await self.send_response(message, body or "AutoTrace: no report")

    def _eligible(self, key, admin, now):
        if self._in_flight:
            return "AutoTrace: trace already in progress"
        global_left = self.global_seconds - (now - self._last_global)
        if global_left > 0:
            return "AutoTrace: global cooldown " + human_wait(global_left)
        user_left = self.per_user_seconds - (now - self._last_by_user.get(key, float("-inf")))
        if user_left > 0 and not admin:
            return "AutoTrace: cooldown " + human_wait(user_left)
        return None

    async def execute(self, message: MeshMessage) -> bool:
        if not self.enabled or not message.is_dm:
            return True
        _trigger, args = self.split_trigger_and_args(message.content or "")
        args = args.strip()
        tokens = args.split()
        target_query = None
        action = ""
        if len(tokens) == 1:
            if tokens[0].lower() in ("paths", "1", "2", "3"):
                action = tokens[0].lower()
            else:
                target_query = tokens[0]
        elif len(tokens) >= 2:
            if tokens[-1].lower() in ("paths", "1", "2", "3"):
                action = tokens[-1].lower()
                target_query = " ".join(tokens[:-1])
            else:
                target_query = args
        if target_query and (len(target_query) > 70 or not target_query.strip()):
            return await self._reply(message, [self.get_help_text()])
        if target_query and not self._check_admin_access(message):
            return await self._reply(message, ["AutoTrace: target lookup is admin-only"])
        try:
            if target_query:
                target, error = self._resolve_target(target_query)
                if error:
                    return await self._reply(message, [error])
                routes = self._target_routes(target)
            else:
                routes = self._routes(message)
        except (sqlite3.Error, OSError, AttributeError) as exc:
            self.logger.warning("autotrace history lookup failed: %s", exc)
            return await self._reply(message, ["AutoTrace: history DB unavailable"])
        if action == "paths":
            if not routes:
                return await self._reply(message, ["AutoTrace: no valid recent DM routes"])
            heading = ("AutoTrace " + target[0][:16] + ": " if target_query else "AutoTrace: ")
            lines = [f"{heading}{len(routes)} recent route(s)"]
            for index, (nodes, created) in enumerate(routes, 1):
                lines.append(f"{index}. {','.join(nodes)}")
            lines.append("Observed paths only; not proof of identity.")
            return await self._reply(message, lines)
        if not routes:
            return await self._reply(message, [f"AutoTrace: no usable observed route in {self.route_age_hours}h"])
        index = int(action) - 1 if action else 0
        if index >= len(routes):
            hint = ("!autotrace " + target_query + " paths") if target_query else "!autotrace paths"
            return await self._reply(message, ["AutoTrace: route not available", "Try " + hint[:70]])
        key = self._identity(message)
        if key is None:
            return await self._reply(message, ["AutoTrace: sender public key required"])
        if not getattr(self.bot, "connected", False) or not getattr(self.bot, "meshcore", None):
            return await self._reply(message, ["AutoTrace: radio disconnected"])
        commands = getattr(self.bot.meshcore, "commands", None)
        if not callable(getattr(commands, "send_trace", None)):
            return await self._reply(message, ["AutoTrace: trace unavailable"])
        if not self.bot.config.getboolean("Trace_Command", "enabled", fallback=True):
            return await self._reply(message, ["AutoTrace: existing trace disabled"])
        nodes, created = routes[index]
        # Historical sender->bot path is reversed for bot->sender. Reuse existing
        # TraceCommand roundtrip helper; the trace returns to the bot's radio.
        trace = TraceCommand(self.bot)
        if len(nodes) > trace.maximum_hops:
            return await self._reply(message, ["AutoTrace: exceeds trace hop limit"])
        outbound = list(reversed(nodes))
        reciprocal = trace._build_reciprocal_path(outbound)
        flags = {2: 0, 4: 1}[len(nodes[0])]
        now = time.monotonic()
        # Only admins with authenticated pubkeys in the configured ACL may
        # bypass the individual cooldown, never the global or in-flight guard.
        admin = self._check_admin_access(message) if (
            now - self._last_by_user.get(key, float("-inf")) < self.per_user_seconds
        ) else False
        deny = self._eligible(key, admin, now)
        if deny:
            return await self._reply(message, [deny])
        # Reserve BEFORE first await/radio operation. Failure and timeouts count.
        self._last_by_user[key] = now
        self._last_global = now
        self._in_flight = True
        try:
            result = await run_trace(
                self.bot, path=reciprocal, flags=flags,
                timeout_seconds=None, max_attempts_override=1,
            )
            self.logger.info(
                "autotrace requester=%s path=%s success=%s",
                key[:12], ",".join(nodes), result.success,
            )
            if result.success:
                snr = [n.get("snr") for n in result.path_nodes if isinstance(n, dict)]
                snr = [x for x in snr if isinstance(x, (float, int))]
                lines = [f"AutoTrace #{index + 1}: reply received",
                         f"Path: {','.join(nodes)}"]
                if snr:
                    lines.append("Last SNR: %+.1fdB" % snr[-1])
                lines.append("Repeater path only; not end-node.")
            else:
                lines = [f"AutoTrace #{index + 1}: no return",
                         f"Path: {','.join(nodes)}",
                         "Cooldown active (even on failure)"]
            return await self._reply(message, lines)
        except Exception as exc:
            self.logger.exception("autotrace failed: %s", exc)
            return await self._reply(message, ["AutoTrace: trace error", "Cooldown active"])
        finally:
            self._in_flight = False
