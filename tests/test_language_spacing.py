import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('spacing_app', os.environ.get(
    'WARDOGS_TEST_APP', str(Path(__file__).parents[1] / 'service/app.py')))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class FakeRcon:
    def __init__(self):
        self.sent = []

    def request(self, method, path, body=None):
        self.sent.append(body['message'])
        return {'ok': True}


class LanguageSpacingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.rcon = FakeRcon()
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.config['language_interval_seconds'] = 10
        self.engine = app.Engine(self.path, self.rcon, self.config)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def enqueue(self, message, kind='periodic', recipient=None):
        with self.engine.db:
            self.engine.add_job(message, 1, kind=kind, recipient=recipient)

    def test_same_language_three_seconds_cross_language_ten(self):
        for message in ['中文第一段', '中文第二段', '[EN] First', '[EN] Second', '[JA] 日本語']:
            self.enqueue(message)
        self.assertTrue(self.engine.send_one(now=100))
        self.assertFalse(self.engine.send_one(now=102))
        self.assertTrue(self.engine.send_one(now=103))
        self.assertFalse(self.engine.send_one(now=106))
        self.assertFalse(self.engine.send_one(now=112))
        self.assertTrue(self.engine.send_one(now=113))
        self.assertTrue(self.engine.send_one(now=116))
        self.assertFalse(self.engine.send_one(now=125))
        self.assertTrue(self.engine.send_one(now=126))
        self.assertEqual(len(self.rcon.sent), 5)

    def test_all_timed_kinds_including_network_and_kd(self):
        for kind in ('periodic', 'network', 'kd'):
            with self.subTest(kind=kind):
                self.engine.state.pop('lastTimedAnnouncement', None)
                self.engine.next_send = 0
                self.enqueue('中文', kind)
                self.enqueue('[EN] Advice', kind)
                self.assertTrue(self.engine.send_one(now=100))
                self.assertFalse(self.engine.send_one(now=109))
                self.assertTrue(self.engine.send_one(now=110))

    def test_restart_preserves_gap(self):
        self.enqueue('中文', 'network')
        self.enqueue('[EN] Advice', 'network')
        self.assertTrue(self.engine.send_one(now=100))
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)
        self.assertFalse(self.engine.send_one(now=109))
        self.assertTrue(self.engine.send_one(now=110))

    def test_management_priority_and_whisper_dont_reset_timed_gap(self):
        self.enqueue('中文')
        self.assertTrue(self.engine.send_one(now=100))
        self.enqueue('[EN] Rules')
        self.enqueue('[EN] Urgent', 'management')
        self.assertTrue(self.engine.send_one(now=103))
        self.enqueue('欢迎', 'welcome', '76561190000000001')
        self.assertFalse(self.engine.send_one(now=106))
        self.assertTrue(self.engine.send_one(now=110))
        self.assertTrue(self.engine.send_one(now=113))
        self.assertEqual(self.rcon.sent, ['中文', '[EN] Urgent', '[EN] Rules', '欢迎'])

    def test_completion_time_starts_gap(self):
        from unittest.mock import patch
        self.enqueue('中文', 'network')
        self.enqueue('[EN] Advice', 'network')
        with patch.object(app.time, 'time', side_effect=[100, 105]):
            self.assertTrue(self.engine.send_one())
        self.assertFalse(self.engine.send_one(now=114))
        self.assertTrue(self.engine.send_one(now=115))
