"""AutoTrace user-side recommendations: unit tests use SQLite and never a radio."""
import asyncio
import configparser
from contextlib import contextmanager
from datetime import datetime, timezone
import time
from unittest.mock import AsyncMock, MagicMock, Mock

from local.commands.autotrace import AutotraceCommand, candidate_route, parse_route
from tests.conftest import mock_message

SOURCE = "a" * 64
TARGET = "abcdef" + "1" * 58
REP = "bbccdd" + "2" * 58
OTHER = "ddffee" + "3" * 58


class DB:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            yield conn


def make_bot(tmp_path):
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    for section in ("Bot", "Channels", "Keywords", "Autotrace_Command",
                    "Stats_Command", "Admin_ACL"):
        bot.config.add_section(section)
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.set("Autotrace_Command", "enabled", "true")
    bot.config.set("Autotrace_Command", "per_user_cooldown_seconds", "900")
    bot.config.set("Autotrace_Command", "global_cooldown_seconds", "60")
    bot.config.set("Stats_Command", "anonymize_users", "false")
    bot.translator.translate = Mock(side_effect=lambda name, **kw: name)
    bot.command_manager.monitor_channels = ["general"]
    bot.db_manager = DB(str(tmp_path / "test.db"))
    with bot.db_manager.connection() as conn:
        conn.execute("""CREATE TABLE message_stats(
            id INTEGER PRIMARY KEY, sender_id TEXT, is_dm INTEGER,
            path TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE complete_contact_tracking(
            name TEXT, public_key TEXT, role TEXT)""")
    return bot


def contact(bot, name, pubkey, role="companion"):
    with bot.db_manager.connection() as conn:
        conn.execute("INSERT INTO complete_contact_tracking VALUES (?,?,?)",
                     (name, pubkey, role))


def history(bot, sender, route, age=0, is_dm=True):
    stamp = datetime.fromtimestamp(time.time()-age, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with bot.db_manager.connection() as conn:
        conn.execute(
            "INSERT INTO message_stats(sender_id,is_dm,path,created_at) VALUES(?,?,?,?)",
            (sender, int(is_dm), route, stamp)
        )


def incoming(text="!autotrace Dee", label="Alice", key=SOURCE, path="11,22,33"):
    msg = mock_message(content=text, sender_id=label, is_dm=True, path=path)
    msg.sender_pubkey = key
    return msg


def run_cmd(cmd, msg):
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    text = cmd.send_response.call_args.args[1]
    assert len(text.encode("utf-8")) <= 158
    return text


def test_strict_route_parser():
    assert parse_route("11,22,33 (3 hops)") == ("11", "22", "33")
    assert parse_route("1122,3344") == ("1122", "3344")
    assert parse_route("112233,445566") is None
    assert parse_route("11,2233") is None
    assert parse_route("11,22,33,44,55,66") is None


def test_join_only_at_observed_common_suffix():
    assert candidate_route(("11", "22", "33"), ("aa", "22", "33")) == ("11", "22", "aa")
    assert candidate_route(("11", "22"), ("aa", "bb")) is None
    assert candidate_route(("11",), ("1122",)) is None
    assert candidate_route(("11", "22", "33"), ("44", "55", "33"), 3) is None


def test_named_target_returns_user_side_route_without_probes(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    history(bot, "Alice", "11,22,33")
    history(bot, "Dee", "aa,22,33")
    bot.meshcore.commands.send_trace = AsyncMock()
    cmd = AutotraceCommand(bot)
    text = run_cmd(cmd, incoming())
    assert "tracer 11,22,aa" in text
    assert "candidate" in text
    bot.meshcore.commands.send_trace.assert_not_awaited()


def test_six_hex_target_and_paths_are_read_only(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    history(bot, "Dee", "aa,22,33")
    cmd = AutotraceCommand(bot)
    out = run_cmd(cmd, incoming("!autotrace ABCDEF paths"))
    assert "11,22,aa" in out
    assert cmd._last_user == {}
    assert cmd._last_global == float("-inf")


def test_no_shared_repeater_means_no_candidate(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    history(bot, "Dee", "aa,bb,cc")
    out = run_cmd(AutotraceCommand(bot), incoming())
    assert "no shared route" in out


def test_prefix_collision_never_chooses_target(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    contact(bot, "Other", "abcdef" + "3" * 58)
    out = run_cmd(AutotraceCommand(bot), incoming("!autotrace ABCDEF"))
    assert "ambiguous" in out


def test_repeater_target_uses_recorded_route_suffix(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Relay", REP, "repeater")
    contact(bot, "OtherRelay", OTHER, "repeater")
    history(bot, "Other", "bb,22,33")
    out = run_cmd(AutotraceCommand(bot), incoming("!autotrace bbccdd"))
    assert "tracer 11,22,bb" in out


def test_repeater_hash_collision_refuses_identity(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Relay", REP, "repeater")
    contact(bot, "Collision", "bbdddd" + "3" * 58, "repeater")
    history(bot, "Other", "bb,22,33")
    out = run_cmd(AutotraceCommand(bot), incoming("!autotrace bbccdd"))
    assert "no observed target route" in out


def test_recommendation_cooldown_rejects_second_attempt(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    history(bot, "Dee", "aa,22,33")
    cmd = AutotraceCommand(bot)
    assert "tracer" in run_cmd(cmd, incoming())
    assert "cooldown" in run_cmd(cmd, incoming())
    # Viewing candidates remains possible during cooldown.
    assert "11,22,aa" in run_cmd(cmd, incoming("!autotrace Dee paths"))


def test_unverified_history_label_never_invents_route(tmp_path):
    bot = make_bot(tmp_path)
    contact(bot, "Alice", SOURCE)
    contact(bot, "Dee", TARGET)
    contact(bot, "Dee", "123456" + "2" * 58)
    history(bot, "Dee", "aa,22,33")
    assert "ambiguous" in run_cmd(AutotraceCommand(bot), incoming("!autotrace Dee"))
