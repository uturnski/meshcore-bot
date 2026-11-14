"""Isolated SQLite tests; no credentials, radio or optional runtime dependencies."""
import asyncio
from contextlib import closing
import sqlite3
from types import SimpleNamespace
from tests.test_local_diagnostics_extra import BaseLocalTest, get_plugin


class TestBots(BaseLocalTest):
    def setUp(self):
        super().setUp()
        self.module = get_plugin('bots')
        self.plugin = self.module.BotsCommand(self.bot)
        self.bot.meshcore = SimpleNamespace(contacts={})
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            for table, stamp in [('complete_contact_tracking', 'last_heard'), ('repeater_contacts', 'last_seen')]:
                conn.execute(f'CREATE TABLE {table}(public_key TEXT, name TEXT, {stamp} TEXT)')

    def add(self, key, name, table='complete_contact_tracking', stamp='2026-01-01'):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute(f'INSERT INTO {table} VALUES(?,?,?)', (key, name, stamp))

    def test_no_role_guessing(self):
        for i, name in enumerate(['Companion', 'Repeater', 'RoomServer']):
            self.add(f'{i:064x}', name)
        self.assertIn('No saved bot candidates', self.query(self.plugin, '!bots'))
        self.add('a'*64, 'Weather Bot')
        reply = self.query(self.plugin, '!bots')
        self.assertIn('name?', reply)
        self.assertIn('unverified', reply)

    def test_dedup_full_keys_and_latest_cache(self):
        self.add('a'*64, 'Old Bot')
        self.add('A'*64, 'New Bot', stamp='2026-02-01')
        self.add('a'*64, 'Other Bot', 'repeater_contacts')
        self.bot.meshcore.contacts = {'a'*64: {'adv_name': 'Cached Bot'}}
        reply = self.query(self.plugin, '!bots')
        self.assertIn('1/1', reply)
        self.assertIn('Cached Bot', reply)

    def test_only_full_valid_keys_are_accepted(self):
        self.add('a'*64, 'Plain node')
        self.add('b'*12, 'Bogus Bot')
        self.assertIn('No saved bot candidates', self.query(self.plugin, '!bots'))

    def test_paging_utf8_and_collision(self):
        for i in range(15):
            self.add('a'*12 + f'{i:052x}', 'Bot ' + '\U0001f916'*60 + '\n\x00evil')
        for i in range(1, 16):
            reply = self.query(self.plugin, f'!bots {i}')
            self.assertIn(f'{i}/15', reply)
            self.assertNotIn('\x00', reply)
        self.assertIn('Only 15', self.query(self.plugin, '!bots 16'))

    def test_arguments(self):
        for arg in ['0', '-1', 'x', '2 extra', '9'*5000]:
            self.assertIn('positive integer', self.query(self.plugin, '!bots ' + arg))

    def test_missing_schema_and_cache_fallback(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as conn:
            conn.execute('DROP TABLE complete_contact_tracking')
        self.bot.meshcore.contacts = {'c'*64: {'adv_name': 'Cache Bot'}}
        self.assertIn('Partial local data', self.query(self.plugin, '!bots'))
        self.bot.db_manager = None
        self.assertIn('Cache Bot', self.query(self.plugin, '!bots'))

    def test_dm_only_and_disabled(self):
        message = SimpleNamespace(content='!bots', is_dm=False, replies=[])
        self.assertFalse(asyncio.run(self.plugin.execute(message)))
        self.assertEqual(message.replies, [])
        self.plugin.enabled = False
        self.assertFalse(self.plugin.can_execute(message))

    def test_any_bot_substring_case_insensitive(self):
        for i, name in enumerate(['Robot', 'Bottom', 'Botany', 'WeatherBot', 'bOt']):
            self.add(f'{i:064x}', name)
        records, _ = self.plugin._contacts()
        self.assertEqual(len(records), 5)
        self.assertTrue(all(row[2] == 'name?' for row in records))
