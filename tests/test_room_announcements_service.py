"""Unit tests for the opt-in, announcement-only Room Server publisher."""

import asyncio
import configparser
import logging
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase

from meshcore.events import EventType
from modules.commands.roomannounce_command import RoomAnnounceCommand
from modules.models import MeshMessage
from modules.service_plugins.room_announcements_service import RoomAnnouncementsService


KEY = "a1" * 32
CONTACT = {"public_key": KEY, "adv_name": "Hamilton Room", "type": 3}
MESSAGE = "Hamilton users: Join #public-1"


class FakeSubscription:
    def __init__(self, mesh, event_type, callback):
        self.mesh = mesh
        self.event_type = event_type
        self.callback = callback

    def unsubscribe(self):
        self.mesh.subscriptions.remove(self)


class FakeMesh:
    def __init__(self, login_event=EventType.LOGIN_SUCCESS):
        self.contacts = {KEY: CONTACT}
        self.subscriptions = []
        self.commands = self
        self.login_event = login_event
        self.sent = []
        self.logins = 0
        self.logouts = 0
        self.acked = True

    def subscribe(self, event_type, callback):
        sub = FakeSubscription(self, event_type, callback)
        self.subscriptions.append(sub)
        return sub

    def emit(self, event_type):
        event = SimpleNamespace(
            type=event_type,
            payload={"pubkey_prefix": KEY[:12]},
            attributes={"pubkey_prefix": KEY[:12]},
        )
        for sub in list(self.subscriptions):
            if sub.event_type == event_type:
                sub.callback(event)

    async def send_login(self, contact, password):
        self.logins += 1
        assert contact == CONTACT
        assert password == "hello"
        self.emit(self.login_event)
        return SimpleNamespace(type=EventType.MSG_SENT)

    async def send_msg_with_retry(self, contact, message, **kwargs):
        self.sent.append((contact, message, kwargs))
        return SimpleNamespace(type=EventType.MSG_SENT) if self.acked else None

    async def send_logout(self, contact):
        self.logouts += 1
        return SimpleNamespace(type=EventType.OK)


class FakeDb:
    def __init__(self):
        self.data = {}

    def get_metadata(self, name):
        return self.data.get(name)

    def set_metadata(self, name, value):
        self.data[name] = value


def make_service(mesh=None, *, message=MESSAGE, room_servers="Hamilton Room", db=None):
    config = configparser.ConfigParser()
    config["Room_Announcements"] = {
        "enabled": "true",
        "room_servers": room_servers,
        "password": "hello",
        "message": message,
        "schedule_minute": "15",
        "login_timeout_seconds": "5",
        "ack_timeout_seconds": "5",
    }
    bot = SimpleNamespace(
        config=config,
        logger=logging.getLogger("test-room-announcements"),
        meshcore=mesh or FakeMesh(),
        db_manager=db if db is not None else FakeDb(),
        connected=True,
        scheduler=SimpleNamespace(get_current_time=lambda: None),
    )
    return RoomAnnouncementsService(bot)


def test_room_only_exact_match_and_type():
    service = make_service()
    assert service.is_target_contact(CONTACT)
    assert not service.is_target_contact({**CONTACT, "type": 1})
    assert not service.is_target_contact({**CONTACT, "adv_name": "Hamilton Room extra"})
    assert service._resolve_contact("Hamilton Room") == CONTACT
    assert service._resolve_contact("Some Room") is None
    assert service._resolve_contact(KEY) == CONTACT  # full key also identifies the room


def test_empty_servers_and_large_messages_rejected():
    with TestCase().assertRaisesRegex(ValueError, "room_servers"):
        make_service(room_servers="")
    with TestCase().assertRaisesRegex(ValueError, "150"):
        make_service(message="é" * 76)


def test_hour_claim_survives_new_service_instance():
    service = make_service()
    assert service._claim_hour("Hamilton Room", "2026-11-22T16-0500")
    assert not service._claim_hour("Hamilton Room", "2026-11-22T16-0500")
    other = make_service()
    other.bot.db_manager = service.bot.db_manager
    assert not other._claim_hour("Hamilton Room", "2026-11-22T16-0500")
    assert other._claim_hour("Hamilton Room", "2026-11-22T17-0500")


def test_publish_requires_successful_login_and_only_sends_once():
    mesh = FakeMesh()
    service = make_service(mesh)
    asyncio.run(service._publish(CONTACT, service.message))
    assert mesh.logins == 1
    assert mesh.logouts == 1
    assert len(mesh.sent) == 1
    assert mesh.sent[0][2]["max_attempts"] == 1
    assert mesh.subscriptions == []


def test_failed_room_login_never_posts():
    mesh = FakeMesh(login_event=EventType.LOGIN_FAILED)
    service = make_service(mesh)
    asyncio.run(service._publish(CONTACT, service.message))
    assert mesh.logins == 1
    assert mesh.logouts == 0
    assert mesh.sent == []
    assert mesh.subscriptions == []


def test_no_ack_does_not_retry():
    mesh = FakeMesh()
    mesh.acked = False
    service = make_service(mesh)
    asyncio.run(service._publish(CONTACT, service.message))
    assert len(mesh.sent) == 1
    assert mesh.logouts == 1


class FakeStatsDb(FakeDb):
    def __init__(self, *, create_table=True):
        super().__init__()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        if create_table:
            self.conn.execute(
                "CREATE TABLE complete_contact_tracking "
                "(role TEXT, first_heard TEXT, last_heard TEXT)"
            )

    def connection(self):
        return nullcontext(self.conn)


STATS_TEMPLATE = (
    "Network: {total_contacts} total ({total_repeaters} repeaters, "
    "{total_companions} companions). {new_repeaters_7d} new repeaters, "
    "{new_companions_7d} new companions in last 7d. "
    "{recent_activity_24h} active in 24h."
)


def test_network_stats_are_rendered_from_contact_history():
    db = FakeStatsDb()
    now = datetime.now()
    rows = [
        ("repeater", now, now),
        ("repeater", now - timedelta(days=10), now - timedelta(days=3)),
        ("companion", now - timedelta(days=1), now),
        ("companion", now - timedelta(days=20), now - timedelta(days=2)),
        ("roomserver", now - timedelta(days=2), now),
    ]
    db.conn.executemany(
        "INSERT INTO complete_contact_tracking VALUES (?, ?, ?)",
        [(role, first.isoformat(" "), last.isoformat(" ")) for role, first, last in rows],
    )
    service = make_service(message=STATS_TEMPLATE, db=db)
    assert len(STATS_TEMPLATE.encode("utf-8")) > 150

    rendered = asyncio.run(service._render_message())
    assert rendered == (
        "Network: 5 total (2 repeaters, 2 companions). 1 new repeaters, "
        "1 new companions in last 7d. 3 active in 24h."
    )
    assert len(rendered.encode("utf-8")) <= 150

    asyncio.run(service._publish(CONTACT, rendered))
    assert len(service.bot.meshcore.sent) == 1
    assert service.bot.meshcore.sent[0][1] == rendered
    db.conn.close()


def test_dynamic_render_rejects_oversized_utf8_payload():
    db = FakeStatsDb()
    service = make_service(message="{total_contacts} " + "é" * 75, db=db)
    assert asyncio.run(service._render_message()) is None
    assert not service._claimed
    assert service.bot.meshcore.sent == []
    db.conn.close()


def test_network_stats_unavailable_skips_instead_of_reporting_zeros():
    db = FakeStatsDb(create_table=False)
    service = make_service(message="Nodes: {total_contacts}", db=db)
    assert asyncio.run(service._render_message()) is None
    assert service.bot.meshcore.sent == []
    db.conn.close()


def test_unknown_and_unsafe_placeholder_rejected_at_startup():
    for template in (
        "{not_a_stat}",
        "{total_contacts.__class__}",
        "{total_contacts[0]}",
        "{total_contacts!r}",
        "{total_contacts:>6}",
        "Broken {total_contacts",
    ):
        with TestCase().assertRaisesRegex(ValueError, "template"):
            make_service(message=template)


def test_static_announcement_still_works_without_stats_db():
    service = make_service(message="Hamilton users: Join #public-1")
    assert asyncio.run(service._render_message()) == "Hamilton users: Join #public-1"


async def _active_service(service, *, current_minute=12):
    """Mark a service healthy without letting its scheduling loop transmit."""
    service.bot.scheduler.get_current_time = lambda: datetime(2026, 11, 22, 19, current_minute)
    service._running = True
    service._task = asyncio.create_task(asyncio.sleep(300))
    return service


def test_manual_trigger_sends_immediately_and_enforces_cooldown():
    async def scenario():
        db = FakeStatsDb()
        db.conn.execute(
            "INSERT INTO complete_contact_tracking VALUES (?, ?, ?)",
            ("repeater", "2026-11-22 15:00:00", "2026-11-22 18:00:00"),
        )
        service = await _active_service(make_service(message="R:{total_repeaters}", db=db))
        try:
            assert await service.publish_manual() == (
                "Room announcement: 1 ACK, 0 unconfirmed, 0 failed, 0 skipped."
            )
            assert service.bot.meshcore.sent[0][1] == "R:1"
            assert await service.publish_manual() == "Room announcement cooldown: 60s remaining."
            assert len(service.bot.meshcore.sent) == 1
            # Outside the schedule window, manual messages never claim scheduled hours.
            assert service._claimed == set()
        finally:
            await service.stop()
            db.conn.close()

    asyncio.run(scenario())


def test_manual_trigger_during_schedule_window_claims_hour_once():
    async def scenario():
        service = await _active_service(make_service(), current_minute=15)
        try:
            assert (await service.publish_manual()).startswith("Room announcement: 1 ACK")
            slot = "2026-11-22T19"
            # Slot in the fake clock is naive (no UTC offset).
            assert not service._claim_hour(KEY, slot)
            assert len(service.bot.meshcore.sent) == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_manual_trigger_reports_unconfirmed_delivery():
    async def scenario():
        mesh = FakeMesh()
        mesh.acked = False
        service = await _active_service(make_service(mesh=mesh))
        try:
            assert await service.publish_manual() == (
                "Room announcement: 0 ACK, 1 unconfirmed, 0 failed, 0 skipped."
            )
            assert len(mesh.sent) == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_manual_trigger_rejects_parallel_publishing():
    async def scenario():
        service = await _active_service(make_service())
        try:
            async with service._publish_lock:
                assert await service.publish_manual() == "Room announcement already in progress."
            assert service.bot.meshcore.sent == []
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_manual_trigger_requires_running_service():
    service = make_service()
    assert asyncio.run(service.publish_manual()).startswith("Room announcement unavailable:")


class FakeCommandManager:
    def __init__(self):
        self.replies = []
        self.monitor_channels = []

    async def send_response(self, message, content, **kwargs):
        self.replies.append(content)
        return True


def make_command(*, admin_key=KEY, service=None):
    service = service or SimpleNamespace(
        is_healthy=lambda: True,
        publish_manual=lambda: asyncio.sleep(
            0, result="Room announcement: 1 ACK, 0 unconfirmed, 0 failed, 0 skipped."
        ),
    )
    fake = make_service()
    bot = fake.bot
    bot.config["Admin_ACL"] = {"admin_pubkeys": admin_key}
    bot.config["Bot"] = {"bot_name": "Bot"}
    bot.services = {"roomannouncements": service}
    bot.command_manager = FakeCommandManager()
    return RoomAnnounceCommand(bot)


def test_room_announce_dm_checks_full_admin_key_and_accepts_both_spellings():
    async def scenario():
        cmd = make_command()
        for spelling in ("announce.room", "annouce.room"):
            msg = MeshMessage(content=spelling, is_dm=True, sender_id="operator", sender_pubkey=KEY)
            assert cmd.matches_keyword(msg)
            assert cmd.can_execute(msg)
            assert await cmd.execute(msg)
        assert len(cmd.bot.command_manager.replies) == 2
        assert all(reply.startswith("Room announcement: 1 ACK") for reply in cmd.bot.command_manager.replies)

    asyncio.run(scenario())


def test_room_announce_rejects_unauthorized_and_channel_invocations():
    async def scenario():
        cmd = make_command()
        invalid = [
            MeshMessage(content="announce.room", is_dm=True, sender_id="operator", sender_pubkey="aa"),
            MeshMessage(content="announce.room", is_dm=True, sender_id="operator", sender_pubkey="bb" * 32),
            MeshMessage(content="announce.room", is_dm=True, sender_id="operator", sender_pubkey=None),
            MeshMessage(content="announce.room", is_dm=False, sender_id="operator", sender_pubkey=KEY,
                        channel="Public"),
        ]
        for msg in invalid:
            assert not cmd.can_execute(msg)
            assert not await cmd.execute(msg)
        assert cmd.bot.command_manager.replies == []

    asyncio.run(scenario())


def test_room_announce_rejects_unconfigured_admin_key_and_arguments():
    async def scenario():
        cmd = make_command(admin_key="")
        denied = MeshMessage(content="announce.room", is_dm=True, sender_id="operator", sender_pubkey=KEY)
        assert not cmd.can_execute(denied)

        cmd = make_command()
        extra = MeshMessage(content="announce.room extra", is_dm=True, sender_id="operator", sender_pubkey=KEY)
        assert await cmd.execute(extra)
        assert cmd.bot.command_manager.replies == ["Usage: announce.room (DM only; no arguments)"]

    asyncio.run(scenario())
