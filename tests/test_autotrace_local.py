"""Tests for local/commands/autotrace.py. No RF required."""
import asyncio
import configparser
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
import sqlite3
import tempfile
import time
from pathlib import Path

from local.commands.autotrace import AutotraceCommand, parse_route
from modules.trace_runner import RunTraceResult
from tests.conftest import mock_message


KEY_A = "a" * 64
KEY_B = "b" * 64


class DB:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
        finally:
            conn.close()


def make_bot(tmp_path):
    from unittest.mock import MagicMock
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    for section in ("Bot", "Channels", "Keywords", "Trace_Command",
                    "Stats_Command", "Autotrace_Command", "Admin_ACL"):
        config.add_section(section)
    config.set("Channels", "monitor_channels", "#test")
    config.set("Channels", "respond_to_dms", "true")
    config.set("Bot", "bot_name", "Bot")
    config.set("Trace_Command", "enabled", "true")
    config.set("Trace_Command", "maximum_hops", "5")
    config.set("Trace_Command", "trace_retry_count", "3")
    config.set("Autotrace_Command", "enabled", "true")
    config.set("Autotrace_Command", "per_user_cooldown_seconds", "900")
    config.set("Autotrace_Command", "global_cooldown_seconds", "60")
    config.set("Stats_Command", "anonymize_users", "false")
    config.set("Admin_ACL", "admin_pubkeys", KEY_B)
    bot.config = config
    bot.connected = True
    bot.meshcore.commands.send_trace = AsyncMock()
    bot.translator.translate = Mock(side_effect=lambda name, **kw: name)
    bot.command_manager.monitor_channels = ["#test"]
    bot.db_manager = DB(str(tmp_path / "history.db"))
    with bot.db_manager.connection() as db:
        db.execute("""CREATE TABLE message_stats (
            id INTEGER PRIMARY KEY, sender_id TEXT, is_dm INTEGER,
            path TEXT, created_at TEXT)""")
        db.commit()
    return bot


def add_route(bot, route, sender="Alice", age=0):
    ts = datetime.fromtimestamp(time.time() - age, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with bot.db_manager.connection() as db:
        db.execute(
            "INSERT INTO message_stats(sender_id,is_dm,path,created_at) VALUES(?,1,?,?)",
            (sender, route, ts)
        )
        db.commit()


def message(sender="Alice", key=KEY_A, text="!autotrace"):
    msg = mock_message(content=text, sender_id=sender, is_dm=True)
    msg.sender_pubkey = key
    return msg


def test_parser_rejects_truncated_mixed_3_byte_and_overlimit():
    assert parse_route("01,7a (2 hops)") == ("01", "7a")
    assert parse_route("abcd,ef01") == ("abcd", "ef01")
    assert parse_route("abcdef,123456") is None
    assert parse_route("01,abcd") is None
    assert parse_route("01,02,03,04,05,06") is None
    assert parse_route("Routed through 2 hops") is None


def test_read_only_paths_works_through_rf_cooldown(tmp_path):
    bot = make_bot(tmp_path)
    add_route(bot, "01,7a (2 hops)")
    add_route(bot, "22,33 (2 hops)", age=30)
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd._last_global = time.monotonic()
    cmd._last_by_user[KEY_A] = time.monotonic()
    with patch("local.commands.autotrace.run_trace", new_callable=AsyncMock) as run:
        assert asyncio.run(cmd.execute(message(text="!autotrace paths")))
        run.assert_not_awaited()
    assert "01,7a" in cmd.send_response.call_args.args[1]


def test_historical_paths_use_receive_time_and_only_asking_label(tmp_path):
    bot = make_bot(tmp_path)
    add_route(bot, "01", age=25 * 3600)
    add_route(bot, "22", sender="SomeoneElse")
    add_route(bot, "aa,bb", age=5)
    cmd = AutotraceCommand(bot)
    paths = cmd._routes(message())
    assert [r[0] for r in paths] == [("aa", "bb")]


def test_single_rf_attempt_and_cooldown_even_when_failed(tmp_path):
    bot = make_bot(tmp_path)
    add_route(bot, "01,7a")
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    fake_result = RunTraceResult(success=False, tag=1, error_message="timeout")
    with patch("local.commands.autotrace.run_trace", new=AsyncMock(return_value=fake_result)) as runner:
        asyncio.run(cmd.execute(message()))
        runner.assert_awaited_once()
        assert runner.await_args.kwargs["max_attempts_override"] == 1
        assert runner.await_args.kwargs["path"] == ["7a", "01", "7a"]
        runner.reset_mock()
        asyncio.run(cmd.execute(message()))
        runner.assert_not_awaited()
        assert "cooldown" in cmd.send_response.call_args.args[1].lower()


def test_global_cooldown_cannot_be_bypassed_by_admin(tmp_path):
    bot = make_bot(tmp_path)
    add_route(bot, "01,7a", sender="Admin")
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd._last_global = time.monotonic()
    with patch("local.commands.autotrace.run_trace", new_callable=AsyncMock) as runner:
        asyncio.run(cmd.execute(message(sender="Admin", key=KEY_B)))
        runner.assert_not_awaited()
    assert "global cooldown" in cmd.send_response.call_args.args[1].lower()


def test_stale_or_missing_sender_key_never_transmits(tmp_path):
    bot = make_bot(tmp_path)
    add_route(bot, "01,7a")
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    with patch("local.commands.autotrace.run_trace", new_callable=AsyncMock) as runner:
        asyncio.run(cmd.execute(message(key=None)))
        runner.assert_not_awaited()
