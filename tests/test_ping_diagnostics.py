"""Isolated tests for the route-aware ping alternative. No radio required.

Run: python -m unittest discover -s tests -p test_ping_diagnostics.py -v
"""
import asyncio
import configparser
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class BaseStub:
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("ping-diagnostics-test")

    def get_config_value(self, *_args, **kwargs):
        return kwargs.get("fallback")

    def can_execute(self, _message, _skip_channel_check=False):
        return True

    def get_max_message_length(self, message):
        return 158 if message.is_dm else 135

    def _strip_quotes_from_config(self, text):
        return text.strip('"') if text else text

    def format_response(self, message, text):
        return text.replace("{sender}", message.sender_id or "Unknown").replace(
            "{connection_info}", "test-info"
        )

    async def send_response(self, message, response, **_kwargs):
        message.replies.append(response)
        return True


def load_alternative():
    root = Path(__file__).resolve().parents[1]
    path = root / "modules" / "commands" / "alternatives" / "ping_diagnostics.py"
    fake_modules = {}
    for name in [
        "modules", "modules.commands", "modules.commands.alternatives",
        "modules.commands.base_command", "modules.models",
    ]:
        fake_modules[name] = types.ModuleType(name)
        fake_modules[name].__path__ = []
    fake_modules["modules.commands.base_command"].BaseCommand = BaseStub
    fake_modules["modules.models"].MeshMessage = type("MeshMessage", (), {})
    with patch.dict(sys.modules, fake_modules):
        spec = importlib.util.spec_from_file_location(
            "modules.commands.alternatives.ping_diagnostics", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class PingDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_alternative()

    def setUp(self):
        cfg = configparser.ConfigParser()
        cfg["Keywords"] = {"ping": "Pong! | {connection_info}"}
        self.rows = [
            {"public_key": "d4e5f6" + "0" * 58, "name": "Repeater Alpha", "role": "repeater"},
            {"public_key": "0a1b2c" + "0" * 58, "name": "Solar Relay Beta", "role": "repeater"},
            {"public_key": "f1e2d3" + "0" * 58, "name": "Example Relay 🅱️", "role": "repeater"},
        ]

        async def get_repeaters(**_kwargs):
            return self.rows

        bot = SimpleNamespace(
            config=cfg,
            repeater_manager=SimpleNamespace(get_repeater_devices=get_repeaters),
        )
        self.cmd = self.mod.PingDiagnosticsCommand(bot)

    def ping(self, **kwargs):
        defaults = dict(
            content="ping", is_dm=True, sender_id="ExampleNode",
            hops=4, snr=5.5, path=None, routing_info=None, replies=[],
        )
        defaults.update(kwargs)
        msg = SimpleNamespace(**defaults)
        self.assertTrue(asyncio.run(self.cmd.execute(msg)))
        self.assertEqual(len(msg.replies), 1)
        self.assertLessEqual(len(msg.replies[0].encode("utf-8")), self.cmd.get_max_message_length(msg))
        return msg.replies[0]

    def test_four_hop_example_exact(self):
        nodes = ["a1b2c3", "d4e5f6", "0a1b2c", "f1e2d3"]
        actual = self.ping(routing_info={"path_nodes": nodes})
        expected = (
            "@[ExampleNode] 4 hops, SNR 5.5\n"
            "a1b2c3: n/a\n"
            "d4e5f6: Repeater Alpha\n"
            "0a1b2c: Solar Relay Beta\n"
            "f1e2d3: Example Relay 🅱️"
        )
        self.assertEqual(actual, expected)

    def test_path_fallback_and_missing_correlation(self):
        text = self.ping(
            routing_info=None,
            path="a1b2c3,d4e5f6,0a1b2c,f1e2d3 (4 hops via ROUTE_TYPE)",
        )
        self.assertIn("d4e5f6: Repeater Alpha", text)
        self.assertEqual(
            self.ping(path="Routed through 4 hops"),
            "@[ExampleNode] 4 hops, SNR 5.5",
        )

    def test_path_hex_and_snr_zero(self):
        text = self.ping(
            snr=0,
            routing_info={"path_length": 2, "bytes_per_hop": 3, "path_hex": "d4e5f60a1b2c"},
            hops=2,
        )
        self.assertIn("2 hops, SNR 0", text)
        self.assertIn("0a1b2c: Solar Relay Beta", text)

    def test_ambiguous_prefix_is_not_guessed(self):
        self.rows.append({
            "public_key": "d4e5f6" + "f" * 58, "name": "Another Repeater", "role": "repeater"
        })
        result = self.ping(hops=1, routing_info={"path_nodes": ["d4e5f6"]})
        self.assertIn("d4e5f6: n/a", result)

    def test_no_details_in_channel(self):
        result = self.ping(is_dm=False, routing_info={"path_nodes": ["d4e5f6"]})
        self.assertNotIn("Davisville", result)
        self.assertEqual(result, "@[ExampleNode] 4 hops, SNR 5.5")

    def test_legacy_ping_escape_hatch(self):
        self.assertEqual(self.ping(content="pingplain"), "Pong! | test-info")

    def test_long_names_stay_within_payload(self):
        self.rows = [
            {"public_key": f"{i:06x}" + "f" * 58,
             "name": ("Repeater with a very long regional descriptive location " * 2) + "🅱️",
             "role": "repeater"}
            for i in range(1, 7)
        ]
        result = self.ping(
            hops=6,
            routing_info={"path_nodes": [f"{i:06x}" for i in range(1, 7)]},
        )
        self.assertIn("000001:", result)
        self.assertLessEqual(len(result.encode("utf-8")), 158)


if __name__ == "__main__":
    unittest.main()
