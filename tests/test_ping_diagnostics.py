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
            {"public_key": "7bcf33" + "0" * 58, "name": "Davisville III", "role": "repeater"},
            {"public_key": "971942" + "0" * 58, "name": "Rusty_Solar Repeater", "role": "repeater"},
            {"public_key": "b1b3e6" + "0" * 58, "name": "Bickford 🅱️", "role": "repeater"},
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
            content="ping", is_dm=True, sender_id="Meshmusher232",
            hops=4, snr=5.5, path=None, routing_info=None, replies=[],
        )
        defaults.update(kwargs)
        msg = SimpleNamespace(**defaults)
        self.assertTrue(asyncio.run(self.cmd.execute(msg)))
        self.assertEqual(len(msg.replies), 1)
        self.assertLessEqual(len(msg.replies[0].encode("utf-8")), self.cmd.get_max_message_length(msg))
        return msg.replies[0]

    def test_four_hop_example_exact(self):
        nodes = ["70cae2", "7bcf33", "971942", "b1b3e6"]
        actual = self.ping(routing_info={"path_nodes": nodes})
        expected = (
            "@[Meshmusher232] 4 hops, SNR 5.5\n"
            "70cae2: n/a\n"
            "7bcf33: Davisville III\n"
            "971942: Rusty_Solar Repeater\n"
            "b1b3e6: Bickford 🅱️"
        )
        self.assertEqual(actual, expected)

    def test_path_fallback_and_missing_correlation(self):
        text = self.ping(
            routing_info=None,
            path="70cae2,7bcf33,971942,b1b3e6 (4 hops via ROUTE_TYPE)",
        )
        self.assertIn("7bcf33: Davisville III", text)
        self.assertEqual(
            self.ping(path="Routed through 4 hops"),
            "@[Meshmusher232] 4 hops, SNR 5.5",
        )

    def test_path_hex_and_snr_zero(self):
        text = self.ping(
            snr=0,
            routing_info={"path_length": 2, "bytes_per_hop": 3, "path_hex": "7bcf33971942"},
            hops=2,
        )
        self.assertIn("2 hops, SNR 0", text)
        self.assertIn("971942: Rusty_Solar Repeater", text)

    def test_ambiguous_prefix_is_not_guessed(self):
        self.rows.append({
            "public_key": "7bcf33" + "f" * 58, "name": "Another Repeater", "role": "repeater"
        })
        result = self.ping(hops=1, routing_info={"path_nodes": ["7bcf33"]})
        self.assertIn("7bcf33: n/a", result)

    def test_no_details_in_channel(self):
        result = self.ping(is_dm=False, routing_info={"path_nodes": ["7bcf33"]})
        self.assertNotIn("Davisville", result)
        self.assertEqual(result, "@[Meshmusher232] 4 hops, SNR 5.5")

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
