"""Self-contained tests for the local MyPaths, Traffic and Airtime commands.

Run: python -m unittest tests.test_local_diagnostics_extra -v
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class FakeBaseCommand:
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("test")

    def get_config_value(self, *_args, **kwargs):
        return kwargs.get("fallback")

    def can_execute(self, *_args, **kwargs):
        return True

    def get_max_message_length(self, _message):
        return 158

    async def send_response(self, message, text):
        message.replies.append(text)
        return True


def get_plugin(stem):
    mocks = {key: types.ModuleType(key) for key in (
        "modules", "modules.commands", "modules.commands.base_command", "modules.models"
    )}
    mocks["modules.commands.base_command"].BaseCommand = FakeBaseCommand
    mocks["modules.models"].MeshMessage = type("MeshMessage", (), {})
    path = Path(__file__).resolve().parents[1] / "local" / "commands" / (stem + ".py")
    with patch.dict(sys.modules, mocks):
        spec = importlib.util.spec_from_file_location("test_" + stem + "_local", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class BaseLocalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "test.db"
        self.config = SimpleNamespace(getboolean=lambda *args, **kw: kw.get("fallback", False))

        class DB:
            def __init__(this, path):
                this.path = path

            @contextmanager
            def connection(this):
                conn = sqlite3.connect(this.path)
                try:
                    yield conn
                finally:
                    conn.close()
        self.bot = SimpleNamespace(db_manager=DB(self.db_path), config=self.config)

    def query(self, plugin, request, sender="Tester"):
        message = SimpleNamespace(content=request, sender_id=sender, is_dm=True, replies=[])
        self.assertTrue(asyncio.run(plugin.execute(message)))
        self.assertEqual(len(message.replies), 1)
        self.assertLessEqual(len(message.replies[0].encode("utf-8")), 158)
        return message.replies[0]


class TestMyPaths(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.plugin = get_plugin("mypaths").MyPathsCommand(self.bot)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE message_stats(id INTEGER PRIMARY KEY, timestamp INTEGER,
                sender_id TEXT, is_dm INTEGER, path TEXT)""")

    def add(self, age, who, route, dm=1):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO message_stats(timestamp,sender_id,is_dm,path) VALUES(?,?,?,?)",
                         (int(time.time()) - age, who, dm, route))

    def test_no_history(self):
        self.assertIn("no saved DMs", self.query(self.plugin, "!mypaths"))

    def test_routes_and_private_dm_filter(self):
        self.add(15, "Tester", "AA,BB (2 hops via FWD)")
        self.add(20, "Tester", "AA,BB (2 hops via FWD)")
        self.add(40, "Tester", "Direct (0 hops)")
        self.add(50, "Other", "CC,DD (2 hops)")
        self.add(60, "Tester", "FF,EE (2 hops)", dm=0)
        output = self.query(self.plugin, "!mypaths")
        self.assertIn("3 DMs, 2 routes", output)
        self.assertIn("AA,BB: 2x", output)
        self.assertNotIn("CC,DD", output)
        self.assertNotIn("FF,EE", output)

    def test_unparsed_and_window(self):
        self.add(12, "Tester", "Routed through 2 hops")
        self.assertIn("no decoded route", self.query(self.plugin, "!mypaths 7d"))
        self.assertIn("DM:", self.query(self.plugin, "!mypaths 11d"))


class TestTraffic(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.plugin = get_plugin("traffic").TrafficCommand(self.bot)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE packet_stream(id INTEGER PRIMARY KEY, timestamp REAL, type TEXT, payload_type_name TEXT)")

    def add(self, seconds, typ="packet", payload="GRP_TXT"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO packet_stream(timestamp,type,payload_type_name) VALUES(?,?,?)",
                         (time.time() - seconds, typ, payload))

    def test_empty(self):
        self.assertIn("no saved RF packets", self.query(self.plugin, "!traffic"))

    def test_counts_types_no_mqtt(self):
        self.add(25, payload="GRP_TXT")
        self.add(1800, payload="ADVERT")
        self.add(9000, payload="TRACE")
        self.add(10, typ="command")
        text = self.query(self.plugin, "!traffic 6h")
        self.assertIn("3 packets", text)
        self.assertIn("15m: 1 / 1h: 2", text)
        self.assertIn("ADVERT", text)
        self.assertNotIn("command", text)

    def test_old_three_column_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE packet_stream RENAME TO old_stream")
            conn.execute("CREATE TABLE packet_stream(timestamp REAL, data TEXT, type TEXT)")
            conn.execute("INSERT INTO packet_stream VALUES(?,'{}','packet')", (time.time(),))
        self.assertIn("1 packets", self.query(self.plugin, "!traffic 1h"))


class TestAirTime(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.module = get_plugin("airtime")
        self.plugin = self.module.AirTimeCommand(self.bot)

    def test_phy_formula(self):
        # SF7/BW125k/CR4/5/8B/preamble 8, explicit header+CRC: ~36.1ms.
        self.assertAlmostEqual(self.module.lora_airtime_ms(8, sf=7, bw=125, cr=5, pre=8), 36.096, places=2)

    def test_arguments(self):
        v = self.query(self.plugin, "!airtime 80 sf=11 bw=250 cr=5 tx=3")
        self.assertIn("SF11 BW250k CR4/5", v)
        self.assertIn("3 TX", v)
        for val in ["!airtime 0", "!airtime 10 bw=100", "!airtime 99 sf=18", "!airtime 20 tx=25"]:
            self.assertTrue(self.query(self.plugin, val))
        self.assertIn("BYTES", self.query(self.plugin, "!airtime"))


if __name__ == "__main__":
    unittest.main()