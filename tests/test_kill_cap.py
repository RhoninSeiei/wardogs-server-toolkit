import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('killcap_app', os.environ.get(
    'WARDOGS_TEST_APP', str(Path(__file__).parents[1] / 'service/app.py')))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
ID = '76561190000000001'


class Fake:
    def __init__(self):
        self.players = [{'steamId': ID, 'name': 'fake', 'kills': 15, 'deaths': 100}]
        self.kicks = []
        self.error = None

    def request(self, method, path, body=None):
        if path == '/v1/players':
            return {'players': copy.deepcopy(self.players)}
        if path.endswith('/kick'):
            self.kicks.append(body['reason'])
            if self.error:
                raise self.error
        return {'ok': True}


class KillCapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.config['high_kill_kick'] = True
        self.config['high_kd_kick'] = False
        self.fake = Fake()
        self.engine = app.Engine(self.path, self.fake, self.config)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def count(self, kind):
        return self.engine.db.execute('SELECT count(*) FROM jobs WHERE kind=?', (kind,)).fetchone()[0]

    def test_fifteen_with_high_deaths_kicks_without_kd_policy_and_stays_private(self):
        self.engine.poll(now=100)
        self.assertEqual(self.count('kill_cap_kick'), 1)
        self.assertTrue(self.engine.send_one(now=100))
        self.assertEqual(len(self.fake.kicks), 1)
        self.assertEqual(self.count('management'), 3)
        messages = [r[0] for r in self.engine.db.execute("SELECT message FROM jobs WHERE kind='management'")]
        self.assertTrue(all('15' not in text and 'KD' not in text for text in messages))
        self.assertEqual(self.engine.kick_store.snapshot()['confirmedKickCount'], 1)
        self.assertEqual(self.engine.kick_store.snapshot()['mode'], 'permanent')

    def test_fourteen_is_not_kicked(self):
        self.fake.players[0]['kills'] = 14
        self.engine.poll(now=100)
        self.assertEqual(self.count('kill_cap_kick'), 0)
        self.assertEqual(self.fake.kicks, [])

    def test_missing_deaths_does_not_prevent_kill_cap(self):
        del self.fake.players[0]['deaths']
        self.engine.poll(now=100)
        self.assertEqual(self.count('kill_cap_kick'), 1)
        self.engine.send_one(now=100)
        self.assertEqual(len(self.fake.kicks), 1)

    def test_count_rechecked_before_kick(self):
        self.engine.poll(now=100)
        self.fake.players[0]['kills'] = 14
        self.engine.send_one(now=100)
        self.assertEqual(self.fake.kicks, [])
        self.assertEqual(self.count('management'), 0)
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE kind='kill_cap_kick'").fetchone()[0], 'cancelled')

    def test_poll_deduplicates_and_restart_keeps_unknown_without_replay(self):
        self.engine.poll(now=100)
        self.engine.poll(now=130)
        self.assertEqual(self.count('kill_cap_kick'), 1)
        self.fake.error = TimeoutError('unknown')
        self.engine.send_one(now=130)
        self.assertEqual(len(self.fake.kicks), 1)
        self.engine.close()
        self.engine = app.Engine(self.path, self.fake, self.config)
        self.engine.poll(now=200)
        self.assertEqual(self.count('kill_cap_kick'), 1)
        self.assertEqual(self.engine.kick_store.snapshot()['confirmedKickCount'], 0)
        self.assertEqual(self.count('management'), 0)

    def test_high_kd_and_cap_enqueue_only_cap(self):
        self.engine.config['high_kd_kick'] = True
        self.fake.players[0]['deaths'] = 0
        self.engine.poll(now=100)
        self.assertEqual(self.count('kill_cap_kick'), 1)
        self.assertEqual(self.count('kd_kick'), 0)

    def test_eligibility_boundaries_and_invalid_counts(self):
        for value in [None, True, False, -1, 14, 14.9, 15.5, float('nan'), float('inf'), 'bad', 10**10000]:
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(app.kill_cap_eligible({'kills': value}))
        for value in [15, 16, 100, '15']:
            self.assertTrue(app.kill_cap_eligible({'kills':value, 'deaths':999}))

    def test_kick_has_priority_over_announcement_backlog(self):
        with self.engine.db:
            self.engine.add_job('old announcement', 1, kind='management')
        self.engine.poll(now=100)
        self.engine.send_one(now=100)
        self.assertEqual(len(self.fake.kicks), 1)
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE message='old announcement'").fetchone()[0], 'queued')
