"""Passive bot-*candidate* directory from local MeshCore contacts, never RF scans.

!bots [page] / !bots page N  -> numbered, paginated name list
!botsN / !bots info N        -> local details for entry N (N is a list index)

Names alone are guesses; correlated public ping replies are stronger but unverified.
"""
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
    description = "List local bot-name candidates and observed public ping responders"
    short_description = description
    usage = "bots [page] | botsN | bots info N"
    examples = ["bots", "bots page 2", "bots1"]
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
            "bots [page]: * means observed public ping reply; ? means name-only. "
            "botsN: stored contact stats and reply count. All unverified."
        )


    def _ping_responses(self):
        """Persist correlated public ping replies as *unverified* bot evidence.

        Never transmits ping, never inspects anyone else's DMs, and never
        treats a display name as a cryptographically verified identity.
        """
        found = {}
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return found, True
        now = int(time.time())
        try:
            window = int(self.get_config_value(
                "Bots_Command", "ping_reply_window_seconds",
                fallback=45, value_type="int"
            ))
            retention = int(self.get_config_value(
                "Bots_Command", "ping_evidence_retention_days",
                fallback=30, value_type="int"
            ))
        except (ValueError, TypeError):
            window, retention = 45, 30
        window = max(5, min(window, 120))
        retention = max(1, min(retention, 180))
        cutoff = now - retention * 86400
        try:
            with db.connection() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS bot_ping_responses (
                         stats_row_id INTEGER PRIMARY KEY,
                         sender_name TEXT NOT NULL,
                         channel TEXT NOT NULL,
                         observed_at INTEGER NOT NULL
                    )"""
                )
                conn.execute(
                    "DELETE FROM bot_ping_responses WHERE observed_at < ?", (cutoff,)
                )
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(message_stats)")
                }
                required = {
                    "id", "timestamp", "sender_id", "channel", "content", "is_dm"
                }
                if required.issubset(columns):
                    clock = ("COALESCE(CAST(strftime('%s', created_at) AS INTEGER), timestamp)"
                             if "created_at" in columns else "timestamp")
                    rows = conn.execute(
                        f"""SELECT id, {clock}, sender_id, channel, content
                            FROM message_stats
                            WHERE is_dm = 0
                            ORDER BY id DESC LIMIT 4000"""
                    ).fetchall()
                    requests = {}
                    for stat_id, observed, sender, channel, body in reversed(rows):
                        try:
                            stamp = int(observed)
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
                        if PING_REQUEST.match(message):
                            requests.setdefault(ch, []).append((stamp, source))
                            requests[ch] = requests[ch][-30:]
                            continue
                        match = PONG_REPLY.match(message) or NAMED_PING_REPLY.match(message)
                        if not match:
                            continue
                        target = match.groupdict().get("target")
                        relevant = any(
                            0 <= stamp - request_stamp <= window and
                            source.casefold() != request_sender.casefold() and
                            (not target or target.casefold() == request_sender.casefold())
                            for request_stamp, request_sender in reversed(requests.get(ch, []))
                        )
                        if relevant:
                            conn.execute(
                                """INSERT OR IGNORE INTO bot_ping_responses
                                   (stats_row_id, sender_name, channel, observed_at)
                                   VALUES (?, ?, ?, ?)""",
                                (stat_id, source, ch, stamp),
                            )
                conn.commit()
                evidence = conn.execute(
                    """SELECT sender_name, COUNT(*), MAX(observed_at)
                         FROM bot_ping_responses
                         WHERE observed_at >= ?
                         GROUP BY lower(sender_name)""",
                    (cutoff,),
                ).fetchall()
                for sender, count, last in evidence:
                    found[str(sender).casefold()] = (str(sender), int(count), int(last))
                return found, not required.issubset(columns)
        except (AttributeError, sqlite3.Error, OSError) as exc:
            self.logger.debug("bots ping evidence unavailable: %s", exc)
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

        observed, evidence_partial = self._ping_responses()
        limited = limited or evidence_partial
        by_name = {}
        for key, name in contacts.items():
            by_name.setdefault(str(name).casefold(), []).append(key)

        rows = []
        matched = set()
        for key, name in contacts.items():
            norm = str(name).casefold()
            evidence = observed.get(norm)
            if evidence and len(by_name.get(norm, [])) == 1:
                rows.append((key, label(name, 90), "ping~", evidence[1], evidence[2]))
                matched.add(norm)
            elif BOT_NAME.search(name):
                rows.append((key, label(name, 90), "name?", 0, None))
        for norm, (name, count, last) in observed.items():
            if norm not in matched:
                # Channel sender label may be spoofed/ambiguous; no verified key.
                rows.append(("", label(name, 90), "ping~", count, last))
        rows.sort(key=lambda row: (
            0 if row[2] == "ping~" else 1, row[1].casefold(), row[0]
        ))
        return rows, limited

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
        key, name, evidence, reply_count, last_reply = records[index - 1]
        stats = self._recorded_stats(key) if key else {}
        role = label(stats.get("role") or stats.get("device_type") or "unknown", 18)
        last = age(stats.get("last_heard") or stats.get("last_seen"))
        adverts = stats.get("advert_count")
        ads = str(adverts) if isinstance(adverts, int) and adverts >= 0 else "n/a"
        snr = snr_display(stats.get("snr"))
        hops = stats.get("hop_count")
        hop_text = str(hops) if isinstance(hops, int) and 0 <= hops < 255 else "n/a"
        lines = [
            f"Bot {index}/{len(records)} ({evidence})",
            label(name, 42),
            f"ID {key[:12] if key else 'n/a'} | {role}",
            (f"Ping {reply_count} | seen {age(datetime.fromtimestamp(last_reply, timezone.utc).isoformat())} ago"
             if reply_count and last_reply else "Ping replies 0"),
            f"Seen {last} ago | Ads {ads}" if last != "n/a" else f"Seen n/a | Ads {ads}",
            f"SNR {snr} | Hops {hop_text}",
            "Partial local stats" if limited else "Local stats only",
        ]
        return lines

    def _pages(self, records, budget, limited):
        """Greedily pack numbered *names* under an exact conservative byte cap."""
        count = len(records)
        footer = (
            "Partial; *=ping ?=name"
            if limited else "*=ping ?=name; unverified"
        )
        # Reserve the largest possible header, independent of final page count.
        reserve = len(f"Bots {count}/{count} ({count})\n".encode("utf-8"))
        space = max(1, budget - reserve - len(("\n" + footer).encode("utf-8")))
        # Leave enough room for at least two entries even in a small
        # channel packet; long names are abbreviated rather than hidden.
        max_line = max(8, min(34, (space - 1) // 2))
        lines = [
            f"{i}. {'*' if record[2] == 'ping~' else '?'}{label(record[1], max(1, max_line - len(f'{i}. *.'.encode('utf-8'))))}"
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
            if tokens == ["bots"]:
                mode = "list"
            elif (len(tokens) == 2 and tokens[0] == "bots"
                  and POSITIVE.fullmatch(tokens[1])):
                mode, number = "list", int(tokens[1])  # old page syntax
            elif (len(tokens) == 3 and tokens[:2] == ["bots", "page"]
                  and POSITIVE.fullmatch(tokens[2])):
                mode, number = "list", int(tokens[2])
            elif (len(tokens) == 3 and tokens[:2] in (
                ["bots", "info"], ["bots", "detail"]
            ) and POSITIVE.fullmatch(tokens[2])):
                mode, number = "details", int(tokens[2])

        if mode is None:
            return await self._respond_lines(
                message, ["Usage: !bots [page] or !botsN (details)"]
            )

        records, limited = self._contacts()
        if not records:
            return await self._respond_lines(message, [
                "No bot-name candidates or observed ping replies.",
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
