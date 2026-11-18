"""Tests for observed_paths-backed, user-originated AutoTrace proposals."""
import asyncio
import configparser
from contextlib import contextmanager
from datetime import datetime, timezone
import time
from unittest.mock import AsyncMock, MagicMock, Mock

from local.commands.autotrace import AutotraceCommand, candidate_route, parse_route
from tests.conftest import mock_message

USER = "a1a1a1" + "0" * 58
DEE = "d13584" + "0" * 58
RELAY_11 = "11aabb" + "0" * 58
RELAY_22 = "22aabb" + "0" * 58
RELAY_33 = "33aabb" + "0" * 58
RELAY_AA = "aaaabb" + "0" * 58


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
    config = configparser.ConfigParser()
    for section in ("Bot", "Channels", "Keywords", "Autotrace_Command",
                    "Stats_Command", "Admin_ACL"):
        config.add_section(section)
    config.set("Bot", "bot_name", "TestBot")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.set("Autotrace_Command", "enabled", "true")
    config.set("Autotrace_Command", "per_user_cooldown_seconds", "900")
    config.set("Autotrace_Command", "global_cooldown_seconds", "60")
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager.monitor_channels = ["general"]
    bot.meshcore.commands.send_trace = AsyncMock()
    bot.db_manager = DB(str(tmp_path / "db.sqlite"))
    with bot.db_manager.connection() as conn:
        conn.execute("""CREATE TABLE complete_contact_tracking(
            name TEXT, public_key TEXT, role TEXT)""")
        conn.execute("""CREATE TABLE observed_paths(
            id INTEGER PRIMARY KEY, public_key TEXT, packet_type TEXT,
            path_hex TEXT, bytes_per_hop INTEGER,
            observation_count INTEGER, last_seen TEXT)""")
        conn.execute("""CREATE TABLE mesh_connections(
            from_prefix TEXT, to_prefix TEXT,
            from_public_key TEXT, to_public_key TEXT,
            observation_count INTEGER, last_seen TEXT)""")
    for name, key, role in [
        ("Alice", USER, "companion"), ("Dee", DEE, "companion"),
        ("R11", RELAY_11, "repeater"), ("R22", RELAY_22, "repeater"),
        ("R33", RELAY_33, "repeater"), ("RAA", RELAY_AA, "repeater")
    ]:
        contact(bot, name, key, role)
    return bot


def contact(bot, name, key, role):
    with bot.db_manager.connection() as conn:
        conn.execute("INSERT INTO complete_contact_tracking VALUES(?,?,?)",
                     (name, key, role))


def advert(bot, key, hashes, width=1, age=0, ptype="advert"):
    stamp = datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat()
    with bot.db_manager.connection() as conn:
        conn.execute("""INSERT INTO observed_paths
            (public_key,packet_type,path_hex,bytes_per_hop,observation_count,last_seen)
            VALUES(?,?,?,?,?,?)""", (key, ptype, hashes, width, 2, stamp))


def graph_edge(bot, source, dest, age=0, count=2, source_key=None, dest_key=None):
    stamp = datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat()
    with bot.db_manager.connection() as conn:
        conn.execute("""INSERT INTO mesh_connections
            (from_prefix,to_prefix,from_public_key,to_public_key,
             observation_count,last_seen) VALUES(?,?,?,?,?,?)""",
            (source, dest, source_key, dest_key, count, stamp))


def user(text="!autotrace Dee"):
    msg = mock_message(content=text, sender_id="Alice", is_dm=True)
    msg.sender_pubkey = USER
    return msg


def send(cmd, text):
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(user(text)))
    result = cmd.send_response.call_args.args[1]
    assert len(result.encode()) <= 158
    return result


def test_decode_observed_three_byte_sample_and_empty_path(tmp_path):
    cmd = AutotraceCommand(make_bot(tmp_path))
    assert cmd._decode_observed("b1b3e6a53e96bcadb8", 3) == (
        "b1b3e6", "a53e96", "bcadb8"
    )
    assert cmd._decode_observed("", 1) is None
    assert cmd._decode_observed("b1b3e6a53e96bcadb8", 1) is not None
    assert cmd._decode_observed("b1b3e6a53e96bcadb8", 4) is None


def test_adverts_are_bound_to_source_key_and_no_probe(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa2233")
    cmd = AutotraceCommand(bot)
    result = send(cmd, "!autotrace Dee")
    assert "tracer 11,22,aa" in result
    assert "candidate" in result
    bot.meshcore.commands.send_trace.assert_not_awaited()


def test_multibyte_and_one_byte_adverts_join_by_full_repeater_keys(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "11aabb22aabb33aabb", width=3)
    advert(bot, DEE, "aa2233", width=1)
    out = send(AutotraceCommand(bot), "!autotrace D13584")
    assert "tracer 11,22,aa" in out


def test_repeater_target_includes_repeater_itself(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, RELAY_AA, "2233")
    assert "tracer 11,22,aa" in send(AutotraceCommand(bot), "!autotrace RAA")


def test_no_shared_repeater_path_is_rejected(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa")
    assert "no recent user-to-repeater link" in send(AutotraceCommand(bot), "!autotrace Dee")


def test_empty_advert_does_not_make_a_route(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "")
    assert "no target advert route" in send(AutotraceCommand(bot), "!autotrace Dee")


def test_prefix_collision_is_rejected(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa2233")
    contact(bot, "Dee2", "d13584" + "1" * 58, "companion")
    assert "ambiguous" in send(AutotraceCommand(bot), "!autotrace d13584")


def test_short_repeater_hash_collision_is_rejected(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa2233")
    contact(bot, "Another-22", "22ffff" + "0" * 58, "repeater")
    assert "no recent user-to-repeater link" in send(AutotraceCommand(bot), "!autotrace Dee")


def test_cooldown_only_for_recommendation_not_paths(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa2233")
    cmd = AutotraceCommand(bot)
    assert "tracer" in send(cmd, "!autotrace Dee")
    assert "cooldown" in send(cmd, "!autotrace Dee")
    assert "11,22,aa" in send(cmd, "!autotrace Dee paths")


def test_no_advert_by_requester_fails_without_current_dm_route(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    assert "no recent user-to-repeater link" in send(AutotraceCommand(bot), "!autotrace Dee")


def test_current_dm_path_works_with_no_requester_advert(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    cmd = AutotraceCommand(bot)
    msg = user("!autotrace Dee")
    msg.routing_info = {"path_nodes": ["11", "22", "33"],
                        "bytes_per_hop": 1, "path_length": 3}
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]
    bot.meshcore.commands.send_trace.assert_not_awaited()


def test_current_dm_three_byte_nodes_joins_target_one_byte(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.routing_info = {
        "path_nodes": ["11aabb", "22aabb", "33aabb"],
        "bytes_per_hop": 3, "path_length": 3,
    }
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]


def test_current_dm_hex_fallback_with_explicit_width(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.routing_info = {
        "path_hex": "11aabb22aabb33aabb",
        "bytes_per_hop": 3, "path_length": 3,
    }
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]


def test_legacy_message_path_fallback(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.path = "11,22,33 (3 hops via ROUTE_TYPE_FLOOD)"
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]


def test_direct_dm_is_not_reported_as_missing_or_used_as_repeater(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    advert(bot, USER, "112233")  # Old advert must not supersede a direct DM
    msg = user("!autotrace Dee")
    msg.routing_info = {"path_nodes": [], "path_length": 0}
    msg.path = "Direct"
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "no recent user-to-repeater link" in cmd.send_response.call_args.args[1]
    bot.meshcore.commands.send_trace.assert_not_awaited()


def test_unknown_direct_display_is_not_proof_of_direct_rf(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.path = "Direct (0 hops)"
    msg.routing_info = None  # upstream path_len=255 can mean unknown
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "no recent user-to-repeater link" in cmd.send_response.call_args.args[1]


def test_actual_routing_nodes_override_incorrect_zero_length(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.routing_info = {"path_nodes": ["11", "22", "33"], "path_length": 0}
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]


def test_missing_dm_metadata_can_fallback_to_adverts(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, USER, "112233")
    advert(bot, DEE, "aa2233")
    msg = user("!autotrace Dee")
    msg.routing_info = None
    msg.path = "Direct (0 hops)"
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    assert "tracer 11,22,aa" in cmd.send_response.call_args.args[1]


def test_direct_dm_graph_fallback_with_historical_first_hop(tmp_path):
    bot = make_bot(tmp_path)
    # Exactly the user's case: no USER advert, incoming direct, graph first
    # hop recorded two days earlier, Dee advert path ends toward the bot.
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "a1a1a1", "22aabb", age=48 * 3600, count=11)
    graph_edge(bot, "22aabb", "aaaabb", age=1200, count=618)
    msg = user("!autotrace Dee")
    msg.routing_info = {"path_length": 0, "path_nodes": []}
    cmd = AutotraceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    asyncio.run(cmd.execute(msg))
    reply = cmd.send_response.call_args.args[1]
    assert "tracer 22,aa" in reply
    assert "candidate" in reply
    bot.meshcore.commands.send_trace.assert_not_awaited()


def test_graph_fallback_requires_source_link_not_just_repeater_path(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "22aabb", "aaaabb", count=800)
    assert "no recent user-to-repeater link" in send(
        AutotraceCommand(bot), "!autotrace Dee"
    )


def test_graph_rejects_old_first_hop(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "a1a1a1", "22aabb", age=96 * 3600, count=50)
    graph_edge(bot, "22aabb", "aaaabb", count=800)
    assert "no recent user-to-repeater link" in send(
        AutotraceCommand(bot), "!autotrace Dee"
    )


def test_graph_refuses_ambiguous_repeater_identity(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "a1a1a1", "22aabb", count=11)
    graph_edge(bot, "22aabb", "aaaabb", count=800)
    contact(bot, "Clashing", "22aabb" + "1" * 58, "repeater")
    assert "no recent user-to-repeater link" in send(
        AutotraceCommand(bot), "!autotrace Dee"
    )


def test_graph_ranks_shorter_routes_and_avoids_cycles(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "a1a1a1", "11aabb", count=11)
    graph_edge(bot, "11aabb", "22aabb", count=8)
    graph_edge(bot, "22aabb", "aaaabb", count=50)
    graph_edge(bot, "11aabb", "aaaabb", count=5)
    assert "tracer 11,aa" in send(AutotraceCommand(bot), "!autotrace Dee")


def test_direct_to_target_repeater_with_zero_hop_advert(tmp_path):
    bot = make_bot(tmp_path)
    graph_edge(bot, "a1a1a1", "aaaabb", count=4)
    # Repeater ADVERT may have empty path_hex (received at bot directly)
    advert(bot, RELAY_AA, "")
    assert "tracer aa" in send(AutotraceCommand(bot), "!autotrace RAA")


def test_graph_reply_cooldown_and_paths_remain_read_only(tmp_path):
    bot = make_bot(tmp_path)
    advert(bot, DEE, "aa2233")
    graph_edge(bot, "a1a1a1", "22aabb", count=11)
    graph_edge(bot, "22aabb", "aaaabb", count=12)
    cmd = AutotraceCommand(bot)
    assert "tracer 22,aa" in send(cmd, "!autotrace Dee")
    assert "cooldown" in send(cmd, "!autotrace Dee")
    assert "22,aa" in send(cmd, "!autotrace Dee paths")
    bot.meshcore.commands.send_trace.assert_not_awaited()
