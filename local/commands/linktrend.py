#!/usr/bin/env python3
"""Read-only zero-hop neighbour SNR history for the MeshCore Bot.

Install as local/commands/linktrend.py. No discovery or RF transmission except
one requested DM reply. Uses migration 22's neighbor_observations table.
"""

from datetime import datetime, timedelta, timezone
from math import isfinite
import re
import sqlite3
from typing import Any

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

WINDOWS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
HEX_KEY = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _when(value: Any) -> datetime | None:
    """Parse migration-22 ISO timestamps, including older naive UTC values."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _ago(timestamp: datetime, now: datetime) -> str:
    seconds = max(0, int((now - timestamp).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _snr(value: float) -> str:
    return f"{value:+.1f}dB"


class LinkTrendCommand(BaseCommand):
    name = "linktrend"
    keywords = ["linktrend"]
    description = "Stored zero-hop neighbour SNR history (DM only)"
    short_description = "Check whether direct neighbour SNR is improving or weakening"
    usage = "linktrend [weak|HEXKEY] [7d|30d|90d|365d]"
    examples = ["linktrend", "linktrend weak", "linktrend A1B2C3D4 7d"]
    category = "meshcore_info"
    requires_dm = True
    cooldown_seconds = 20

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            "Linktrend_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return self.command_enabled and super().can_execute(message, skip_channel_check)

    def get_help_text(self) -> str:
        return (
            "DM: linktrend [7d|30d|90d|365d]; "
            "linktrend weak [window]; linktrend <hexkey> [window]. "
            "Uses stored direct-neighbour SNR."
        )

    async def _send_lines(self, message: MeshMessage, lines: list[str]) -> bool:
        """Send one complete-lines-only reply within the firmware byte budget."""
        budget = self.get_max_message_length(message)
        output = ""
        for line in lines:
            candidate = output + ("\n" if output else "") + line
            if len(candidate.encode("utf-8")) > budget:
                break
            output = candidate
        return await self.send_response(message, output or "No link data to display")

    def _observations(self, days: int) -> tuple[str | None, list[tuple[str, datetime, float]]]:
        """Read samples for the latest bot radio public key, never mixed radios."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        with self.bot.db_manager.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='neighbor_observations'"
            ).fetchone():
                return None, []
            last = conn.execute(
                """SELECT self_public_key FROM neighbor_observations
                   ORDER BY observed_at DESC, id DESC LIMIT 1"""
            ).fetchone()
            if not last:
                return None, []
            self_key = str(last[0])
            rows = conn.execute(
                """SELECT neighbor_public_key, observed_at, snr
                   FROM neighbor_observations
                   WHERE lower(self_public_key) = lower(?)
                     AND julianday(observed_at) >= julianday(?)
                   ORDER BY observed_at DESC, id DESC""",
                (self_key, cutoff.isoformat()),
            ).fetchall()
        readings: list[tuple[str, datetime, float]] = []
        for row in rows:
            seen = _when(row[1])
            if not seen or seen < cutoff or seen > now + timedelta(minutes=5):
                continue
            try:
                snr = float(row[2])
            except (TypeError, ValueError):
                continue
            if not isfinite(snr):
                continue
            key = str(row[0] or "").lower()
            if not key:
                continue
            readings.append((key, seen, snr))
        readings.sort(key=lambda record: record[1], reverse=True)
        return self_key, readings

    async def execute(self, message: MeshMessage) -> bool:
        if not message.is_dm:
            return True
        parts = (message.content or "").strip().removeprefix("!").split()
        args = [part.lower() for part in parts[1:]]
        if args and args[0] == "help":
            return await self._send_lines(message, [self.get_help_text()])
        if len(args) > 2:
            return await self._send_lines(message, [self.get_help_text()])

        target: str | None = None
        window = "30d"
        if args:
            if args[0] in WINDOWS:
                if len(args) != 1:
                    return await self._send_lines(message, [self.get_help_text()])
                window = args[0]
            elif args[0] == "weak" or HEX_KEY.fullmatch(args[0]):
                target = args[0]
                if len(args) == 2:
                    if args[1] not in WINDOWS:
                        return await self._send_lines(message, [self.get_help_text()])
                    window = args[1]
            else:
                return await self._send_lines(message, [self.get_help_text()])

        try:
            self_key, observations = self._observations(WINDOWS[window])
        except (sqlite3.Error, OSError, AttributeError) as exc:
            self.logger.error("linktrend DB read failed: %s", exc)
            return await self._send_lines(message, ["LinkTrend: neighbour DB unavailable"])
        if self_key is None:
            return await self._send_lines(message, ["LinkTrend: no discovery history. Ask admin to run !neighbors."])
        if not observations:
            return await self._send_lines(message, [f"LinkTrend: no SNR samples in {window}."])

        now = datetime.now(timezone.utc)
        latest_time = observations[0][1]
        # First reading per key is newest (the query sorts descending).
        latest = {}
        for key, stamp, snr in observations:
            latest.setdefault(key, (stamp, snr))

        if target == "weak":
            # Compare the last discovery cycle only, not stale neighbours.
            recent = [(key, snr) for key, stamp, snr in observations if stamp == latest_time]
            recent.sort(key=lambda pair: pair[1])
            lines = [f"Weakest last cycle ({window})"]
            lines.extend(f"{key[:8].upper()} {_snr(snr)}" for key, snr in recent[:5])
            return await self._send_lines(message, lines)

        if target is None:
            recent = [snr for _, stamp, snr in observations if stamp == latest_time]
            cycles = len({stamp for _, stamp, _ in observations})
            weakest_key, (_, weakest_snr) = min(latest.items(), key=lambda p: p[1][1])
            return await self._send_lines(message, [
                f"LinkTrend {window}: {cycles} cycles",
                f"Last {_ago(latest_time, now)} ago: {len(recent)} heard",
                f"Last avg: {_snr(sum(recent) / len(recent))}",
                f"Weakest seen: {weakest_key[:8].upper()} {_snr(weakest_snr)}",
                "Try: linktrend weak or <hexkey>",
            ])

        matched = sorted({key for key in latest if key.startswith(target)})
        if not matched:
            return await self._send_lines(message, [f"No neighbour matching {target.upper()} in {window}."])
        if len(matched) > 1:
            return await self._send_lines(message, [
                f"Ambiguous {target.upper()}: {len(matched)} matches",
                *[key[:16].upper() for key in matched[:5]],
                "Use a longer hex prefix",
            ])
        key = matched[0]
        values = [(stamp, snr) for k, stamp, snr in observations if k == key]
        # Only one reading per cycle; table is normally already one row per cycle.
        unique: dict[datetime, float] = {}
        for stamp, snr in values:
            unique.setdefault(stamp, snr)
        values = list(unique.items())
        stamp, last_snr = values[0]
        lines = [f"{key[:8].upper()} {window}: {len(values)} cycles",
                 f"Last {_snr(last_snr)} ({_ago(stamp, now)} ago)"]
        if len(values) > 1:
            previous_snr = values[1][1]
            lines.append(f"Prev {_snr(previous_snr)}; delta {_snr(last_snr - previous_snr)}")
            nums = [snr for _, snr in values]
            lines.append(f"Avg {_snr(sum(nums)/len(nums))}; range {_snr(min(nums))}..{_snr(max(nums))}")
        else:
            lines.append("One sample: no trend yet")
        return await self._send_lines(message, lines)
