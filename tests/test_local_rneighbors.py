"""Isolated tests for manual allowlisted remote repeater neighbour snapshots."""
import asyncio
from contextlib import closing
import json
from types import SimpleNamespace
import sqlite3
import tempfile
from tests.test_local_diagnostics_extra import BaseLocalTest, get_plugin

KEY_A = "a" * 64
KEY_B = "b" * 64
ADMIN = "c" * 64

class TestRneighbors(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.module = get_plugin("rneighbors")
        self.bot.meshcore = SimpleNamespace(commands=SimpleNamespace())
        self.bot.config = SimpleNamespace(has_section=lambda s: True)
        self.plugin = self.module.RneighborsCommand(self.bot)
        self.plugin.enabled = True
        self.plugin._check_admin_access = (
            lambda msg: getattr(msg, "sender_pubkey", None) == ADMIN
        )
        self.plugin._selected = lambda: {"rep1": KEY_A}
        self.plugin.get_max_message_length = lambda _: 158
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""CREATE TABLE complete_contact_tracking (
                public_key TEXT, name TEXT, last_heard TEXT)""")
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES(?,?,?)",
                (KEY_B, "Example Neighbor", "2026-11-15")
            )
            conn.commit()
        self.calls = []
        async def remote(key, **kwargs):
            self.calls.append((key, kwargs))
            if kwargs["offset"] == 0:
                return {
                    "neighbours_count": 14,
                    "results_count": 12,
                    "neighbours": [
                        {"pubkey": KEY_B[:12], "secs_ago": 15, "snr": 5.25}
                    ] + [
                        {"pubkey": ("%012x" % i), "secs_ago": 60, "snr": -3.5}
                        for i in range(11)
                    ],
                }
            return {
                "neighbours_count": 14,
                "results_count": 2,
                "neighbours": [
                    {"pubkey": "00aabbccddff", "secs_ago": 10, "snr": 3.25},
                    {"pubkey": "00aabbccddee", "secs_ago": 33, "snr": 2.5},
                ],
            }
        self.bot.meshcore.commands.req_neighbours_sync = remote

    def ask(self, cmd, sender=ADMIN, dm=True):
        msg = SimpleNamespace(content=cmd, sender_id="Tester",
                              sender_pubkey=sender, is_dm=dm, replies=[])
        ok = asyncio.run(self.plugin.execute(msg))
        return ok, msg.replies[0] if msg.replies else ""

    def test_strict_allowlist_validation(self):
        parse = self.module._allowlist
        good = parse(f"rep1={KEY_A}, rep2={KEY_B}")
        self.assertEqual(good, {"rep1": KEY_A, "rep2": KEY_B})
        self.assertEqual(parse(f"rep1=abcdef, rep2={KEY_B}"), {"rep2": KEY_B})
        self.assertEqual(parse(f"rep1={KEY_A},rep1={KEY_B}"), {"rep1": KEY_A})

    def test_only_admin_dm_and_allowlisted(self):
        self.assertEqual(self.ask("!rneighbors", sender=KEY_B), (False, ""))
        self.assertEqual(self.ask("!rneighbors", dm=False), (False, ""))
        ok, text = self.ask("!rneighbors refresh unknown")
        self.assertTrue(ok)
        self.assertIn("not selected", text)
        self.assertEqual(self.calls, [])

    def test_selected_list_is_passive(self):
        ok, text = self.ask("!rneighbors")
        self.assertTrue(ok)
        self.assertIn("rep1", text)
        self.assertIn(KEY_A[:12], text)
        self.assertNotIn(KEY_A, text)
        self.assertEqual(self.calls, [])

    def test_cache_requires_explicit_refresh(self):
        ok, text = self.ask("!rneighbors rep1")
        self.assertTrue(ok)
        self.assertIn("no cached neighbours", text)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.ask("!rneighbors refresh rep1")[0])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], KEY_A)
        self.assertEqual(self.calls[0][1]["count"], 12)
        self.assertEqual(self.calls[0][1]["offset"], 0)
        self.assertEqual(self.calls[0][1]["pubkey_prefix_length"], 4)
        result = self.ask("!rneighbors rep1")[1]
        self.assertIn("12/14", result)
        self.assertIn("Example Neigh", result)  # name is byte-limited on-air
        self.assertLessEqual(len(result.encode("utf-8")), 158)
        self.assertEqual(len(self.calls), 1)

    def test_manual_second_fetch_and_cached_pages(self):
        self.ask("!rneighbors refresh rep1")
        self.ask("!rneighbors refresh rep1 2")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][1]["offset"], 12)
        result = self.ask("!rneighbors rep1 5")[1]
        self.assertIn("14/14", result)
        self.assertIn("13.", result)
        self.assertEqual(len(self.calls), 2)

    def test_unsupported_api_or_timeout_does_not_fake_cache(self):
        del self.bot.meshcore.commands.req_neighbours_sync
        self.assertIn("unavailable", self.ask("!rneighbors refresh rep1")[1])
        self.assertIn("no cached", self.ask("!rneighbors rep1")[1])

    def test_bad_arguments_never_query(self):
        for request in ["!rneighbors xxx", "!rneighbors refresh", 
                        "!rneighbors refresh rep1 0", "!rneighbors rep1 nope"]:
            self.assertTrue(self.ask(request)[0])
        self.assertEqual(self.calls, [])
