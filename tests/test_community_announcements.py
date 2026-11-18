import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('community_app', os.environ.get(
    'WARDOGS_TEST_APP', str(Path(__file__).parents[1] / 'service/app.py')))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class Fake:
    def __init__(self):
        self.players = [{'steamId':'76561190000000001','name':'fake','kills':0,'deaths':0}]
        self.sent=[]

    def request(self,method,path,body=None):
        if path=='/v1/players':return {'players':self.players}
        self.sent.append(body['message'])
        return {'ok':True}


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'state.db'
        self.config=copy.deepcopy(app.DEFAULT_CONFIG)
        self.config.update(community_announcement_seconds=900,community_messages=['QQ群1000000001；无管理时审核可能变慢。','[EN] QQ 1000000001. Reviews may take longer without admins.','[JA] QQ群1000000001。管理者不在時は審査が遅れる場合があります。'])
        self.fake=Fake();self.engine=app.Engine(self.path,self.fake,self.config)

    def tearDown(self):
        self.engine.close();self.temp.cleanup()

    def count(self):
        return self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='community'").fetchone()[0]

    def clear(self):
        with self.engine.db:self.engine.db.execute("UPDATE jobs SET status='sent'")

    def test_fifteen_minute_round_persisted_and_no_duplicates(self):
        self.engine.poll(now=100)
        self.assertEqual(self.count(),0)
        self.engine.poll(now=999)
        self.assertEqual(self.count(),0)
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.config)
        self.engine.poll(now=1000)
        self.assertEqual(self.count(),3)
        self.engine.poll(now=1030)
        self.assertEqual(self.count(),3)
        self.clear()
        self.engine.poll(now=1900)
        self.assertEqual(self.count(),6)

    def test_empty_server_and_pending_round_do_not_repeat(self):
        self.engine.poll(now=100)
        self.fake.players=[];self.engine.poll(now=1000)
        self.assertEqual(self.count(),0)
        self.fake.players=[{'steamId':'76561190000000001','name':'fake'}]
        self.engine.poll(now=1030)
        self.assertEqual(self.count(),3)
        self.engine.poll(now=2000)
        self.assertEqual(self.count(),3)
        self.clear();self.engine.poll(now=2030)
        self.assertEqual(self.count(),6)

    def test_atomic_queue_overflow_does_not_advance_timer(self):
        self.engine.poll(now=100);self.clear()
        self.engine.state['lastGlobalRound']=1000
        self.engine.config['queue_limit']=2
        with self.assertRaises(ValueError):self.engine.poll(now=1000)
        self.assertEqual(self.count(),0)
        self.assertEqual(self.engine.state['lastCommunityRound'],100)
        self.engine.config['queue_limit']=1000
        self.engine.poll(now=1030)
        self.assertEqual(self.count(),3)

    def test_three_languages_wait_ten_seconds(self):
        self.engine.poll(now=100);self.clear()
        self.engine.state['lastGlobalRound']=1000
        self.engine.poll(now=1000)
        self.assertTrue(self.engine.send_one(now=1000))
        self.assertFalse(self.engine.send_one(now=1009))
        self.assertTrue(self.engine.send_one(now=1010))
        self.assertFalse(self.engine.send_one(now=1019))
        self.assertTrue(self.engine.send_one(now=1020))
        self.assertEqual(len(self.fake.sent),3)
