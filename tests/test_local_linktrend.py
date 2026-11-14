"""Regression checks for the read-only local linktrend command.

Run: python -m unittest tests.test_local_linktrend -v
Dependencies: standard library only; radio/bot modules are stubbed for this test.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class FakeBaseCommand:
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("linktrend-tests")

    def get_config_value(self, *_a, **kwargs):
        return kwargs.get("fallback")

    def can_execute(self, message, skip_channel_check=False):
        return True

    def get_max_message_length(self, message):
        return 158

    async def send_response(self, message, text):
        message.replies.append(text)
        return True


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in ("modules", "modules.commands", "modules.commands.base_command", "modules.models")
    }
    modules["modules.commands.base_command"].BaseCommand = FakeBaseCommand
    modules["modules.models"].MeshMessage = type("MeshMessage", (), {})
    plugin_path = Path(__file__).resolve().parents[1] / "local" / "commands" / "linktrend.py"
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("local_linktrend_under_test", plugin_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.LinkTrendCommand


class TestLinktrend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Command = load_plugin()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE neighbor_observations(
              id INTEGER PRIMARY KEY, observed_at TEXT, self_public_key TEXT,
              neighbor_public_key TEXT, snr REAL, heard_secs_ago INTEGER,
              scopes TEXT, status TEXT)""")
        path = self.path

        class Manager:
            @contextmanager
            def connection(self):
                con = sqlite3.connect(path)
                try:
                    yield con
                finally:
                    con.close()

        self.cmd = self.Command(SimpleNamespace(db_manager=Manager()))
        self.now = datetime.now(timezone.utc)

    def add(self, key, snr, age, radio="radio1"):
        stamp = (self.now - timedelta(days=age)).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""INSERT INTO neighbor_observations
                 (observed_at,self_public_key,neighbor_public_key,snr,status)
                 VALUES (?,?,?,?,'responded')""", (stamp, radio, key, snr))

    def ask(self, command):
        msg = SimpleNamespace(content=command, is_dm=True, replies=[])
        self.assertTrue(asyncio.run(self.cmd.execute(msg)))
        self.assertEqual(len(msg.replies), 1)
        self.assertLessEqual(len(msg.replies[0].encode()), 158)
        return msg.replies[0]

    def test_missing_and_single_sample(self):
        self.assertIn("no discovery history", self.ask("!linktrend"))
        self.add("aabbccdd" + "0" * 56, -7, 1)
        self.assertIn("One sample", self.ask("!linktrend aabbccdd"))
        self.assertIn("linktrend weak", self.ask("!linktrend invalid"))

    def test_trend_summary_weak_and_window(self):
        self.add("a1b2c3d4" + "0" * 56, 2, 4)
        self.add("a1b2c3d4" + "0" * 56, 7, 1)
        self.add("baadf00d" + "0" * 56, -5, 1)
        self.add("c001cafe" + "0" * 56, -3, 80)
        self.assertIn("LinkTrend 30d: 2 cycles", self.ask("!linktrend"))
        self.assertIn("Last avg: +1.0dB", self.ask("!linktrend"))
        self.assertIn("BAADF00D", self.ask("!linktrend weak"))
        self.assertIn("delta +5.0dB", self.ask("!linktrend a1b2c3d4 7d"))
        self.assertIn("C001CAFE", self.ask("!linktrend c001cafe 90d"))
        self.assertIn("No neighbour", self.ask("!linktrend c001cafe 7d"))

    def test_ambiguity_and_radio_isolation(self):
        self.add("abcdaaaa" + "0" * 56, 3, 1)
        self.add("abcdbbbb" + "0" * 56, 4, 1)
        self.assertIn("Ambiguous ABCD", self.ask("!linktrend abcd"))
        self.add("abcdaaaa" + "0" * 56, 40, 2, radio="other")
        self.assertNotIn("+40.0dB", self.ask("!linktrend abcd"))


if __name__ == "__main__":
    unittest.main()
