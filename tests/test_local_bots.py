"""Isolated, passive bot-directory tests. No radio, Internet, or credentials."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
import time
from types import SimpleNamespace
from tests.test_local_diagnostics_extra import BaseLocalTest, get_plugin


class TestBots(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.module = get_plugin("bots")
        self.plugin = self.module.BotsCommand(self.bot)
        self.bot.meshcore = SimpleNamespace(contacts={})
        self.plugin.is_channel_allowed = (
            lambda msg: msg.is_dm or getattr(msg, "channel", None) == "#test"
        )
        self.plugin._cleaned_content_matches = (
            lambda msg, pred: pred(msg.content.strip().removeprefix("!").lower())
        )
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute("""CREATE TABLE complete_contact_tracking (
                public_key TEXT, name TEXT, last_heard TEXT, role TEXT,
                device_type TEXT, advert_count INTEGER, snr REAL,
                hop_count INTEGER, latitude REAL, longitude REAL,
                last_advert_timestamp TEXT)""")
            conn.execute("""CREATE TABLE repeater_contacts (
                public_key TEXT, name TEXT, last_seen TEXT,
                device_type TEXT)""")
            conn.execute("""CREATE TABLE message_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER,
                sender_id TEXT, sender_pubkey TEXT,
                channel TEXT, content TEXT, is_dm INTEGER)""")

    def add(self, name, digit="a", stamp="2026-01-01", role="repeater",
            adverts=None, snr=None, hops=None, lat=None, lon=None, key=None):
        key = key or digit * 64
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute(
                """INSERT INTO complete_contact_tracking
                   (public_key, name, last_heard, role, advert_count, snr,
                    hop_count, latitude, longitude)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, name, stamp, role, adverts, snr, hops, lat, lon),
            )
        return key

    def message(self, sender, content, seconds=0, channel="#test", dm=0, pubkey=None):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute(
                """INSERT INTO message_stats
                   (timestamp, sender_id, sender_pubkey, channel, content, is_dm)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (int(time.time()) + seconds, sender, pubkey, channel, content, dm),
            )

    def test_strict_bot_name_matching(self):
        for i, name in enumerate(
            ["Abbot", "Robot", "Bottom", "Botany", "Repeater"]
        ):
            self.add(name, f"{i:01x}")
        self.assertIn("No bot-name candidates", self.query(self.plugin, "!bots"))
        self.add("WeatherBot", "f")
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. ?FFFF n/a", result)

    def test_multiple_names_and_pages(self):
        for i in range(12):
            self.add(f"Bot {i:02d}", f"{i:01x}")
        result = self.query(self.plugin, "!bots")
        self.assertIn("Bots 1/", result)
        self.assertIn("1. ?0000 n/a", result)
        self.assertIn("2. ?1111 n/a", result)
        items, incomplete = self.plugin._contacts()
        pages, _ = self.plugin._pages(items, 158, incomplete)
        self.assertGreater(len(pages), 1)
        self.assertLess(len(pages), 12)
        self.assertIn("Bots 2/", self.query(self.plugin, "!bots 2"))
        self.assertIn("Bots 2/", self.query(self.plugin, "!bots page 2"))
        self.assertTrue(self.plugin.matches_custom_syntax(
            SimpleNamespace(content="!bots1")
        ))
        self.assertFalse(self.plugin.matches_custom_syntax(
            SimpleNamespace(content="botsxyz")
        ))

    def test_named_candidate_details(self):
        seen = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        key = self.add("WeatherBot", "b", stamp=seen, adverts=17,
                       snr=-7.5, hops=2)
        result = self.query(self.plugin, "!bots1")
        self.assertIn("Bot 1/1 ? WeatherBot", result)
        self.assertIn("ID bbbbbbbbbbbb | repeater", result)
        self.assertIn("Dist n/a | Seen 3h", result)
        self.assertIn("Ads 17 | SNR -7.5dB | Hops 2", result)
        self.assertIn("Reply 0x none", result)
        self.assertNotIn(key, result)
        self.assertEqual(result, self.query(self.plugin, "!bots info 1"))

    def test_single_ping_response_discovery_without_bot_name(self):
        self.add("ServiceAlpha", "a")
        self.message("UserX", "!ping", seconds=-10)
        self.message("ServiceAlpha", "Pong! | SNR: 5.5", seconds=-8)
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. *AAAA n/a", result)
        detail = self.query(self.plugin, "!bots1")
        self.assertIn("Bot 1/1 * ServiceAlpha", detail)
        self.assertIn("Reply 1x ping", detail)
        self.assertEqual(self.plugin._contacts()[0][0][3], 1)
        self.assertEqual(self.plugin._contacts()[0][0][3], 1)  # no duplicate insert

    def test_multi_command_secondary_evidence(self):
        self.add("MeshService", "a")
        self.message("UserX", "!ping", seconds=-35)
        self.message("MeshService", "Pong! | RSSI: -110", seconds=-34)
        self.message("UserX", "!test", seconds=-30)
        self.message("MeshService", "ack @[UserX] | SNR: 8", seconds=-28)
        self.message("UserX", "!path", seconds=-20)
        self.message("MeshService", "7bcf33: Example Relay", seconds=-18)
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. +AAAA n/a", result)
        detail = self.query(self.plugin, "!bots1")
        self.assertIn("Reply 3x path,ping,test", detail)
        self.assertIn("Bot 1/1 +", detail)
        row = self.plugin._contacts()[0][0]
        self.assertEqual(set(row[5]), {"ping", "path", "test"})
        self.assertEqual(row[3], 3)

    def test_official_direct_path_response_alone(self):
        self.message("Requester", "!path", seconds=-15)
        self.message("RouteHelper", "📡 Direct connection (0 hops)", seconds=-12)
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. *~ROU n/a", result)
        detail = self.query(self.plugin, "!bots1")
        self.assertIn("Reply 1x path", detail)

    def test_test_reply_without_bot_name(self):
        self.message("Requester", "!test", seconds=-15)
        self.message("ServiceAgent", "ack @[Requester] | RSSI -111", seconds=-10)
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. *~SER n/a", result)

    def test_repeated_same_command_is_not_multi_verification(self):
        self.message("Requester", "!ping", seconds=-20)
        self.message("ServiceAgent", "Pong!", seconds=-19)
        self.message("ServiceAgent", "Pong! | extra", seconds=-18)
        result = self.query(self.plugin, "!bots")
        self.assertIn("1. *~SER n/a", result)
        self.assertNotIn("1. +----", result)
        self.assertIn("Reply 2x ping", self.query(self.plugin, "!bots1"))

    def test_nonmatching_target_channel_window_and_dm(self):
        self.message("UserX", "!test", seconds=-30)
        self.message("NotBot", "ack @[AnotherUser] | Hello", seconds=-25)
        self.message("UserX", "!ping", seconds=-220)
        self.message("NotBot", "Pong!", seconds=-5)
        self.message("UserX", "!path", seconds=-10, channel="#other")
        self.message("NotBot", "a1b2c3: Repeater", seconds=-7)
        self.message("UserX", "!test", seconds=-4, dm=1)
        self.message("NotBot", "ack @[UserX] | hello", seconds=-2, dm=1)
        self.assertIn("No bot-name candidates", self.query(self.plugin, "!bots"))

    def test_random_path_text_never_counts(self):
        self.message("Requester", "!path", seconds=-20)
        self.message("OrdinaryUser", "Walking a new path today", seconds=-19)
        self.message("OrdinaryUser", "Node a1b2c3 is near me", seconds=-18)
        self.assertIn("No bot-name candidates", self.query(self.plugin, "!bots"))

    def test_cache_only_and_missing_schema(self):
        self.bot.meshcore.contacts = {
            "b"*64: {"adv_name": "WeatherBot"}
        }
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute("DROP TABLE message_stats")
        result = self.query(self.plugin, "!bots")
        self.assertIn("?BBBB n/a", result)
        self.assertIn("partial", result)

    def test_fallback_unknown_stats(self):
        self.bot.meshcore.contacts = {
            "b"*64: {"adv_name": "WeatherBot"}
        }
        reply = self.query(self.plugin, "!bots1")
        self.assertIn("Dist n/a | Seen n/a", reply)
        self.assertIn("Ads n/a | SNR n/a | Hops n/a", reply)

    def test_list_keyword_and_distance_are_read_only(self):
        # Bot at Toronto-area reference, contact one degree west.
        self.bot.config.getfloat = lambda _section, name, fallback=None: {
            "bot_latitude": 43.0, "bot_longitude": -80.0
        }.get(name, fallback)
        self.add("WeatherBot", "a", lat=43.0, lon=-79.0)
        listing = self.query(self.plugin, "!bots list")
        self.assertRegex(listing, r"1\. \?AAAA 81\.\d+km")
        detail = self.query(self.plugin, "!bots1")
        self.assertRegex(detail, r"Dist 81\.\d+km")
        self.assertEqual(listing, self.query(self.plugin, "!bots"))
        self.assertEqual(listing, self.query(self.plugin, "!bots list 1"))

    def test_bad_position_and_unknown_identity_show_na(self):
        self.bot.config.getfloat = lambda _section, name, fallback=None: {
            "bot_latitude": 43.0, "bot_longitude": -80.0
        }.get(name, fallback)
        self.add("WeatherBot", "a", lat=0.0, lon=0.0)
        self.message("Requester", "!ping", seconds=-9)
        self.message("AnonymousResponder", "Pong!", seconds=-7)
        output = self.query(self.plugin, "!bots")
        self.assertIn("?AAAA n/a", output)
        self.assertIn("*~ANO n/a", output)
        self.bot.config.getfloat = lambda _section, name, fallback=None: None
        self.assertIn("n/a", self.query(self.plugin, "!bots1"))

    def test_saved_sender_key_used_even_without_a_contact_or_bot_name(self):
        self.message("Requester", "!ping", seconds=-15)
        self.message(
            "RouteHelper", "Pong!", seconds=-12,
            pubkey="a1b2" + "c" * 60,
        )
        output = self.query(self.plugin, "!bots")
        self.assertIn("1. *A1B2 n/a", output)
        details = self.query(self.plugin, "!bots1")
        self.assertIn("ID a1b2cccccccc", details)

    def test_ambiguous_sender_keys_do_not_assign_an_identity(self):
        self.message("Requester", "!ping", seconds=-15)
        self.message("RouteHelper", "Pong!", seconds=-12, pubkey="a" * 64)
        self.message("RouteHelper", "Pong! extra", seconds=-11, pubkey="b" * 64)
        output = self.query(self.plugin, "!bots")
        self.assertIn("1. *~ROU n/a", output)
        self.assertNotIn("*AAAA", output)
        self.assertNotIn("*BBBB", output)

    def test_anonymization_disables_identity_linking_from_existing_rows(self):
        self.message("Requester", "!ping", seconds=-15)
        self.message(
            "RouteHelper", "Pong!", seconds=-12,
            pubkey="a1b2" + "c" * 60,
        )
        self.bot.config.getboolean = (
            lambda section, field, fallback=False:
            True if (section, field) == ("Stats_Command", "anonymize_users")
            else fallback
        )
        self.assertIn("*~ROU n/a", self.query(self.plugin, "!bots"))
        self.assertNotIn("*A1B2", self.query(self.plugin, "!bots"))

    def test_old_message_stats_schema_keeps_name_hint(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute("DROP TABLE message_stats")
            conn.execute("""CREATE TABLE message_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER,
                sender_id TEXT, channel TEXT, content TEXT, is_dm INTEGER)""")
            conn.execute(
                "INSERT INTO message_stats(timestamp,sender_id,channel,content,is_dm)"
                " VALUES(?,?,?,?,0)",
                (int(time.time()) - 12, "Requester", "#test", "!ping"),
            )
            conn.execute(
                "INSERT INTO message_stats(timestamp,sender_id,channel,content,is_dm)"
                " VALUES(?,?,?,?,0)",
                (int(time.time()) - 10, "RouteHelper", "#test", "Pong!"),
            )
        self.assertIn("*~ROU n/a", self.query(self.plugin, "!bots"))

    def test_prefix_collision_expands_four_hex_digit_id(self):
        self.add("Bot Alpha", key="a" * 64)
        self.add("Bot Beta", key="aaaa" + "b" * 60)
        output = self.query(self.plugin, "!bots")
        self.assertIn("?AAAAAA n/a", output)
        self.assertIn("?AAAABB n/a", output)
        self.assertNotIn("?AAAA n/a", output)

    def test_channel_budget(self):
        for i in range(5):
            self.add("WeatherBot " + "🤖"*30, f"{i:01x}")
        self.plugin.get_max_message_length = lambda msg: 110
        msg = SimpleNamespace(
            content="!bots", is_dm=False, channel="#test", replies=[]
        )
        self.assertTrue(asyncio.run(self.plugin.execute(msg)))
        self.assertLessEqual(len(msg.replies[0].encode("utf-8")), 110)
        self.assertIn("1.", msg.replies[0])
        self.assertIn("2.", msg.replies[0])

    def test_config_reference(self):
        from configparser import ConfigParser
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for file in ["docs/examples/mesh-diagnostics.ini",
                     "config.ini.diagnostics"]:
            parser = ConfigParser(interpolation=None)
            parser.read(root / file, encoding="utf-8")
            self.assertEqual(parser.get("Bots_Command", "channels"), "#test")
            self.assertEqual(
                parser.getint("Bots_Command", "reply_window_seconds"), 45
            )
