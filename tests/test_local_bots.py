"""Isolated, read-only bot-directory tests. No credentials, internet or radio."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
from tests.test_local_diagnostics_extra import BaseLocalTest, get_plugin


class TestBots(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.module = get_plugin("bots")
        self.plugin = self.module.BotsCommand(self.bot)
        self.bot.meshcore = SimpleNamespace(contacts={})
        self.plugin.is_channel_allowed = lambda msg: msg.is_dm or getattr(msg, "channel", None) == "#test"
        # Stub only the BaseCommand mention/prefix helper; real code uses the
        # upstream cleanup matcher in BaseCommand.
        self.plugin._cleaned_content_matches = (
            lambda message, predicate: predicate(message.content.strip().removeprefix("!").lower())
        )
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute("""CREATE TABLE complete_contact_tracking (
              public_key TEXT, name TEXT, last_heard TEXT, role TEXT,
              device_type TEXT, advert_count INTEGER, snr REAL, hop_count INTEGER)""")
            conn.execute("""CREATE TABLE repeater_contacts (
              public_key TEXT, name TEXT, last_seen TEXT, device_type TEXT)""")

    def add(self, key, name, table="complete_contact_tracking", stamp="2026-01-01",
            role="repeater", adverts=None, snr=None, hops=None):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            if table == "complete_contact_tracking":
                conn.execute(
                    """INSERT INTO complete_contact_tracking
                        (public_key,name,last_heard,role,advert_count,snr,hop_count)
                        VALUES(?,?,?,?,?,?,?)""",
                    (key, name, stamp, role, adverts, snr, hops),
                )
            else:
                conn.execute(
                    "INSERT INTO repeater_contacts VALUES(?,?,?,?)",
                    (key, name, stamp, role),
                )

    def test_name_heuristic_not_role(self):
        for i, name in enumerate(["Companion", "Repeater", "RoomServer"]):
            self.add(f"{i:064x}", name)
        self.assertIn("No saved bot-name candidates", self.query(self.plugin, "!bots"))
        self.add("a"*64, "Weather Bot")
        reply = self.query(self.plugin, "!bots")
        self.assertIn("name?", reply)
        self.assertIn("unverified", reply)
        self.assertIn("1. Weather Bot", reply)

    def test_multi_name_list_and_detail_syntax(self):
        for i in range(8):
            self.add(f"{i:064x}", f"Bot {i:02d}")
        page_one = self.query(self.plugin, "!bots")
        self.assertIn("Bots 1/", page_one)
        self.assertIn("1. Bot 00", page_one)
        self.assertIn("2. Bot 01", page_one)
        self.assertIn("!botsN info", page_one)
        self.assertNotIn(" | repeater", page_one)
        all_records, limited = self.plugin._contacts()
        pages, _ = self.plugin._pages(all_records, 158, limited)
        self.assertGreater(len(pages), 1)
        self.assertLess(len(pages), 8)  # multiple names per list page
        self.assertIn("Bots 2/", self.query(self.plugin, "!bots 2"))
        self.assertIn("Bots 2/", self.query(self.plugin, "!bots page 2"))
        self.assertTrue(self.plugin.matches_custom_syntax(SimpleNamespace(content="!bots1")))
        self.assertTrue(self.plugin.matches_custom_syntax(SimpleNamespace(content="bots2")))
        self.assertFalse(self.plugin.matches_custom_syntax(SimpleNamespace(content="botsxxx")))

    def test_bot_number_details_include_recorded_stats(self):
        seen = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        key = "b"*64
        self.add(key, "WeatherBot", stamp=seen, role="repeater",
                 adverts=17, snr=-7.5, hops=2)
        reply = self.query(self.plugin, "!bots1")
        self.assertIn("Bot 1/1 (unverified)", reply)
        self.assertIn("WeatherBot", reply)
        self.assertIn("ID bbbbbbbbbbbb | repeater", reply)
        self.assertIn("Seen 3h ago | Ads 17", reply)
        self.assertIn("SNR -7.5dB | Hops 2", reply)
        self.assertNotIn(key, reply)
        self.assertEqual(reply, self.query(self.plugin, "!bots info 1"))
        self.assertEqual(reply, self.query(self.plugin, "!bots detail 1"))

    def test_no_data_does_not_invent_stats(self):
        self.bot.meshcore.contacts = {
            "a"*64: {"adv_name": "SkyBot"}
        }
        reply = self.query(self.plugin, "!bots1")
        self.assertIn("SkyBot", reply)
        self.assertIn("Seen n/a | Ads n/a", reply)
        self.assertIn("SNR n/a | Hops n/a", reply)

    def test_dedup_full_keys_and_latest_cache(self):
        self.add("a"*64, "Old Bot")
        self.add("A"*64, "New Bot", stamp="2026-02-01")
        self.add("a"*64, "Other Bot", "repeater_contacts")
        self.bot.meshcore.contacts = {"a"*64: {"adv_name": "Cached Bot"}}
        reply = self.query(self.plugin, "!bots")
        self.assertIn("(1)", reply)
        self.assertIn("Cached Bot", reply)
        self.assertNotIn("Old Bot", reply)

    def test_invalid_short_keys_not_candidates(self):
        self.add("a"*64, "Plain node")
        self.add("b"*12, "Bogus Bot")
        self.assertIn("No saved bot-name candidates", self.query(self.plugin, "!bots"))

    def test_name_cleanup_utf8_pages_and_collision(self):
        for i in range(15):
            self.add("a"*12 + f"{i:052x}", "Bot " + "🤖"*60 + "\n\x00evil")
        records, limited = self.plugin._contacts()
        pages, footer = self.plugin._pages(records, 158, limited)
        self.assertGreater(len(pages), 1)
        self.assertLess(len(pages), 15)
        for i in range(1, len(pages)+1):
            reply = self.query(self.plugin, f"!bots {i}")
            self.assertIn(f"Bots {i}/{len(pages)}", reply)
            self.assertNotIn("\x00", reply)
            self.assertLessEqual(len(reply.encode("utf-8")), 158)
        self.assertIn("Only", self.query(self.plugin, f"!bots {len(pages)+1}"))
        reply = self.query(self.plugin, "!bots1")
        self.assertIn("ID aaaaaaaaaaaa", reply)
        self.assertLessEqual(len(reply.encode("utf-8")), 158)

    def test_arguments_and_range(self):
        for arg in ["0", "-1", "x", "2 extra", "9"*5000]:
            self.assertIn("Usage:", self.query(self.plugin, "!bots " + arg))
        self.assertIn("Usage:", self.query(self.plugin, "!bots0"))
        self.add("a"*64, "TestBot")
        self.assertIn("Only", self.query(self.plugin, "!bots99"))

    def test_missing_schema_and_cache_fallback(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute("DROP TABLE complete_contact_tracking")
        self.bot.meshcore.contacts = {"c"*64: {"adv_name": "Cache Bot"}}
        self.assertIn("Partial;", self.query(self.plugin, "!bots"))
        self.bot.db_manager = None
        self.assertIn("Cache Bot", self.query(self.plugin, "!bots"))

    def test_other_channels_and_disabled(self):
        message = SimpleNamespace(content="!bots", is_dm=False, channel="#general", replies=[])
        self.assertFalse(asyncio.run(self.plugin.execute(message)))
        self.assertEqual(message.replies, [])
        self.plugin.enabled = False
        self.assertFalse(self.plugin.can_execute(message))

    def test_case_insensitive_name_match_and_identity(self):
        for i, name in enumerate(["Robot", "Bottom", "Botany", "WeatherBot", "bOt"]):
            self.add(f"{i:064x}", name)
        rows, _ = self.plugin._contacts()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[2] == "name?" for row in rows))

    def test_channel_small_budget_list_and_detail(self):
        for i in range(4):
            self.add(f"{i:064x}", "WeatherBot " + "🤖"*40)
        message = SimpleNamespace(content="!bots", is_dm=False, channel="#test", replies=[])
        self.plugin.get_max_message_length = lambda msg: 110
        self.assertTrue(asyncio.run(self.plugin.execute(message)))
        self.assertLessEqual(len(message.replies[-1].encode("utf-8")), 110)
        self.assertIn("1.", message.replies[-1])
        self.assertIn("2.", message.replies[-1])  # abbreviated to fit two
        message.content = "!bots1"
        self.assertTrue(asyncio.run(self.plugin.execute(message)))
        self.assertLessEqual(len(message.replies[-1].encode("utf-8")), 110)

    def test_config_examples_channel(self):
        from configparser import ConfigParser
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for name in ["docs/examples/mesh-diagnostics.ini", "config.ini.diagnostics"]:
            config = ConfigParser(interpolation=None)
            config.read(root / name, encoding="utf-8")
            self.assertEqual(config.get("Bots_Command", "channels"), "#test")
