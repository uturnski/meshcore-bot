#!/usr/bin/env python3
"""Local MeshCore Bot command: !heardme / !seenme.

Read-only reception report from the built-in Stats_Command.message_stats and
path_stats tables. Only reports DMs recorded under the requesting sender ID.

Install at: /opt/meshcore-bot/local/commands/heardme.py
Requires: agessaman/meshcore-bot with local command plugin support.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


class HeardMeCommand(BaseCommand):
    name = "heardme"
    keywords = ["heardme", "seenme"]
    description = "Show how the bot has heard your DMs (SNR, hops, history)"
    short_description = "Personal RF reception statistics from this bot"
    usage = "heardme [1h|6h|24h|7d]"
    examples = ["heardme", "heardme 1h", "heardme 7d", "seenme"]
    category = "meshcore_info"
    requires_dm = True
    cooldown_seconds = 30

    WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value(
            "Heardme_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return self.enabled and super().can_execute(message, skip_channel_check)

    def get_help_text(self) -> str:
        """Short, complete help for the upstream !help <command> handler."""
        return (
            "heardme [1h|6h|24h|7d] (DM only): current SNR/hops, "
            "received DM count, historical SNR avg/range and routes. Alias: seenme."
        )

    @staticmethod
    def _snr(value: Optional[float]) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):+.1f}dB"

    @staticmethod
    def _history_sender_id(message: MeshMessage, anonymized: bool) -> Optional[str]:
        """Match StatsCommand.record_message's sender_id representation exactly."""
        sender = (message.sender_id or "").strip()
        if not sender or sender.lower() == "unknown":
            return None
        if anonymized:
            sender = "user_" + hashlib.md5(sender.encode("utf-8")).hexdigest()[:8]
        return sender

    def _history_enabled(self) -> bool:
        """Don't imply coverage if upstream message recording was turned off."""
        try:
            return (
                self.bot.config.getboolean("Stats_Command", "collect_stats", fallback=True)
                and self.bot.config.getboolean("Stats_Command", "track_all_messages", fallback=True)
            )
        except (AttributeError, ValueError):
            return False

    def _sender_anonymized(self) -> bool:
        try:
            return self.bot.config.getboolean(
                "Stats_Command", "anonymize_users", fallback=False
            )
        except (AttributeError, ValueError):
            return False

    def _query_history(self, sender: str, hours: int) -> Optional[dict]:
        """Read SQLite aggregates; return None if message_stats is unavailable.

        created_at is the DB's UTC receive time; message_stats.timestamp is a
        timestamp supplied by the transmitting node and may be inaccurate.
        """
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        until = now.strftime("%Y-%m-%d %H:%M:%S")

        manager = getattr(self.bot, "db_manager", None)
        if manager is None:
            return None

        with manager.connection() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_stats'"
            ).fetchone()
            if not table:
                return None
            row = conn.execute(
                """SELECT COUNT(*) AS dm_count,
                          COUNT(snr) AS snr_count,
                          AVG(snr) AS avg_snr,
                          MIN(snr) AS min_snr,
                          MAX(snr) AS max_snr
                   FROM message_stats
                   WHERE sender_id = ? AND is_dm = 1
                     AND datetime(created_at) >= datetime(?)
                     AND datetime(created_at) <= datetime(?)""",
                (sender, since, until),
            ).fetchone()
            # path_stats is optional; it only stores usable decoded routes.
            paths = None
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='path_stats'"
            ).fetchone():
                paths = conn.execute(
                    """SELECT COUNT(DISTINCT path_string)
                       FROM path_stats
                       WHERE sender_id = ? AND channel IS NULL
                         AND datetime(created_at) >= datetime(?)
                         AND datetime(created_at) <= datetime(?)""",
                    (sender, since, until),
                ).fetchone()[0]

        if row is None:
            return None
        return {
            "count": int(row[0]), "samples": int(row[1]),
            "avg": row[2], "min": row[3], "max": row[4], "paths": paths,
        }

    async def execute(self, message: MeshMessage) -> bool:
        # The command manager enforces DM-only, but keep a defensive check.
        if not message.is_dm:
            return True

        parts = (message.content or "").lstrip("!").strip().lower().split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in self.WINDOWS):
            return await self.send_response(message, self.get_help_text())
        window = parts[1] if len(parts) == 2 else "24h"

        hops = message.hops
        now_snr = self._snr(message.snr)
        hop_text = f"{hops} hop{'s' if hops != 1 else ''}" if isinstance(hops, int) and hops >= 0 else "hops n/a"
        lines = [f"HeardMe {window}", f"Now: {now_snr}, {hop_text}"]

        if not self._history_enabled():
            lines.append("History off (Stats_Command)")
        else:
            sender = self._history_sender_id(message, self._sender_anonymized())
            if not sender:
                lines.append("History unavailable: no sender ID")
            else:
                try:
                    history = self._query_history(sender, self.WINDOWS[window])
                except (sqlite3.Error, OSError) as exc:
                    self.logger.warning("HeardMe stats lookup failed: %s", exc)
                    history = None

                if history is None:
                    lines.append("History unavailable (stats DB)")
                else:
                    lines.append(f"Received DMs: {history['count']}")
                    if history["samples"]:
                        lines.append(
                            f"Hist SNR avg: {self._snr(history['avg'])} ({history['samples']})"
                        )
                        lines.append(
                            f"Range: {self._snr(history['min'])} to {self._snr(history['max'])}"
                        )
                    else:
                        lines.append("Hist SNR: no measured samples")
                    if history["paths"] is not None and history["paths"] > 0:
                        lines.append(f"Decoded routes: {history['paths']}")

        # DM payload limit is 158 UTF-8 bytes. Include only complete lines.
        limit = self.get_max_message_length(message)
        output = ""
        for line in lines:
            candidate = output + ("\n" if output else "") + line
            if len(candidate.encode("utf-8")) > limit:
                break
            output = candidate
        return await self.send_response(message, output)