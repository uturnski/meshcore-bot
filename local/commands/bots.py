"""Passive bot-*candidate* directory from local MeshCore contacts, never RF scans.

!bots / !bots list [page]    -> compact 4-hex-ID + straight-line distance list
!botsN / !bots info N        -> local details for entry N (N is a list index)

Names alone are guesses; correlated public ping replies are stronger but unverified.
"""
from collections import Counter
from datetime import datetime, timezone
import math
import re
import sqlite3
import time
import unicodedata

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


KEY = re.compile(r"[0-9a-fA-F]{64}")
# Explicit bot token or CamelCase Bot marker; avoid Abbot, Robot, Botany.
BOT_NAME = re.compile(
    r"(?i:(?:^|[\s_-])bot(?=$|[\s_-]))|[a-z0-9]Bot(?=$|[\s_-])|^Bot(?=[A-Z])"
)
PING_REQUEST = re.compile(r"^\s*(?:@\[[^\]\r\n]{1,64}\]\s*)?!?ping(?:\s|$)", re.I)
PONG_REPLY = re.compile(r"^\s*(?:@\[(?P<target>[^\]\r\n]{1,64})\]\s*)?pong(?:!|\b)", re.I)
NAMED_PING_REPLY = re.compile(
    r"^\s*@\[(?P<target>[^\]\r\n]{1,64})\]\s+\d{1,3}\s+hops?,\s*SNR\b", re.I
)
ANON_SENDER = re.compile(r"^user_[0-9a-f]{8}$", re.I)
UNATTRIBUTABLE = {"", "unknown", "channel user"}
NUMBERED = re.compile(r"bots([1-9][0-9]{0,5})\Z", re.IGNORECASE)
POSITIVE = re.compile(r"[1-9][0-9]{0,5}\Z")
TABLES = (("complete_contact_tracking", "last_heard"),
          ("repeater_contacts", "last_seen"))


def label(value, budget=32):
    """One-line, UTF-8-safe abbreviation that does not split emoji."""
    value = str(value or "unnamed")
    clean = " ".join("".join(
        c if not unicodedata.category(c).startswith("C") else " "
        for c in value
    ).split())
    if budget < 1:
        return ""
    if len(clean.encode("utf-8")) <= budget:
        return clean
    if budget <= 3:
        return clean.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    short = clean.encode("utf-8")[:budget - 3].decode("utf-8", errors="ignore")
    return short.rstrip() + "..."


def age(value):
    """Return conservative age for an ISO/SQLite UTC last-seen timestamp."""
    if value in (None, ""):
        return "n/a"
    try:
        seen = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - seen).total_seconds()
        if not math.isfinite(elapsed) or elapsed < 0:
            return "n/a"
        if elapsed < 3600:
            return f"{int(elapsed // 60)}m"
        if elapsed < 86400:
            return f"{int(elapsed // 3600)}h"
        return f"{int(elapsed // 86400)}d"
    except (TypeError, ValueError, OverflowError):
        return "n/a"


def snr_display(value):
    try:
        val = float(value)
        return f"{val:+g}dB" if math.isfinite(val) else "n/a"
    except (ValueError, TypeError):
        return "n/a"


class BotsCommand(BaseCommand):
    name = "bots"
    keywords = ["bots"]
    description = "List passive bot responders with cached straight-line distances"
    short_description = description
    usage = "bots [list] [page] | botsN | bots info N"
    examples = ["bots", "bots list", "bots page 2", "bots1"]
    category = "analytics"
    requires_dm = False
    cooldown_seconds = 3

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Bots_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message, skip_channel_check=False):
        return self.enabled and super().can_execute(
            message, skip_channel_check=skip_channel_check
        )

    def matches_custom_syntax(self, message):
        """Allow !bots1, !bots2, etc., despite the built-in whole-word matcher."""
        return self._cleaned_content_matches(
            message, lambda text: NUMBERED.fullmatch(text) is not None
        )

    def get_help_text(self):
        return (
            "bots [list] [page]: + multi, * single, ? name only; "
            "4 hex chars identify contacts (extended if ambiguous); "
            "~ABC is a sender-label hint where no key is known. "
            "Distance is straight-line from configured bot position, or n/a. "
            "botsN: contact details. Evidence unverified."
        )


    def _command_responses(self):
        """Passively correlate public channel command requests and structured replies.

        Evidence is tied to the *display name*, NOT an authenticated MeshCore
        public key. No outgoing probes, no other users' private DMs. Events are
        retained in an on-disk idempotent table keyed to message_stats row ID.
        """
        found = {}
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return found, True
        now = int(time.time())
        try:
            window = int(self.get_config_value(
                "Bots_Command", "reply_window_seconds", fallback=45, value_type="int"
            ))
            retention = int(self.get_config_value(
                "Bots_Command", "evidence_retention_days",
                fallback=30, value_type="int"
            ))
        except (ValueError, TypeError):
            window, retention = 45, 30
        window = max(5, min(window, 120))
        retention = max(1, min(retention, 180))
        cutoff = now - retention * 86400
        request_pattern = re.compile(
            r"^\s*(?:@\[[^\]\r\n]{1,64}\]\s*)?!?(ping|path|test)(?:\s|$)", re.I
        )
        # The default upstream test reply is: ack @[requester] | SNR... .
        test_reply = re.compile(
            r"^\s*ack\s+@\[(?P<target>[^\]\r\n]{1,64})\](?:\s|\||$)", re.I
        )
        # Built-in path replies list hexadecimal route prefixes with names,
        # or use an explicit "Path:" label. This is a heuristic, not proof.
        path_reply = re.compile(
            r"^\s*(?:(?:📡\s*)?(?:path\s*:|direct connection\b)"
            r"|[0-9a-f]{2}(?:[0-9a-f]{2}){0,2}\s*:\s*\S"
            r"|❌\s*(?:no path\b|no valid hex\b|error decoding path\b))",
            re.I
        )
        try:
            with db.connection() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS bot_command_responses (
                        stats_row_id INTEGER PRIMARY KEY,
                        sender_name TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        command_name TEXT NOT NULL,
                        observed_at INTEGER NOT NULL
                    )"""
                )
                conn.execute(
                    "DELETE FROM bot_command_responses WHERE observed_at < ?", (cutoff,)
                )
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(message_stats)")
                }
                required = {
                    "id", "timestamp", "sender_id", "channel", "content", "is_dm"
                }
                if required.issubset(columns):
                    clock = (
                        "COALESCE(CAST(strftime('%s', created_at) AS INTEGER), timestamp)"
                        if "created_at" in columns else "timestamp"
                    )
                    # Newest bounded history: low cost, no full-message export.
                    history = conn.execute(
                        f"""SELECT id, {clock}, sender_id, channel, content
                            FROM message_stats
                            WHERE is_dm = 0
                            ORDER BY id DESC LIMIT 4000"""
                    ).fetchall()
                    requests = {}
                    for stat_id, timestamp, sender, channel, body in reversed(history):
                        try:
                            stamp = int(timestamp)
                        except (ValueError, TypeError):
                            continue
                        if stamp < cutoff or stamp > now + 60:
                            continue
                        source = str(sender or "").strip()
                        ch = str(channel or "").strip().casefold()
                        message = str(body or "").strip()
                        if (not ch or not source or
                            source.casefold() in UNATTRIBUTABLE or
                            ANON_SENDER.fullmatch(source)):
                            continue
                        request = request_pattern.match(message)
                        if request:
                            command = request.group(1).lower()
                            requests.setdefault(ch, []).append(
                                (stamp, source, command)
                            )
                            requests[ch] = requests[ch][-60:]
                            continue

                        reply_kind = None
                        target = None
                        match = PONG_REPLY.match(message) or NAMED_PING_REPLY.match(message)
                        if match:
                            reply_kind, target = "ping", match.groupdict().get("target")
                        else:
                            match = test_reply.match(message)
                            if match:
                                reply_kind, target = "test", match.group("target")
                            elif path_reply.match(message):
                                reply_kind = "path"
                        if not reply_kind:
                            continue

                        correlated = any(
                            req_kind == reply_kind
                            and 0 <= stamp - req_stamp <= window
                            and source.casefold() != req_sender.casefold()
                            and (not target or target.casefold() == req_sender.casefold())
                            for req_stamp, req_sender, req_kind
                            in reversed(requests.get(ch, []))
                        )
                        if correlated:
                            conn.execute(
                                """INSERT OR IGNORE INTO bot_command_responses
                                   (stats_row_id, sender_name, channel,
                                    command_name, observed_at)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (stat_id, source, ch, reply_kind, stamp),
                            )
                conn.commit()
                rows = conn.execute(
                    """SELECT sender_name, command_name, COUNT(*), MAX(observed_at)
                         FROM bot_command_responses
                         WHERE observed_at >= ?
                         GROUP BY lower(sender_name), command_name""",
                    (cutoff,),
                ).fetchall()
                # A full sender key is saved only when the radio supplied one
                # and the operator has not enabled Stats anonymization.
                # Do not infer identity from a display label or packet timing.
                known_keys = {}
                if "sender_pubkey" in columns and required.issubset(columns):
                    linked = conn.execute(
                        """SELECT b.sender_name, m.sender_pubkey
                             FROM bot_command_responses AS b
                             JOIN message_stats AS m ON m.id = b.stats_row_id
                             WHERE b.observed_at >= ?
                               AND m.sender_pubkey IS NOT NULL""",
                        (cutoff,),
                    ).fetchall()
                    for name, value in linked:
                        if isinstance(value, str) and KEY.fullmatch(value):
                            known_keys.setdefault(
                                str(name).casefold(), set()
                            ).add(value.lower())
                for name, kind, count, last in rows:
                    key = str(name).casefold()
                    prior = found.setdefault(key, [str(name), 0, 0, set()])
                    prior[1] += int(count)
                    prior[2] = max(prior[2], int(last))
                    prior[3].add(str(kind))
                return {
                    key: (
                        item[0], item[1], item[2],
                        tuple(sorted(item[3])),
                        next(iter(known_keys[key])) if len(known_keys.get(key, ())) == 1 else "",
                    )
                    for key, item in found.items()
                }, not required.issubset(columns)
        except (AttributeError, sqlite3.Error, OSError) as exc:
            self.logger.debug("bot responder evidence unavailable: %s", exc)
            return {}, True

    def _contacts(self):
        """Return (full_key, name, evidence), sorted by name, plus incomplete flag."""
        contacts = {}
        limited = False

        def add(key, name):
            if isinstance(key, str) and KEY.fullmatch(key):
                contacts[key.lower()] = str(name or "unnamed")

        try:
            with self.bot.db_manager.connection() as conn:
                # Chronological query: newest stored name for each full identity wins.
                for table, stamp in (("repeater_contacts", "last_seen"),
                                     ("complete_contact_tracking", "last_heard")):
                    try:
                        rows = conn.execute(
                            f"SELECT public_key, name FROM {table} "
                            f"ORDER BY {stamp}, public_key, name"
                        ).fetchall()
                        for key, name in rows:
                            add(key, name)
                    except sqlite3.Error:
                        limited = True
        except (AttributeError, sqlite3.Error, OSError):
            limited = True

        cached = getattr(getattr(self.bot, "meshcore", None), "contacts", None)
        if isinstance(cached, dict):
            for key, contact in cached.items():
                if isinstance(contact, dict):
                    add(contact.get("public_key") or key,
                        contact.get("adv_name") or contact.get("name"))
        else:
            limited = True

        observed, evidence_partial = self._command_responses()
        limited = limited or evidence_partial
        by_name = {}
        for key, name in contacts.items():
            by_name.setdefault(str(name).casefold(), []).append(key)

        rows = []
        matched = set()
        for key, name in contacts.items():
            norm = str(name).casefold()
            evidence = observed.get(norm)
            if evidence and (
                evidence[4] == key if evidence[4]
                else len(by_name.get(norm, [])) == 1
            ):
                rows.append((
                    key, label(name, 90),
                    "multi~" if len(evidence[3]) >= 2 else "cmd~",
                    evidence[1], evidence[2], evidence[3],
                ))
                matched.add(norm)
            elif BOT_NAME.search(name):
                rows.append((key, label(name, 90), "name?", 0, None, ()))
        for norm, (name, count, last, kinds, observed_key) in observed.items():
            if norm not in matched:
                # Full keys are only from saved radio-provided sender fields;
                # public channel labels are not authenticated identities.
                existing = next(
                    (i for i, row in enumerate(rows)
                     if observed_key and row[0] == observed_key), None
                )
                value = (
                    observed_key, label(name, 90),
                    "multi~" if len(kinds) >= 2 else "cmd~", count, last, kinds,
                )
                if existing is not None:
                    rows[existing] = value
                else:
                    rows.append(value)
        rows.sort(key=lambda row: (
            {"multi~": 0, "cmd~": 1, "name?": 2}.get(row[2], 3), row[1].casefold(), row[0]
        ))
        return rows, limited

    def _origin(self):
        """Configured node position; no guessed or geocoded fallback."""
        try:
            lat = self.bot.config.getfloat("Bot", "bot_latitude", fallback=None)
            lon = self.bot.config.getfloat("Bot", "bot_longitude", fallback=None)
            if lat is None or lon is None:
                return None
            lat, lon = float(lat), float(lon)
            if (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and (lat, lon) != (0.0, 0.0)):
                return lat, lon
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
        return None

    def _distance(self, full_key):
        """Haversine km from this bot to latest valid *advertised* contact fix.

        The contact database contains location evidence, not verified identity.
        A name-only or channel-only responder without a unique key gets n/a.
        """
        origin = self._origin()
        if not full_key or not origin:
            return "n/a"
        try:
            with self.bot.db_manager.connection() as conn:
                fields = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(complete_contact_tracking)"
                    )
                }
                if not {"public_key", "latitude", "longitude"}.issubset(fields):
                    return "n/a"
                stamp = (
                    "COALESCE(last_advert_timestamp, last_heard)"
                    if {"last_advert_timestamp", "last_heard"}.issubset(fields)
                    else "last_heard" if "last_heard" in fields else "rowid"
                )
                rows = conn.execute(
                    f"""SELECT latitude, longitude
                        FROM complete_contact_tracking
                        WHERE lower(public_key) = ?
                          AND latitude IS NOT NULL AND longitude IS NOT NULL
                          AND NOT (latitude = 0 AND longitude = 0)
                        ORDER BY {stamp} DESC LIMIT 50""",
                    (full_key.lower(),),
                ).fetchall()
            for lat, lon in rows:
                try:
                    lat, lon = float(lat), float(lon)
                    if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                        continue
                    r1, r2 = map(math.radians, origin)
                    r3, r4 = math.radians(lat), math.radians(lon)
                    a = (math.sin((r3-r1)/2) ** 2
                         + math.cos(r1)*math.cos(r3)
                         * math.sin((r4-r2)/2) ** 2)
                    km = 6371.0 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))
                    return f"{km:.1f}km"
                except (TypeError, ValueError, OverflowError):
                    continue
        except (AttributeError, sqlite3.Error, OSError) as exc:
            self.logger.debug("bot location unavailable: %s", exc)
        return "n/a"

    @staticmethod
    def _short_ids(records):
        """Four hex digits normally; expand on actual identity-prefix collisions."""
        keys = [row[0].lower() for row in records if row[0]]
        counts = Counter(key[:4] for key in keys)
        result = {}
        for key in keys:
            length = 4
            if counts[key[:4]] > 1:
                while length < 64 and sum(
                    other.startswith(key[:length]) for other in keys
                ) > 1:
                    length += 2
            result[key] = key[:length].upper()
        return result

    def _recorded_stats(self, full_key):
        """Select the freshest stored contact row, using optional schema fields."""
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return {}
        try:
            with db.connection() as conn:
                for table, stamp in TABLES:
                    try:
                        available = {
                            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                        }
                        if not {"public_key", stamp}.issubset(available):
                            continue
                        fields = [
                            field for field in (
                                stamp, "role", "device_type", "advert_count",
                                "snr", "hop_count", "signal_strength", "first_heard"
                            ) if field in available
                        ]
                        query = (
                            f"SELECT {', '.join(fields)} FROM {table} "
                            f"WHERE lower(public_key) = ? "
                            f"ORDER BY {stamp} DESC LIMIT 1"
                        )
                        row = conn.execute(query, (full_key.lower(),)).fetchone()
                        if row:
                            return dict(zip(fields, row))
                    except sqlite3.Error:
                        continue
        except (AttributeError, sqlite3.Error, OSError):
            pass
        return {}

    def _details(self, index, records, limited):
        key, name, evidence, reply_count, last_reply, kinds = records[index - 1]
        stats = self._recorded_stats(key) if key else {}
        role = label(stats.get("role") or stats.get("device_type") or "unknown", 18)
        last = age(stats.get("last_heard") or stats.get("last_seen"))
        adverts = stats.get("advert_count")
        ads = str(adverts) if isinstance(adverts, int) and adverts >= 0 else "n/a"
        snr = snr_display(stats.get("snr"))
        hops = stats.get("hop_count")
        hop_text = str(hops) if isinstance(hops, int) and 0 <= hops < 255 else "n/a"
        badge = "+" if evidence == "multi~" else "*" if evidence == "cmd~" else "?"
        kind_summary = ",".join(kinds) if kinds else "none"
        last_response = age(
            datetime.fromtimestamp(last_reply, timezone.utc).isoformat()
        ) if last_reply else "n/a"
        lines = [
            f"Bot {index}/{len(records)} {badge} {label(name, 30)}",
            f"ID {key[:12] if key else 'n/a'} | {role}",
            f"Dist {self._distance(key)} | Seen {last}",
            f"Ads {ads} | SNR {snr} | Hops {hop_text}",
            f"Reply {reply_count}x {kind_summary} ({last_response})",
            "Partial data" if limited else "Local only; unverified",
        ]
        return lines

    @staticmethod
    def _unresolved_label(name):
        """Four-character display hint, clearly NOT a public key prefix."""
        text = "".join(char for char in str(name) if char.isascii() and char.isalnum())
        return "~" + ((text[:3]).upper() or "???")

    def _pages(self, records, budget, limited):
        """Greedily pack indexed four-hex-ID + geographic-distance entries."""
        count = len(records)
        footer = (
            "partial +=multi *=reply ?=name ~=label"
            if limited else "+=multi *=reply ?=name ~=label"
        )
        reserve = len(f"Bots {count}/{count} ({count})\n".encode("utf-8"))
        space = max(1, budget - reserve - len(("\n" + footer).encode("utf-8")))
        ids = self._short_ids(records)
        lines = [
            f"{i}. {'+' if record[2] == 'multi~' else '*' if record[2] == 'cmd~' else '?'}"
            f"{ids.get(record[0].lower(), self._unresolved_label(record[1]))} "
            f"{self._distance(record[0])}"
            for i, record in enumerate(records, start=1)
        ]
        pages = []
        page = []
        used = 0
        for line in lines:
            # Even in extremely small channel budgets, never split an emoji.
            line = label(line, max(1, space))
            extra = len(line.encode("utf-8")) + (1 if page else 0)
            if page and used + extra > space:
                pages.append(page)
                page, used = [], 0
                extra = len(line.encode("utf-8"))
            page.append(line)
            used += extra
        if page:
            pages.append(page)
        return pages, footer

    async def _respond_lines(self, message, lines):
        """Never send a partially truncated detail line."""
        budget = max(0, min(158, self.get_max_message_length(message)))
        output = ""
        for line in lines:
            possible = output + ("\n" if output else "") + str(line)
            if len(possible.encode("utf-8")) <= budget:
                output = possible
            else:
                break
        return await self.send_response(message, output or "No room for bot details.")

    async def execute(self, message: MeshMessage) -> bool:
        if not self.is_channel_allowed(message):
            return False
        text = str(message.content or "").strip().removeprefix("!").lower()
        command = NUMBERED.fullmatch(text)
        mode, number = None, 1
        if command:
            mode, number = "details", int(command.group(1))
        else:
            tokens = text.split()
            if tokens in (["bots"], ["bots", "list"]):
                mode = "list"
            elif (len(tokens) == 2 and tokens[0] == "bots"
                  and POSITIVE.fullmatch(tokens[1])):
                mode, number = "list", int(tokens[1])  # old page syntax
            elif (len(tokens) == 3 and tokens[:2] == ["bots", "list"]
                  and POSITIVE.fullmatch(tokens[2])):
                mode, number = "list", int(tokens[2])
            elif (len(tokens) == 3 and tokens[:2] == ["bots", "page"]
                  and POSITIVE.fullmatch(tokens[2])):
                mode, number = "list", int(tokens[2])
            elif (len(tokens) == 3 and tokens[:2] in (
                ["bots", "info"], ["bots", "detail"]
            ) and POSITIVE.fullmatch(tokens[2])):
                mode, number = "details", int(tokens[2])

        if mode is None:
            return await self._respond_lines(
                message, ["Usage: !bots [list] [page] or !botsN (details)"]
            )

        records, limited = self._contacts()
        if not records:
            return await self._respond_lines(message, [
                "No bot-name candidates or command responders.",
                "Local data incomplete." if limited else "No observed ping evidence."
            ])

        if mode == "details":
            if number > len(records):
                return await self._respond_lines(
                    message, [f"Only {len(records)} bot entries. Try !bots."]
                )
            return await self._respond_lines(
                message, self._details(number, records, limited)
            )

        budget = max(1, min(158, self.get_max_message_length(message)))
        pages, footer = self._pages(records, budget, limited)
        if number > len(pages):
            return await self._respond_lines(
                message, [f"Only {len(pages)} bots list pages. Use !bots."]
            )
        response = [
            f"Bots {number}/{len(pages)} ({len(records)})",
            *pages[number - 1],
            footer,
        ]
        return await self._respond_lines(message, response)
