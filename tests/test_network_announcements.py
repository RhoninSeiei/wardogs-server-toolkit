import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from service.network_announcements import next_round, validate

SPEC = importlib.util.spec_from_file_location('network_test_app', os.environ.get(
    'WARDOGS_TEST_APP', str(Path(__file__).parents[1] / 'service' / 'app.py')))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class FakeRcon:
    players = [{'steamId': '76561190000000001', 'name': 'example', 'kills': 0, 'deaths': 0}]

    def request(self, method, path, body=None):
        return {'players': self.players, 'ok': True}


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.config['network_messages'] = ['网络提示', '[EN] Network advice', '[JA] 通信の案内']
        self.rcon = FakeRcon()
        self.engine = app.Engine(self.path, self.rcon, self.config)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def clear_queue(self):
        with self.engine.db:
            self.engine.db.execute("UPDATE jobs SET status='sent'")

    def network_count(self):
        return self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='network'").fetchone()[0]

    def test_interval_and_projected_stagger(self):
        for now in (100, 130, 1777):
            for _ in range(100):
                due = next_round(self.config, now, 100)
                self.assertTrue(1080 <= due - now <= 1320)
                phase = (due - 100) % 600
                self.assertGreaterEqual(min(phase, 600-phase), 90)

    def test_due_persisted_restart_and_no_duplicate(self):
        self.engine.poll(now=100)
        due = self.engine.state['nextNetworkRound']
        self.assertTrue(1180 <= due <= 1420)
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)
        self.assertEqual(self.engine.state['nextNetworkRound'], due)
        # Simulate normal rule rounds already delivered.
        self.engine.state['lastGlobalRound'] = due - 200
        self.clear_queue()
        self.engine.poll(now=due)
        self.assertEqual(self.network_count(), 3)
        self.assertEqual(self.engine.state['lastNetworkRound'], due)
        self.assertTrue(1080 <= self.engine.state['nextNetworkRound'] - due <= 1320)
        self.engine.poll(now=due+30)
        self.assertEqual(self.network_count(), 3)

    def test_rules_due_and_pending_delay_network(self):
        self.engine.poll(now=100)
        self.engine.state['nextNetworkRound'] = 700
        self.engine.poll(now=700)
        self.assertEqual(self.network_count(), 0)
        self.engine.poll(now=820)
        self.assertEqual(self.network_count(), 0)
        self.clear_queue()
        self.engine.poll(now=850)
        self.assertEqual(self.network_count(), 3)

    def test_rule_round_approaching_defers_late_network(self):
        self.engine.poll(now=100)
        self.clear_queue()
        self.engine.state['nextNetworkRound'] = 650
        self.engine.poll(now=650)
        self.assertEqual(self.network_count(), 0)

    def test_empty_server_and_transaction_rollback(self):
        self.engine.poll(now=100)
        self.engine.state.update(nextNetworkRound=300, lastGlobalRound=100)
        self.clear_queue()
        self.rcon.players = []
        self.engine.poll(now=300)
        self.assertEqual(self.network_count(), 0)
        self.rcon.players = FakeRcon.players
        self.engine.config['queue_limit'] = 2
        with self.assertRaises(Exception):
            self.engine.poll(now=330)
        self.assertEqual(self.network_count(), 0)
        self.assertEqual(self.engine.state['nextNetworkRound'], 300)
        self.engine.config['queue_limit'] = 1000
        self.engine.poll(now=360)
        self.assertEqual(self.network_count(), 3)

    def test_validation_and_disabled_default(self):
        validate(self.config)
        for field, value in [('network_announcement_seconds', 120),
                             ('network_announcement_jitter_seconds', True),
                             ('network_messages', ['x'*201])]:
            config = copy.deepcopy(self.config)
            config[field] = value
            with self.assertRaises(ValueError):
                validate(config)
        self.engine.config['network_messages'] = []
        self.engine.poll(now=100)
        self.assertNotIn('nextNetworkRound', self.engine.state)
