"""Passive, conservative bot directory from saved contacts; never queries RF."""

import re
import sqlite3
import unicodedata

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


KEY = re.compile(r"[0-9a-fA-F]{64}")
# User-selected name heuristic; candidates are explicitly unverified.
BOT_NAME = re.compile(r"bot", re.IGNORECASE)


def label(value, budget=32):
    text = " ".join("".join(
        c if not unicodedata.category(c).startswith("C") else " "
        for c in str(value or "unnamed")
    ).split())
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


class BotsCommand(BaseCommand):
    name = "bots"
    keywords = ["bots"]
    description = "List saved bot candidates; no RF scan (!bots [page])"
    short_description = description
    usage = "bots [page]"
    examples = ["bots", "bots 2"]
    category = "analytics"
    requires_dm = False
    cooldown_seconds = 3

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value('Bots_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message, skip_channel_check=False):
        return self.enabled and super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self):
        return "!bots [page]: saved bot candidates. Names are unverified; records may be stale/incomplete."

    def _contacts(self):
        """Return normalized identities and whether a source was unavailable."""
        contacts = {}
        limited = False

        def add(key, name):
            if isinstance(key, str) and KEY.fullmatch(key):
                # Most recent DB row wins; in-memory radio contact name wins over DB.
                contacts[key.lower()] = str(name or 'unnamed')

        try:
            with self.bot.db_manager.connection() as conn:
                for table, stamp in (("repeater_contacts", "last_seen"),
                                     ("complete_contact_tracking", "last_heard")):
                    try:
                        rows = conn.execute(
                            f"SELECT public_key, name FROM {table} ORDER BY {stamp}, public_key, name"
                        ).fetchall()
                        for key, name in rows:
                            add(key, name)
                    except sqlite3.Error:
                        limited = True
        except (AttributeError, sqlite3.Error, OSError):
            limited = True
        cached = getattr(getattr(self.bot, 'meshcore', None), 'contacts', None)
        if isinstance(cached, dict):
            for key, contact in cached.items():
                if isinstance(contact, dict):
                    add(contact.get('public_key') or key, contact.get('adv_name') or contact.get('name'))
        else:
            limited = True
        return sorted((key, label(name), 'name?')
                      for key, name in contacts.items()
                      if BOT_NAME.search(name)), limited

    async def _respond(self, message, text):
        # Channel budgets also account for the radio name and regional scope.
        budget = max(0, min(158, self.get_max_message_length(message)))
        text = text.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
        return await self.send_response(message, text)

    async def execute(self, message: MeshMessage) -> bool:
        if not self.is_channel_allowed(message):
            return False
        parts = message.content.strip().removeprefix('!').split()
        if len(parts) > 2 or (len(parts) == 2 and
                             (not re.fullmatch(r'[1-9][0-9]{0,5}', parts[1]))):
            return await self._respond(message, "Usage: !bots [page], page must be a positive integer.")
        page = int(parts[1]) if len(parts) == 2 else 1
        records, limited = self._contacts()
        caveat = "Local only; stale/incomplete. name?=unverified."
        if limited:
            caveat = "Partial local data; stale/incomplete. name?=unverified."
        if not records:
            return await self._respond(message, "No saved bot candidates. " + caveat)
        # One entry per request: bounded airtime even with huge contact sets. A 12-hex
        # prefix is a display label, never an identity match (which uses the full key).
        if page > len(records):
            return await self._respond(message, f"Only {len(records)} bots pages. " + caveat)
        key, name, evidence = records[page - 1]
        header = f"Bots {page}/{len(records)} ({evidence})\n{key[:12]} "
        footer = "\n" + caveat
        budget = min(158, self.get_max_message_length(message))
        name = label(name, max(0, budget - len((header + footer).encode('utf-8'))))
        return await self._respond(message, header + name + footer)
