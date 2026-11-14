"""Local DM-only observed RF packet activity summary.

Counts packets present in packet_stream, not collisions or network-wide load.
The packet_stream table is fed by the bot's web-viewer integration.
"""

from datetime import datetime, timezone
import sqlite3
import time

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

WINDOWS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}


class TrafficCommand(BaseCommand):
    name = "traffic"
    keywords = ["traffic"]
    description = "Observed RF packet counts at this bot's receiver (DM only)"
    short_description = "Packet activity at this observer"
    usage = "traffic [15m|1h|6h|24h]"
    examples = ["traffic", "traffic 6h"]
    category = "meshcore_info"
    requires_dm = True
    cooldown_seconds = 20

    def __init__(self, bot):
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            "Traffic_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message, skip_channel_check=False):
        return self.command_enabled and super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self):
        return "DM: traffic [15m|1h|6h|24h]. Counts packets saved by this bot receiver. Not collision rate or whole-mesh utilisation."

    async def _reply(self, message, lines):
        budget = self.get_max_message_length(message)
        output = ""
        for line in lines:
            candidate = output + ("\n" if output else "") + line
            if len(candidate.encode("utf-8")) > budget:
                break
            output = candidate
        return await self.send_response(message, output or "Traffic: no report")

    async def execute(self, message: MeshMessage) -> bool:
        if not message.is_dm:
            return True
        parts = (message.content or "").strip().removeprefix("!").split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in WINDOWS):
            return await self._reply(message, [self.get_help_text()])
        window = parts[1].lower() if len(parts) > 1 else "1h"
        end = time.time() + 60
        start = time.time() - WINDOWS[window]
        try:
            with self.bot.db_manager.connection() as conn:
                table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packet_stream'").fetchone()
                if not table:
                    return await self._reply(message, ["Traffic: packet_stream table unavailable"])
                columns = {row[1] for row in conn.execute("PRAGMA table_info(packet_stream)")}
                num = conn.execute(
                    "SELECT COUNT(*) FROM packet_stream WHERE type='packet' AND timestamp BETWEEN ? AND ?",
                    (start, end),
                ).fetchone()[0]
                if not num:
                    return await self._reply(message, [f"Traffic {window}: no saved RF packets. Check viewer integration."])
                last15 = conn.execute(
                    "SELECT COUNT(*) FROM packet_stream WHERE type='packet' AND timestamp BETWEEN ? AND ?",
                    (time.time() - 900, end),
                ).fetchone()[0]
                last60 = conn.execute(
                    "SELECT COUNT(*) FROM packet_stream WHERE type='packet' AND timestamp BETWEEN ? AND ?",
                    (time.time() - 3600, end),
                ).fetchone()[0]
                # For older installations lacking denormalized packet_type_name,
                # avoid parsing unbounded JSON packet contents in a DM command.
                top = []
                if "payload_type_name" in columns:
                    top = conn.execute(
                        """SELECT COALESCE(NULLIF(payload_type_name,''),'unknown'), COUNT(*) AS c
                        FROM packet_stream WHERE type='packet' AND timestamp BETWEEN ? AND ?
                        GROUP BY COALESCE(NULLIF(payload_type_name,''),'unknown')
                        ORDER BY c DESC LIMIT 3""",
                        (start, end),
                    ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.logger.error("traffic DB read failed: %s", exc)
            return await self._reply(message, ["Traffic: database unavailable"])
        lines = [f"Traffic RX {window}: {num} packets", f"15m: {last15} / 1h: {last60}"]
        if top:
            cats = ", ".join(f"{str(t[0])[:9]} {t[1]}" for t in top)
            lines.append(f"Types: {cats}")
        lines.append("Receiver-only count, not collisions")
        return await self._reply(message, lines)