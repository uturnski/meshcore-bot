"""Local DM-only command to summarize a user's previously recorded DM routes.

Reads StatsCommand.message_stats; does not initiate RF probes. Because upstream
message_stats stores a contact display name (not the sender's public key),
history may include DMs from another device with the same display name.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import re
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
# A path must start with an actual hexadecimal repeater sequence; explanatory
# strings like "Routed through 2 hops" are NOT proof of a known route.
PATH_RE = re.compile(r"^([0-9A-Fa-f]{2,6}(?:\s*,\s*[0-9A-Fa-f]{2,6})*)(?:\s*\(|\s*$)")


def _route(path):
    if not path:
        return None
    raw = str(path).strip()
    if re.match(r"^Direct\s*\(0 hops?\)", raw, re.IGNORECASE):
        return "DIRECT"
    matched = PATH_RE.match(raw)
    if not matched:
        return None
    return ",".join(x.strip().upper() for x in matched.group(1).split(","))


def _ago(timestamp):
    seconds = max(0, int(time.time() - float(timestamp)))
    return f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 3600}h" if seconds >= 3600 else f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


class MyPathsCommand(BaseCommand):
    name = "mypaths"
    keywords = ["mypaths"]
    description = "Show previously recorded incoming DM routes (DM only)"
    short_description = "Historical routes to this bot's radio"
    usage = "mypaths [24h|7d|30d]"
    examples = ["mypaths", "mypaths 7d"]
    category = "meshcore_info"
    requires_dm = True
    cooldown_seconds = 20

    def __init__(self, bot):
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            "Mypaths_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message, skip_channel_check=False):
        return self.command_enabled and super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self):
        return "DM: mypaths [24h|7d|30d]. Historical DM routes seen by this bot; not a live trace. Identified by saved contact name."

    async def _reply(self, message, lines):
        limit = self.get_max_message_length(message)
        out = ""
        for line in lines:
            candidate = f"{out}\n{line}" if out else line
            if len(candidate.encode("utf-8")) > limit:
                break
            out = candidate
        return await self.send_response(message, out or "MyPaths: no report")

    async def execute(self, message: MeshMessage) -> bool:
        if not message.is_dm:
            return True
        parts = (message.content or "").strip().removeprefix("!").split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in WINDOWS):
            return await self._reply(message, [self.get_help_text()])
        window = parts[1].lower() if len(parts) > 1 else "24h"
        name = str(message.sender_id or "").strip()
        if not name or name.casefold() == "unknown":
            return await self._reply(message, ["MyPaths: no authenticated contact label for history"])
        # Upstream StatsCommand optionally hashes sender *display names*.
        anonymize = self.bot.config.getboolean("Stats_Command", "anonymize_users", fallback=False)
        lookup_name = f"user_{hashlib.md5(name.encode()).hexdigest()[:8]}" if anonymize else name
        cutoff = int(time.time()) - WINDOWS[window]
        try:
            with self.bot.db_manager.connection() as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_stats'").fetchone()
                if not exists:
                    return await self._reply(message, ["MyPaths: stats database not initialized"])
                rows = conn.execute(
                    """SELECT timestamp, path FROM message_stats
                    WHERE is_dm = 1 AND sender_id = ? AND timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC, id DESC LIMIT 1500""",
                    (lookup_name, cutoff, int(time.time()) + 60),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.logger.error("mypaths history query failed: %s", exc)
            return await self._reply(message, ["MyPaths: history database unavailable"])
        if not rows:
            return await self._reply(message, [f"MyPaths {window}: no saved DMs for this name"])
        counts = Counter(filter(None, (_route(row[1]) for row in rows)))
        if not counts:
            return await self._reply(message, [f"MyPaths {window}: {len(rows)} DMs, but no decoded route data"])
        lines = [f"MyPaths {window}: {len(rows)} DMs, {len(counts)} routes"]
        for route, count in counts.most_common(3):
            shown = route if len(route) <= 39 else route[:36] + "..."
            lines.append(f"{shown}: {count}x")
        lines.append(f"Last DM: {_ago(rows[0][0])} ago")
        return await self._reply(message, lines)