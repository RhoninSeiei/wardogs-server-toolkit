import json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
import test_monitor as f
from service.welcome_delivery import pack,MAX_AGE
ID='76561190000000001'
class Transport(f.FakeRcon):
 def __init__(self):super().__init__();self.calls=[];self.reply={'ok':True};self.failure=None;self.lookup_failure=False;self.leave_on_post=False
 def request(self,method,path,body=None):
  self.calls.append((method,path))
  if path=='/v1/players':
   if self.lookup_failure:raise TimeoutError()
   return {'players':self.players}
  if path.endswith('/message'):
   if self.leave_on_post:self.players=[]
   if self.failure:raise self.failure
   self.sent.append(body['message']);self.paths.append(path);return self.reply
  return super().request(method,path,body)
class WelcomeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.r=Transport();self.r.players=[f.player(identity=ID)];self.cfg=dict(f.app.DEFAULT_CONFIG,welcome_delivery_enabled=True);self.e=f.app.Engine(self.path,self.r,self.cfg);self.cache=self.path.parent/'local-cache/events.sqlite3';self.cache.parent.mkdir()
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute('CREATE TABLE source_snapshots(kind TEXT PRIMARY KEY,body TEXT,observed REAL)')
  self.roster([f.player(identity=ID)])
 def tearDown(self):self.e.close();self.tmp.cleanup()
 def roster(self,ps,observed=100):
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute('INSERT OR REPLACE INTO source_snapshots VALUES(?,?,?)',('players',json.dumps({'players':ps}),observed))
 def add(self,msg='welcome',created=100):
  with self.e.db:return self.e.add_job(msg,created,recipient=ID,kind='welcome')
 def test_real_rules_pack_without_loss(self):
  parts=[x.replace('{name}','X'*40) for x in self.cfg['rules']];self.assertEqual(len(pack(parts)),1);self.assertTrue(all(p in pack(parts)[0] for p in parts));self.assertLessEqual(len(pack(parts)[0]),200)
 def test_pack_preserves_boundaries_and_rejects_oversize(self):
  self.assertEqual(pack(['a'*150,'b'*100]),['a'*150,'b'*100]);self.assertRaises(ValueError,pack,['a'*201]);self.assertRaises(ValueError,pack,['bad\nline'])
 def test_poll_enqueues_compact_single_private(self):
  self.e.poll(now=100);self.assertEqual(self.e.db.execute("select count(*) from jobs where kind='welcome'").fetchone()[0],1);self.e.send_one(now=100);self.assertEqual(len(self.r.sent),1);self.assertTrue(self.r.paths[0].endswith('/message'))
 def test_fresh_cache_online_avoids_extra_game_read(self):
  j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'sent');self.assertEqual(self.r.calls,[('POST','/v1/players/'+ID+'/message')])
 def test_offline_cancels_only_same_old_batch(self):
  old=self.add();sibling=self.add('part2');new=self.add('new',created=102);self.roster([],observed=101);self.e.send_one(now=103)
  self.assertEqual(self.e.job(old)['status'],'cancelled');self.assertEqual(self.e.job(sibling)['status'],'cancelled');self.assertEqual(self.e.job(new)['status'],'queued');self.assertEqual(self.r.sent,[])
 def test_negative_snapshot_older_than_job_needs_live_check(self):
  self.roster([],observed=100);j=self.add(created=101);self.e.send_one(now=102);self.assertEqual(self.e.job(j)['status'],'sent');self.assertEqual(self.r.calls[0],('GET','/v1/players'))
 def test_stale_or_invalid_cache_falls_back_to_live(self):
  for ps in [[{'steamId':'invalid'}],[f.player(identity=ID),f.player(identity=ID)]]:
   self.roster(ps);j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'sent');self.e.next_send=0
  self.roster([],observed=1);j=self.add();self.e.send_one(now=102);self.assertEqual(self.e.job(j)['status'],'sent')
 def test_lookup_unavailable_defers_without_post_or_fake_sent(self):
  self.roster([],observed=1);self.r.lookup_failure=True;j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'queued');self.assertEqual(self.r.sent,[])
 def test_404_then_offline_cancels_remaining(self):
  self.r.failure=f.app.RconError(404);self.r.leave_on_post=True;j=self.add();other=self.add('part2');self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'cancelled');self.assertEqual(self.e.job(other)['status'],'cancelled');self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1)
 def test_404_online_retries_once_then_fails(self):
  self.r.failure=f.app.RconError(404);j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'queued');self.e.send_one(now=104);self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1);self.e.send_one(now=106);self.assertEqual(self.e.job(j)['status'],'failed');self.e.send_one(now=112);self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),2)
 def test_404_retry_can_recover(self):
  self.r.failure=f.app.RconError(404);j=self.add();self.e.send_one(now=101);self.r.failure=None;self.e.send_one(now=106);self.assertEqual(self.e.job(j)['status'],'sent');self.assertEqual(len(self.r.sent),1)
 def test_deferred_whisper_does_not_block_other_jobs(self):
  self.r.failure=f.app.RconError(404);self.add();self.e.send_one(now=101)
  with self.e.db:self.e.add_job('global',102)
  self.e.send_one(now=104);self.assertEqual(self.r.sent,['global'])
 def test_post_timeout_unknown_not_retried(self):
  self.r.failure=TimeoutError();j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'unknown');self.e.send_one(now=107);self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1)
 def test_pending_is_not_sent_and_not_retried(self):
  self.r.reply={'ok':True,'pending':True};j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'unknown');self.e.send_one(now=107);self.assertEqual(len(self.r.sent),1)
 def test_malformed_response_not_sent(self):
  self.r.reply={};j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'unknown')
 def test_429_respects_backoff_and_expiry_still_runs(self):
  self.r.failure=f.app.RconError(429,retry_after=300);j=self.add();self.e.send_one(now=101);self.assertGreaterEqual(self.e.next_send,401);self.e.send_one(now=100+MAX_AGE+1);self.assertEqual(self.e.job(j)['status'],'cancelled');self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1)
 def test_age_boundary_and_old_task_expiry(self):
  j=self.add();self.e.send_one(now=100+MAX_AGE);self.assertEqual(self.e.job(j)['status'],'sent');old=self.add(created=100);self.e.next_send=0;self.e.send_one(now=100+MAX_AGE+.01);self.assertEqual(self.e.job(old)['status'],'cancelled')
 def test_retry_survives_restart_without_early_send(self):
  self.r.failure=f.app.RconError(404);j=self.add();self.e.send_one(now=101);self.e.close();self.e=f.app.Engine(self.path,self.r,self.cfg);self.e.next_send=0;self.e.send_one(now=104);self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1);self.r.failure=None;self.e.send_one(now=106);self.assertEqual(self.e.job(j)['status'],'sent')
 def test_delivery_audit_distinguishes_online_404(self):
  self.r.failure=f.app.RconError(404);self.add();self.e.send_one(now=101);r=self.e.db.execute('select reason,http_status,online from welcome_delivery_attempts order by id desc limit 1').fetchone();self.assertEqual(r,('http_404_online_retry',404,1))

 def test_429_sender_deadline_persists_restart(self):
  self.r.failure=f.app.RconError(429,retry_after=30);j=self.add();self.e.send_one(now=101);self.e.close();self.e=f.app.Engine(self.path,self.r,self.cfg);self.assertGreaterEqual(self.e.next_send,131);self.assertFalse(self.e.send_one(now=110));self.r.failure=None;self.e.send_one(now=131);self.assertEqual(self.e.job(j)['status'],'sent')
 def test_live_preflight_429_honors_retry_after(self):
  self.roster([],observed=1);original=self.r.request
  def req(method,path,body=None):
   if path=='/v1/players':raise f.app.RconError(429,retry_after=45)
   return original(method,path,body)
  self.r.request=req;j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'queued');self.assertGreaterEqual(self.e.next_send,146);self.assertEqual(self.r.sent,[])
 def test_expiry_rechecked_after_slow_online_lookup(self):
  from unittest.mock import patch
  self.roster([],observed=1);clock=[279.];original=self.r.request
  def req(method,path,body=None):
   if path=='/v1/players':clock[0]=281.
   return original(method,path,body)
  self.r.request=req;j=self.add()
  with patch('time.time',side_effect=lambda:clock[0]):self.e.send_one()
  self.assertEqual(self.e.job(j)['status'],'cancelled');self.assertEqual(self.r.sent,[])
 def test_server_error_unknown_without_replay(self):
  self.r.failure=f.app.RconError(503);j=self.add();self.e.send_one(now=101);self.assertEqual(self.e.job(j)['status'],'unknown');self.e.send_one(now=110);self.assertEqual(sum(x[0]=='POST' for x in self.r.calls),1)
