"""Announcement-only publisher for MeshCore Room Servers.

No inbound room commands, automatic replies, bridging, or retransmission on an
unknown delivery outcome. This service shares the bot's existing radio link.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import string
from typing import Any

from meshcore.events import EventType

from .base_service import BaseServicePlugin

ROOM_TYPE = 3  # MeshCore AdvType.CHAT / Room Server
MAX_MESSAGE_BYTES = 150
CHECK_INTERVAL_SECONDS = 5
MAX_TEMPLATE_BYTES = 2048
STAT_FIELDS = (
    "total_contacts",
    "total_repeaters",
    "total_companions",
    "new_repeaters_7d",
    "new_companions_7d",
    "recent_activity_24h",
)


def _template_fields(template: str) -> frozenset[str]:
    """Only allow the documented numeric fields; reject attribute/index lookup."""
    fields: set[str] = set()
    try:
        for _, field, format_spec, conversion in string.Formatter().parse(template):
            if field is None:
                continue
            if field not in STAT_FIELDS or format_spec or conversion:
                raise ValueError(f"Unsupported placeholder {{{field}}}")
            fields.add(field)
    except ValueError as exc:
        raise ValueError(f"Invalid Room announcement template: {exc}") from exc
    return frozenset(fields)



def _public_key(contact: dict[str, Any]) -> str:
    key = contact.get("public_key", "")
    return key.hex().lower() if isinstance(key, bytes) else str(key).lower()


def _is_room(contact: Any) -> bool:
    if not isinstance(contact, dict):
        return False
    kind = contact.get("type")
    return getattr(kind, "value", kind) in (ROOM_TYPE, "3")


class RoomAnnouncementsService(BaseServicePlugin):
    """Post a short announcement to explicitly selected Room Servers hourly."""

    config_section = "Room_Announcements"
    description = "One-way scheduled and admin-triggered Room Server announcements"

    settings_schema = [
        {"key": "room_servers", "label": "Room Servers", "type": "list", "default": "",
         "help": "Comma-separated exact contact names or full public keys. No discovery-wide broadcast."},
        {"key": "password", "label": "Guest password", "type": "password", "default": "hello",
         "help": "Write-capable Room Server login password. Default guest password: hello."},
        {"key": "message", "label": "Announcement text", "type": "str", "default": "",
         "help": "Static text or network-stat placeholders; rendered result must be 1-150 UTF-8 bytes."},
        {"key": "schedule_minute", "label": "Minute of every hour", "type": "int",
         "min": 0, "max": 59, "default": 15,
         "help": "Publish once per server per local hour at this minute."},
        {"key": "login_timeout_seconds", "label": "Login wait", "type": "int",
         "min": 5, "max": 180, "default": 60, "unit": "s"},
        {"key": "ack_timeout_seconds", "label": "ACK wait", "type": "int",
         "min": 5, "max": 120, "default": 25, "unit": "s"},
        {"key": "manual_cooldown_seconds", "label": "DM trigger cooldown", "type": "int",
         "min": 30, "max": 3600, "default": 60, "unit": "s",
         "help": "Minimum seconds between authorized manual Room Server announcements."},
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        section = self.config_section
        raw = bot.config.get(section, "room_servers", fallback="")
        self.room_servers = list(dict.fromkeys(
            s.strip() for s in raw.split(",") if s.strip()
        ))
        self.password = bot.config.get(section, "password", fallback="hello")
        self.message = bot.config.get(section, "message", fallback="").strip()
        self.schedule_minute = bot.config.getint(section, "schedule_minute", fallback=15)
        self.login_timeout = bot.config.getint(section, "login_timeout_seconds", fallback=60)
        self.ack_timeout = bot.config.getint(section, "ack_timeout_seconds", fallback=25)
        self.manual_cooldown = bot.config.getint(section, "manual_cooldown_seconds", fallback=60)
        self._publish_lock = asyncio.Lock()
        self._last_manual_attempt: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._claimed: set[tuple[str, str]] = set()

        if not self.room_servers:
            raise ValueError("[Room_Announcements] requires room_servers")
        if not self.message:
            raise ValueError("Room announcement message is required")
        if any(ch in self.message for ch in "\r\n"):
            raise ValueError("Room announcement must be a single line")
        if len(self.message.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError("Room announcement template is too long")
        self._stats_fields = _template_fields(self.message)
        if not self._stats_fields and len(self.message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Room announcement must contain 1-150 UTF-8 bytes")
        if not 0 <= self.schedule_minute <= 59:
            raise ValueError("schedule_minute must be between 0 and 59")
        if not 5 <= self.login_timeout <= 180 or not 5 <= self.ack_timeout <= 120:
            raise ValueError("Room Server timeout outside supported range")
        if not 30 <= self.manual_cooldown <= 3600:
            raise ValueError("manual_cooldown_seconds must be between 30 and 3600")

    async def start(self) -> None:
        if self._running:
            return
        if not getattr(self.bot, "meshcore", None):
            self.logger.warning("Room announcements: no radio connection")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="room-announcements")
        self.logger.info(
            "Room announcements enabled: %d server(s), hourly at :%02d",
            len(self.room_servers), self.schedule_minute,
        )

    async def stop(self) -> None:
        self._running = False
        await self._cancel_tasks(self._task)
        self._task = None

    def is_healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def is_target_contact(self, contact: Any) -> bool:
        """Used by DM handler to suppress bot command replies from these rooms."""
        if not _is_room(contact):
            return False
        key = _public_key(contact)
        name = str(contact.get("adv_name", "")).casefold()
        for target in self.room_servers:
            needle = target.casefold()
            if needle == name or needle == key:
                return True
        return False

    def _resolve_contact(self, target: str) -> dict[str, Any] | None:
        mc = self.bot.meshcore
        contacts = getattr(mc, "contacts", {})
        entries = list(contacts.values()) if isinstance(contacts, dict) else list(contacts or [])
        matches = []
        for contact in entries:
            if not self.is_target_contact(contact):
                continue
            # Match *this* target, not just another configured target.
            if (target.casefold() == str(contact.get("adv_name", "")).casefold()
                    or target.casefold() == _public_key(contact)):
                matches.append(contact)
        if len(matches) != 1:
            self.logger.warning(
                "Room announcements: target '%s' matches %d Room Server contacts "
                "(must match exactly one); skipping",
                target, len(matches),
            )
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", _public_key(matches[0])):
            self.logger.warning("Room announcements: '%s' has no complete public key", target)
            return None
        return matches[0]

    def _claim_hour(self, target: str, slot: str) -> bool:
        """Persist before sending to avoid duplicate transmissions after restart.

        A failed/uncertain attempt is *not* retried in the same hourly slot.
        """
        identifier = hashlib.sha256(target.casefold().encode("utf-8")).hexdigest()[:24]
        marker = "room_announcements.last_attempt." + identifier
        if (marker, slot) in self._claimed:
            return False
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            self.logger.error("Room announcements: no database for deduplication; skipping")
            return False
        try:
            if db.get_metadata(marker) == slot:
                self._claimed.add((marker, slot))
                return False
            db.set_metadata(marker, slot)
            self._claimed.add((marker, slot))
            return True
        except Exception as exc:
            self.logger.error("Room announcements: cannot persist send claim: %s", exc)
            return False

    def _load_network_stats(self) -> dict[str, int]:
        """Read one consistent snapshot from the existing contact-tracking table.

        The repeater manager stores first_heard/last_heard as host-local naive
        datetimes. Match those with SQLite localtime, not UTC thresholds.
        """
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            raise RuntimeError("No contact-tracking database")
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       COUNT(CASE WHEN role = 'repeater' THEN 1 END),
                       COUNT(CASE WHEN role = 'companion' THEN 1 END),
                       COUNT(CASE WHEN role = 'repeater'
                                   AND first_heard >= datetime('now','localtime','-7 days')
                                  THEN 1 END),
                       COUNT(CASE WHEN role = 'companion'
                                   AND first_heard >= datetime('now','localtime','-7 days')
                                  THEN 1 END),
                       COUNT(CASE WHEN last_heard >= datetime('now','localtime','-1 day')
                                  THEN 1 END)
                FROM complete_contact_tracking
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("Contact-tracking statistics unavailable")
        return dict(zip(STAT_FIELDS, (int(value or 0) for value in row)))

    async def _render_message(self) -> str | None:
        """Resolve statistics before any login or per-hour send claim.

        Never post placeholders, truncate UTF-8 bytes, or silently substitute
        zeros when the database query fails.
        """
        try:
            fields = (
                await asyncio.to_thread(self._load_network_stats)
                if self._stats_fields else {}
            )
            message = self.message.format_map(fields)
            size = len(message.encode("utf-8"))
            if not 1 <= size <= MAX_MESSAGE_BYTES:
                self.logger.error(
                    "Room announcements: rendered message is %d UTF-8 bytes (limit %d); "
                    "skipping publication", size, MAX_MESSAGE_BYTES,
                )
                return None
            if self._stats_fields:
                self.logger.info(
                    "Room announcements: rendered message (%d bytes): %s", size, message,
                )
            return message
        except Exception:
            self.logger.exception(
                "Room announcements: failed to render network statistics; "
                "skipping publication"
            )
            return None

    async def publish_manual(self) -> str:
        """Publish immediately on an authenticated DM, without waiting for the clock.

        Only the command layer exposes this entry point; it checks the full
        sender public key against Admin_ACL. Manual sends have their own
        cooldown, but a send made during the scheduled minute participates
        in that hour's persisted deduplication.
        """
        if not self.is_healthy() or not self.bot.connected or self.bot.meshcore is None:
            return "Room announcement unavailable: service or radio offline."
        if self._publish_lock.locked():
            return "Room announcement already in progress."

        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._last_manual_attempt is not None:
            remaining = self.manual_cooldown - (now - self._last_manual_attempt)
            if remaining > 0:
                return f"Room announcement cooldown: {int(remaining) + 1}s remaining."

        async with self._publish_lock:
            if not self.is_healthy() or not self.bot.connected:
                return "Room announcement unavailable: service or radio offline."
            self._last_manual_attempt = loop.time()
            rendered = await self._render_message()
            if rendered is None:
                return "Room announcement skipped: stats unavailable or message too long."

            clock = self.bot.scheduler.get_current_time()
            slot = clock.strftime("%Y-%m-%dT%H%z") if clock.minute == self.schedule_minute else None
            acked = unconfirmed = failed = skipped = 0
            seen: set[str] = set()
            for target in self.room_servers:
                if not self._running or not self.bot.connected:
                    failed += 1
                    continue
                contact = self._resolve_contact(target)
                if contact is None:
                    failed += 1
                    continue
                key = _public_key(contact)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                # Avoid a second post when invoked during the scheduled minute.
                if slot is not None and not self._claim_hour(key, slot):
                    skipped += 1
                    continue
                result = await self._publish(contact, rendered)
                if result is True:
                    acked += 1
                elif result is False:
                    unconfirmed += 1
                else:
                    failed += 1

            self.logger.info(
                "Room announcements: manual trigger completed: %d ACK, "
                "%d unconfirmed, %d failed, %d skipped",
                acked, unconfirmed, failed, skipped,
            )
            return (
                f"Room announcement: {acked} ACK, {unconfirmed} unconfirmed, "
                f"{failed} failed, {skipped} skipped."
            )

    async def _run(self) -> None:
        last_render_slot: str | None = None
        rendered_message: str | None = None
        while self._running:
            try:
                now = self.bot.scheduler.get_current_time()
                if (now.minute == self.schedule_minute and self.bot.connected
                        and self.bot.meshcore is not None):
                    # Include UTC offset to distinguish the repeated DST fall-back hour.
                    slot = now.strftime("%Y-%m-%dT%H%z")
                    async with self._publish_lock:
                        # Manual publication may have held the lock past this hour.
                        current_slot = self.bot.scheduler.get_current_time().strftime("%Y-%m-%dT%H%z")
                        if current_slot == slot:
                            if slot != last_render_slot:
                                rendered_message = await self._render_message()
                                last_render_slot = slot
                            if rendered_message is not None:
                                for target in self.room_servers:
                                    if not self._running or not self.bot.connected:
                                        break
                                    contact = self._resolve_contact(target)
                                    if contact is None:
                                        continue
                                    # Canonicalize to key: name/key duplicates send once.
                                    if not self._claim_hour(_public_key(contact), slot):
                                        continue
                                    await self._publish(contact, rendered_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Room announcements: scheduler iteration failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _publish(self, contact: dict[str, Any], message: str) -> bool | None:
        """Login, send once, then logout. Receive-side commands are never handled."""
        mc = self.bot.meshcore
        prefix = _public_key(contact)[:12]
        name = str(contact.get("adv_name", prefix))
        loop = asyncio.get_running_loop()
        login_result: asyncio.Future[bool] = loop.create_future()

        def on_login(event: Any) -> None:
            # Filter by exact 6-byte MeshCore login-response pubkey prefix.
            attributes = getattr(event, "attributes", {}) or {}
            received = attributes.get("pubkey_prefix")
            if received is None:
                received = (getattr(event, "payload", {}) or {}).get("pubkey_prefix")
            if str(received).lower() != prefix:
                return
            if not login_result.done():
                login_result.set_result(event.type == EventType.LOGIN_SUCCESS)

        subscriptions = [
            mc.subscribe(EventType.LOGIN_SUCCESS, on_login),
            mc.subscribe(EventType.LOGIN_FAILED, on_login),
        ]
        logged_in = False
        try:
            result = await mc.commands.send_login(contact, self.password)
            if result is None or result.type == EventType.ERROR:
                self.logger.warning("Room announcements: login request to '%s' failed", name)
                return None
            try:
                logged_in = await asyncio.wait_for(login_result, timeout=self.login_timeout)
            except asyncio.TimeoutError:
                self.logger.warning("Room announcements: login timed out for '%s'", name)
                return None
            if not logged_in:
                self.logger.warning("Room announcements: login denied by '%s'", name)
                return None

            # Exactly one RF send: retry helper is used only for ACK correlation.
            # Without an ACK, delivery is UNKNOWN and we deliberately do not resend.
            ack = await mc.commands.send_msg_with_retry(
                contact, message, max_attempts=1, max_flood_attempts=1,
                timeout=self.ack_timeout, min_timeout=self.ack_timeout,
            )
            if ack is not None and ack.type != EventType.ERROR:
                self.logger.info("Room announcements: '%s' ACK received", name)
                return True
            else:
                self.logger.warning(
                    "Room announcements: '%s' posted once without ACK; "
                    "delivery unconfirmed; no automatic retry", name
                )
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Room announcements: publishing to '%s' failed", name)
            return None
        finally:
            for sub in subscriptions:
                try:
                    sub.unsubscribe()
                except Exception:
                    self.logger.debug("Room announcements: unsubscribe failed", exc_info=True)
            if logged_in:
                try:
                    await mc.commands.send_logout(contact)
                except Exception:
                    self.logger.warning("Room announcements: logout failed for '%s'", name)
