"""Passive bot-*candidate* directory from local MeshCore contacts, never RF scans.

!bots [page] / !bots page N  -> numbered, paginated name list
!botsN / !bots info N        -> local details for entry N (N is a list index)

Names containing "bot" are unverified guesses; MeshCore has no bot flag.
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
    description = "Local bot-name directory; !botsN shows recorded contact stats"
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
            "bots [page]: numbered bot-name list. botsN (e.g. bots2) or "
            "bots info N: stored last-seen/advert/SNR/hops. "
            "Name matches only; unverified."
        )

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

        return sorted(
            ((key, label(name, 90), "name?") for key, name in contacts.items()
             if BOT_NAME.search(name)),
            key=lambda row: (row[1].casefold(), row[0]),
        ), limited

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
        key, name, _ = records[index - 1]
        stats = self._recorded_stats(key)
        role = label(stats.get("role") or stats.get("device_type") or "unknown", 18)
        last = age(stats.get("last_heard") or stats.get("last_seen"))
        adverts = stats.get("advert_count")
        ads = str(adverts) if isinstance(adverts, int) and adverts >= 0 else "n/a"
        snr = snr_display(stats.get("snr"))
        hops = stats.get("hop_count")
        hop_text = str(hops) if isinstance(hops, int) and 0 <= hops < 255 else "n/a"
        lines = [
            f"Bot {index}/{len(records)} (unverified)",
            label(name, 42),
            f"ID {key[:12]} | {role}",
            f"Seen {last} ago | Ads {ads}" if last != "n/a" else f"Seen n/a | Ads {ads}",
            f"SNR {snr} | Hops {hop_text}",
            "Partial local stats" if limited else "Stored local stats only",
        ]
        return lines

    def _pages(self, records, budget, limited):
        """Greedily pack numbered *names* under an exact conservative byte cap."""
        count = len(records)
        footer = (
            "Partial; name? unverified. !botsN info"
            if limited else "name? unverified. !botsN info"
        )
        # Reserve the largest possible header, independent of final page count.
        reserve = len(f"Bots {count}/{count} ({count})\n".encode("utf-8"))
        space = max(1, budget - reserve - len(("\n" + footer).encode("utf-8")))
        # Leave enough room for at least two entries even in a small
        # channel packet; long names are abbreviated rather than hidden.
        max_line = max(8, min(34, (space - 1) // 2))
        lines = [
            f"{i}. {label(record[1], max(1, max_line - len(f'{i}. '.encode('utf-8'))))}"
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
                "No saved bot-name candidates.",
                "Local data incomplete." if limited else "Names are unverified."
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
