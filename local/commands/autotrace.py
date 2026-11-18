"""Suggest *user-originated* MeshCore trace routes from passive DM observations.

NEVER sends a trace. The requesting user executes a suggested route on their own
companion. All observed routes flow sender -> repeaters -> this bot; joining
two routes at a shared repeater implies reverse-direction links and MUST be
reported as an unverified candidate, not proof of bidirectional RF connectivity.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

PUBKEY = re.compile(r"^[0-9a-fA-F]{64}$")
NODE = re.compile(r"^[0-9a-fA-F]{2}(?:[0-9a-fA-F]{2})?$")
PATH = re.compile(
    r"^([0-9a-fA-F]{2,6}(?:\s*,\s*[0-9a-fA-F]{2,6})*)"
    r"(?:\s*\(|\s*$)"
)


def parse_route(text, max_hops=5):
    """Parse a complete observed path. Reject mixed widths, >2-byte or truncation."""
    m = PATH.match(str(text or "").strip())
    if not m:
        return None
    nodes = tuple(x.strip().lower() for x in m.group(1).split(","))
    if (not nodes or len(nodes) > max_hops
            or len({len(x) for x in nodes}) != 1
            or not all(NODE.fullmatch(x) for x in nodes)):
        return None
    return nodes


def candidate_route(source, target, max_hops=5):
    """Join two sender->bot histories at their first common repeater toward bot.

    Every outbound direction after the junction is a reversed *observation*,
    not a validated bidirectional link. Require same on-air hash width.
    """
    if not source or not target or len(source[0]) != len(target[0]):
        return None
    shared = 0
    for left, right in zip(reversed(source), reversed(target)):
        if left != right:
            break
        shared += 1
    if not shared:
        return None
    path = source[:len(source) - shared + 1] + tuple(
        reversed(target[:len(target) - shared])
    )
    if len(path) > max_hops or len(set(path)) != len(path):
        return None
    return path


def wait_label(seconds):
    seconds = max(1, int(seconds + .999))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class AutotraceCommand(BaseCommand):
    name = "autotrace"
    keywords = ["autotrace"]
    description = "Suggest user-to-target trace routes from observed mesh paths"
    short_description = "User-originated route suggestions; no bot trace probes"
    usage = "autotrace [paths|NODE [paths|1|2|3]]"
    examples = ["autotrace Dee", "autotrace a1b2c3", "autotrace Dee paths"]
    category = "meshcore_info"
    requires_dm = True
    # Only the command reply uses RF. The trace itself must be run by the user.
    cooldown_seconds = 0
    enabled_attr = "enabled"
    settings_schema = [
        {"key": "enabled", "label": "Enable AutoTrace", "type": "bool", "default": False},
        {"key": "per_user_cooldown_seconds", "label": "User reply cooldown",
         "type": "int", "min": 0, "default": 900},
        {"key": "global_cooldown_seconds", "label": "Global reply cooldown",
         "type": "int", "min": 0, "default": 60},
        {"key": "maximum_hops", "label": "Maximum candidate hops",
         "type": "int", "min": 1, "max": 5, "default": 5},
        {"key": "max_route_age_hours", "label": "Observation age",
         "type": "int", "min": 1, "max": 168, "default": 24},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Autotrace_Command", "enabled", fallback=False, value_type="bool"
        )
        self.per_user_seconds = max(0, self._int("per_user_cooldown_seconds", 900))
        self.global_seconds = max(0, self._int("global_cooldown_seconds", 60))
        self.max_hops = max(1, min(5, self._int("maximum_hops", 5)))
        self.route_age_hours = max(1, min(168, self._int("max_route_age_hours", 24)))
        self._last_user = {}
        self._last_global = float("-inf")

    def _int(self, key, default):
        try:
            return int(self.get_config_value(
                "Autotrace_Command", key, fallback=default, value_type="int"
            ))
        except (TypeError, ValueError):
            return default

    def can_execute(self, message, skip_channel_check=False):
        return self.enabled and message.is_dm and super().can_execute(
            message, skip_channel_check=skip_channel_check
        )

    def get_help_text(self):
        return ("DM: autotrace NODE [paths|1|2|3]. "
                "Candidate route FROM your radio; execute trace yourself. No bot probe.")

    def _label(self, value):
        label = str(value or "").strip()
        if not label or label.casefold() == "unknown":
            return None
        if self.bot.config.getboolean("Stats_Command", "anonymize_users", fallback=False):
            return "user_" + hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
        return label

    def _identity(self, message):
        key = str(getattr(message, "sender_pubkey", "") or "").lower().strip()
        return key if PUBKEY.fullmatch(key) else None

    def _contacts(self, conn):
        return [
            (str(name or ""), str(key or "").strip().lower(), str(role or "").lower())
            for name, key, role in conn.execute(
                "SELECT name, public_key, role FROM complete_contact_tracking"
            ).fetchall()
            if PUBKEY.fullmatch(str(key or "").strip())
        ]

    def _resolve_target(self, query, contacts):
        term = str(query or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{6}", term):
            matches = [c for c in contacts if c[1].startswith(term.lower())]
        else:
            matches = [c for c in contacts if c[0].casefold() == term.casefold()]
            if not matches:
                matches = [c for c in contacts if term.casefold() in c[0].casefold()]
        identities = {c[1]: c for c in matches}
        if not identities:
            return None, "AutoTrace: target not found"
        if len(identities) != 1:
            return None, "AutoTrace: ambiguous; use 3-byte ID"
        return next(iter(identities.values())), None

    def _history(self, conn, label=None, any_sender=False, limit=1000):
        cutoff = (datetime.now(timezone.utc) - timedelta(
            hours=self.route_age_hours
        )).strftime("%Y-%m-%d %H:%M:%S")
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        where = "" if any_sender else "AND is_dm=1 AND sender_id=?"
        params = (cutoff, until) if any_sender else (label, cutoff, until)
        rows = conn.execute(
            "SELECT path,created_at FROM message_stats WHERE 1=1 " + where +
            " AND datetime(created_at) BETWEEN datetime(?) AND datetime(?)"
            " ORDER BY datetime(created_at) DESC,id DESC LIMIT " + str(limit),
            params,
        ).fetchall()
        results, seen = [], set()
        for raw, stamp in rows:
            route = parse_route(raw, max_hops=self.max_hops)
            if route and route not in seen:
                results.append((route, str(stamp)))
                seen.add(route)
        return results

    def _target_histories(self, conn, target, contacts):
        name, full_key, role = target
        if role not in ("repeater", "roomserver"):
            # The upstream stats table ties paths to a *name*, not a pubkey.
            # Reject duplicate labels rather than claim target verification.
            if len({c[1] for c in contacts
                    if c[0].casefold() == name.casefold()}) != 1:
                return []
            return self._history(conn, self._label(name))
        known_reps = {c[1] for c in contacts if c[2] in ("repeater", "roomserver")}
        paths = self._history(conn, any_sender=True, limit=2000)
        results = []
        for route, stamp in paths:
            target_hash = full_key[:len(route[0])]
            # A 3-byte identity cannot disambiguate a collided on-air
            # 1-/2-byte path hash. Require exactly one known repeater match.
            if sum(k.startswith(target_hash) for k in known_reps) != 1:
                continue
            positions = [i for i, hop in enumerate(route) if hop == target_hash]
            if len(positions) == 1:
                results.append((route[positions[0]:], stamp))
        return results

    def _proposals(self, message, target=None):
        with self.bot.db_manager.connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if not {"message_stats", "complete_contact_tracking"} <= tables:
                return [], "AutoTrace: contact/history DB unavailable"
            contacts = self._contacts(conn)
            own_label = str(message.sender_id or "").strip()
            source_key = self._identity(message)
            # Protect against same-name collisions in old DM history. Any
            # sender may still request a recommendation for their own current
            # observed path when history is ambiguous.
            historical_ok = bool(source_key and
                {c[1] for c in contacts if c[0].casefold() == own_label.casefold()}
                == {source_key})
            sources = []
            current = parse_route(getattr(message, "path", None), self.max_hops)
            if current:
                sources.append((current, "current"))
            if historical_ok:
                sources.extend(self._history(conn, self._label(own_label))[:20])
            if target is None:
                return [(nodes, "observed to bot") for nodes, _ in sources[:3]], None
            tgt, error = self._resolve_target(target, contacts)
            if error:
                return [], error
            target_histories = self._target_histories(conn, tgt, contacts)
            if not target_histories:
                return [], "AutoTrace: no observed target route"
            proposals = []
            seen = set()
            for own, _ in sources[:20]:
                for toward, _ in target_histories[:100]:
                    linked = candidate_route(own, toward, self.max_hops)
                    if linked and linked not in seen:
                        proposals.append((linked, tgt[0]))
                        seen.add(linked)
                        if len(proposals) >= 3:
                            return proposals, None
            return proposals, ("AutoTrace: no shared route evidence"
                               if not proposals else None)

    async def _reply(self, message, lines):
        limit = min(158, self.get_max_message_length(message))
        body = ""
        for line in lines:
            candidate = body + ("\n" if body else "") + line
            if len(candidate.encode("utf-8")) > limit:
                break
            body = candidate
        return await self.send_response(message, body or "AutoTrace: no result")

    async def execute(self, message: MeshMessage) -> bool:
        if not self.enabled or not message.is_dm:
            return True
        _trigger, raw = self.split_trigger_and_args(message.content or "")
        tokens = raw.strip().split()
        action = ""
        if tokens and tokens[-1].lower() in ("paths", "1", "2", "3"):
            action = tokens.pop().lower()
        query = " ".join(tokens).strip() or None
        if query is not None and len(query) > 70:
            return await self._reply(message, [self.get_help_text()])
        if action in ("1", "2", "3") and not query:
            return await self._reply(message, [self.get_help_text()])
        try:
            proposals, error = self._proposals(message, target=query)
        except (sqlite3.Error, OSError, AttributeError, ValueError) as exc:
            self.logger.warning("autotrace route lookup error: %s", exc)
            return await self._reply(message, ["AutoTrace: history unavailable"])
        if error:
            return await self._reply(message, [error])
        if not proposals:
            return await self._reply(message, ["AutoTrace: no route evidence"])
        if query is None:
            lines = ["Your observed route(s) to bot:"]
            lines.extend(f"{i}. {','.join(p)}" for i, (p, _) in
                         enumerate(proposals, 1))
            return await self._reply(message, lines)
        if action == "paths":
            lines = ["Candidate routes (unverified):"]
            lines.extend(f"{i}. {','.join(p)}" for i, (p, _) in
                         enumerate(proposals, 1))
            return await self._reply(message, lines)
        index = int(action) - 1 if action else 0
        if index >= len(proposals):
            return await self._reply(message, ["AutoTrace: selected route unavailable"])
        key = self._identity(message)
        if key is None:
            return await self._reply(message, ["AutoTrace: sender key required"])
        now = time.monotonic()
        global_wait = self.global_seconds - (now - self._last_global)
        user_wait = self.per_user_seconds - (
            now - self._last_user.get(key, float("-inf"))
        )
        if global_wait > 0:
            return await self._reply(message, ["AutoTrace: cooldown " + wait_label(global_wait)])
        if user_wait > 0:
            return await self._reply(message, ["AutoTrace: cooldown " + wait_label(user_wait)])
        self._last_global = now
        self._last_user[key] = now
        route, name = proposals[index]
        # Manual user-side execution. We deliberately DO NOT invoke run_trace
        # or radio.commands.send_trace anywhere in this plugin.
        return await self._reply(message, [
            f"To {name[:18]}: candidate only",
            "Your radio: tracer " + ",".join(route),
            "Reverse links untested; no bot probe."
        ])
