from pathlib import Path
import tempfile
import unittest
from service.temporary_notice import Runner
from service.rcon import RconError

class Fake:
    def __init__(self):self.calls=[];self.error=None;self.before=None
    def request(self,method,path,body=None):
        if self.before:self.before()
        self.calls.append((method,path,body))
        if self.error:raise self.error
        return {'ok':True,'pending':False}

class TemporaryNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db'
        self.now=1000;self.fake=Fake()
        self.config={'startAt':1000,'endAt':3000,'intervalSeconds':900,'languageGapSeconds':10,'messages':['维护','[EN] Maintenance','[JA] メンテナンス']}
        self.runner=Runner(self.path,self.config,self.fake,lambda:self.now)
    def tearDown(self):self.runner.close();self.temp.cleanup()
    def test_first_immediate_language_spacing_and_fifteen_minutes(self):
        self.assertTrue(self.runner.step());self.now=1009;self.assertFalse(self.runner.step())
        self.now=1010;self.assertTrue(self.runner.step());self.now=1020;self.assertTrue(self.runner.step())
        self.now=1899;self.assertFalse(self.runner.step());self.now=1900;self.assertTrue(self.runner.step())
        self.assertEqual([c[2]['message'] for c in self.fake.calls],self.config['messages']+[self.config['messages'][0]])
    def test_no_dispatch_at_or_after_deadline(self):
        self.now=3000;self.assertFalse(self.runner.step());self.assertEqual(self.fake.calls,[])
    def test_deadline_crossing_during_claim_cancels_dispatch(self):
        ticks=iter([1000,3000,3000]);self.runner.clock=lambda:next(ticks)
        self.assertFalse(self.runner.step());self.assertEqual(self.fake.calls,[])
        self.assertEqual(self.runner.db.execute('SELECT status FROM deliveries').fetchone()[0],'cancelled')
    def test_restart_keeps_progress_and_does_not_backfill_old_rounds(self):
        self.runner.step();self.runner.close();self.now=2800
        self.runner=Runner(self.path,self.config,self.fake,lambda:self.now)
        self.runner.step()
        self.assertEqual(self.runner.db.execute('SELECT DISTINCT slot FROM deliveries ORDER BY slot').fetchall(),[(0,),(2,)])
    def test_unknown_is_not_replayed(self):
        self.fake.error=TimeoutError();self.runner.step();self.runner.close()
        self.runner=Runner(self.path,self.config,self.fake,lambda:self.now)
        self.now=1010;self.fake.error=None;self.runner.step()
        self.assertEqual(self.fake.calls[-1][2]['message'],self.config['messages'][1])
        self.assertEqual(self.runner.db.execute('SELECT status FROM deliveries WHERE slot=0 AND language=0').fetchone()[0],'unknown')
    def test_rate_limit_retries_only_after_retry_delay(self):
        self.fake.error=RconError(429,30);self.runner.step();self.now=1010;self.assertFalse(self.runner.step())
        self.now=1030;self.fake.error=None;self.runner.step()
        self.assertEqual(self.fake.calls[-1][2]['message'],'维护')
    def test_persisted_dispatch_boundary_and_expired_config_reuse(self):
        self.fake.before=lambda:self.assertEqual(self.runner.db.execute('SELECT status FROM deliveries').fetchone()[0],'sending')
        self.runner.step();self.runner.close();self.config['endAt']=4000
        with self.assertRaises(ValueError):Runner(self.path,self.config,self.fake,lambda:self.now)
        self.config['endAt']=3000;self.runner=Runner(self.path,self.config,self.fake,lambda:self.now)
    def test_invalid_language_spacing_and_message_lengths(self):
        for patch in [{'languageGapSeconds':3},{'messages':['x'*201,'en','ja']},{'endAt':999}]:
            config=dict(self.config);config.update(patch)
            with self.assertRaises(ValueError):Runner(':memory:',config,self.fake,lambda:self.now)


    def test_japanese_pause_cancels_delivery_without_network_or_attempt(self):
        self.runner.japanese_enabled=lambda:False
        self.runner.step();self.now=1010;self.runner.step();self.now=1020;self.runner.step()
        self.assertEqual(len(self.fake.calls),2)
        self.assertEqual(self.runner.db.execute('SELECT status,attempts FROM deliveries WHERE language=2').fetchone(),('cancelled',0))
        self.assertEqual(self.config['messages'][2],'[JA] メンテナンス')
    def test_japanese_reenable_never_replays_skipped_round(self):
        self.runner.japanese_enabled=lambda:False
        for now in [1000,1010,1020]:self.now=now;self.runner.step()
        self.runner.japanese_enabled=lambda:True;self.now=1030;self.assertFalse(self.runner.step())
        self.assertEqual(len(self.fake.calls),2)

if __name__=='__main__':unittest.main()
