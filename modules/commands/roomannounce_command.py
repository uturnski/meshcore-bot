"""Admin-only DM trigger for the one-way Room Server announcement service.

This command is independent of [Announcements_Command], which handles channel
announcements and may remain disabled. It never accepts an ad-hoc message.
"""
from __future__ import annotations

from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand


class RoomAnnounceCommand(BaseCommand):
    """Immediately publish the configured Room Server announcement via a DM."""

    name = "roomannounce"
    keywords = ["announce.room", "annouce.room"]
    description = "Send configured network stats to Room Server(s) now (admin DM only)"
    short_description = "Immediately publish the Room Server announcement"
    usage = "announce.room"
    examples = ["announce.room", "annouce.room"]
    requires_dm = True
    admin_only = True
    render_safe = False
    category = "admin"

    def __init__(self, bot: Any):
        super().__init__(bot)

    async def execute(self, message: MeshMessage) -> bool:
        # Defense in depth: do not assume only CommandManager invokes execute.
        if not self.can_execute(message):
            return False

        _trigger, args = self.split_trigger_and_args(message.content)
        if args:
            await self.send_response(
                message, "Usage: announce.room (DM only; no arguments)",
                skip_user_rate_limit=True,
            )
            return True

        room_service = getattr(self.bot, "services", {}).get("roomannouncements")
        if room_service is None or not room_service.is_healthy():
            await self.send_response(
                message, "Room announcements service is not enabled or running.",
                skip_user_rate_limit=True,
            )
            return True

        try:
            summary: str = await room_service.publish_manual()
        except Exception:
            self.logger.exception("Room announcement manual DM trigger failed")
            summary = "Room announcement failed. Check bot logs."

        await self.send_response(message, summary, skip_user_rate_limit=True)
        return True
