"""Unit tests for the opt-in, announcement-only Room Server publisher."""

import asyncio
import configparser
import logging
from types import SimpleNamespace

import pytest
from meshcore.events import EventType

from modules.service_plugins.room_announcements_service import (
    RoomAnnouncementsService,
)


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


def make_service(mesh=None, *, message=MESSAGE, room_servers="Hamilton Room"):
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
        db_manager=FakeDb(),
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
    with pytest.raises(ValueError, match="room_servers"):
        make_service(room_servers="")
    with pytest.raises(ValueError, match="150"):
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
    asyncio.run(service._publish(CONTACT))
    assert mesh.logins == 1
    assert mesh.logouts == 1
    assert len(mesh.sent) == 1
    assert mesh.sent[0][2]["max_attempts"] == 1
    assert mesh.subscriptions == []


def test_failed_room_login_never_posts():
    mesh = FakeMesh(login_event=EventType.LOGIN_FAILED)
    service = make_service(mesh)
    asyncio.run(service._publish(CONTACT))
    assert mesh.logins == 1
    assert mesh.logouts == 0
    assert mesh.sent == []
    assert mesh.subscriptions == []


def test_no_ack_does_not_retry():
    mesh = FakeMesh()
    mesh.acked = False
    service = make_service(mesh)
    asyncio.run(service._publish(CONTACT))
    assert len(mesh.sent) == 1
    assert mesh.logouts == 1
