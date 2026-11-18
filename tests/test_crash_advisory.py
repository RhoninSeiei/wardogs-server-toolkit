import copy,json,tempfile,unittest
from pathlib import Path
import test_monitor as f

MESSAGE='官方与服务商确认：复制实体超过65536可能触发游戏BUG。请优先建造大实体并清理已完工建筑。'
class AdvisoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.policy=self.path.parent/'crash-advisory-policy.json';self.write()
  self.r=f.FakeRcon();self.players(21);self.cfg=copy.deepcopy(f.app.DEFAULT_CONFIG);self.cfg.update(rules=[],global_rules=[]);self.e=f.app.Engine(self.path,self.r,self.cfg)
 def tearDown(self):self.e.close();self.tmp.cleanup()
 def write(self,enabled=True,message=MESSAGE):self.policy.write_text(json.dumps({'version':1,'enabled':enabled,'message':message}))
 def players(self,n):self.r.players=[f.player(identity=str(76561190000000000+i)) for i in range(n)]
 def jobs(self):return self.e.db.execute("SELECT message,status FROM jobs WHERE kind='crash_advisory' ORDER BY rowid").fetchall()
 def test_one_whole_chinese_message_only_above_twenty(self):
  self.players(20);self.e.poll(now=100);self.assertEqual(self.jobs(),[])
  self.players(21);self.e.poll(now=130);self.assertEqual(self.jobs(),[(MESSAGE,'queued')]);self.e.send_one(now=130);self.assertEqual(self.r.sent,[MESSAGE])
 def test_five_minutes_from_last_dispatch_no_queue_accumulation(self):
  self.e.poll(now=100);self.e.poll(now=110);self.assertEqual(len(self.jobs()),1);self.e.send_one(now=110)
  self.e.poll(now=409);self.assertEqual(len(self.jobs()),1);self.e.poll(now=410);self.assertEqual(len(self.jobs()),2)
 def test_player_count_rechecked_before_actual_send(self):
  self.e.poll(now=100);self.players(20);self.e.send_one(now=100);self.assertEqual(self.r.sent,[]);self.assertEqual(self.jobs()[0][1],'cancelled')
 def test_disabled_policy_cancels_queued_notice_without_reload(self):
  self.e.poll(now=100);self.write(False);self.e.send_one(now=100);self.assertEqual(self.r.sent,[]);self.assertEqual(self.jobs()[0][1],'cancelled');self.e.poll(now=500);self.assertEqual(len(self.jobs()),1)
 def test_bad_or_missing_policy_does_not_affect_normal_monitor(self):
  for text in ['{',json.dumps({'version':1,'enabled':True,'message':'x'*201})]:
   self.policy.write_text(text);self.e.poll(now=100);self.assertEqual(self.jobs(),[])
  self.policy.unlink();self.e.poll(now=200);self.assertEqual(self.jobs(),[])
 def test_roster_read_failure_prevents_broadcast(self):
  self.e.poll(now=100);self.r.fail=True;self.e.send_one(now=100);self.assertEqual(self.r.sent,[]);self.assertEqual(self.jobs()[0][1],'cancelled')
 def test_restart_retains_five_minute_interval(self):
  self.e.poll(now=100);self.e.send_one(now=100);self.e.close();self.e=f.app.Engine(self.path,self.r,self.cfg);self.e.poll(now=200);self.assertEqual(len(self.jobs()),1);self.e.poll(now=400);self.assertEqual(len(self.jobs()),2)
 def test_management_and_fun_keep_priority(self):
  self.e.poll(now=100)
  with self.e.db:self.e.add_job('管理',100,kind='management');self.e.add_job('趣味',100,kind='helicopter_fun')
  self.e.send_one(now=100);self.e.send_one(now=103);self.e.send_one(now=106);self.assertEqual(self.r.sent,['管理','趣味',MESSAGE])
 def test_ten_second_language_gap_preserved(self):
  self.e.poll(now=100);self.e.state['lastTimedAnnouncement']={'language':'en','finishedAt':100};self.assertFalse(self.e.send_one(now=109));self.assertTrue(self.e.send_one(now=110));self.assertEqual(self.r.sent,[MESSAGE])
 def test_message_replacement_cancels_old_queue(self):
  self.e.poll(now=100);self.write(message='更新通告');self.e.send_one(now=100);self.assertEqual(self.r.sent,[]);self.e.poll(now=130);self.e.send_one(now=130);self.assertEqual(self.r.sent,['更新通告'])
 def test_duplicate_roster_cannot_meet_threshold(self):
  self.r.players=[f.player()]*21;self.e.poll(now=100);self.assertEqual(self.jobs(),[])
 def test_unknown_broadcast_not_replayed_immediately(self):
  self.e.poll(now=100);old=self.r.request
  def request(method,path,body=None):
   if path=='/v1/broadcast':raise TimeoutError()
   return old(method,path,body)
  self.r.request=request;self.e.send_one(now=100);self.assertEqual(self.jobs()[0][1],'unknown');self.e.poll(now=130);self.assertEqual(len(self.jobs()),1)

 def pair(self):
  p=json.loads(self.policy.read_text());p['followup']='大锤普及前建议每局30分钟内结束，避免实体上限导致崩服。';self.policy.write_text(json.dumps(p));return p['followup']
 def test_two_whole_notices_in_order_and_no_duplicates(self):
  followup=self.pair();self.e.poll(now=100);self.e.poll(now=101);self.assertEqual(len(self.jobs()),2);self.e.send_one(now=100);self.e.send_one(now=103);self.assertEqual(self.r.sent,[MESSAGE,followup]);self.e.poll(now=402);self.assertEqual(len(self.jobs()),2);self.e.poll(now=403);self.assertEqual(len(self.jobs()),4)
 def test_hot_added_followup_completes_current_round_once(self):
  self.e.poll(now=100);self.e.send_one(now=100);followup=self.pair();self.e.poll(now=110);self.e.send_one(now=110);self.e.poll(now=140);self.assertEqual(self.r.sent,[MESSAGE,followup]);self.assertEqual(len(self.jobs()),2)
 def test_followup_rechecks_threshold(self):
  self.pair();self.e.poll(now=100);self.e.send_one(now=100);self.players(20);self.e.send_one(now=103);self.assertEqual(self.r.sent,[MESSAGE]);self.assertEqual(self.jobs()[1][1],'cancelled')
 def test_followup_not_sent_after_unknown_primary(self):
  self.pair();self.e.poll(now=100);old=self.r.request
  def request(method,path,body=None):
   if path=='/v1/broadcast':raise TimeoutError()
   return old(method,path,body)
  self.r.request=request;self.e.send_one(now=100);self.e.send_one(now=103);self.assertEqual([s for _,s in self.jobs()],['unknown','cancelled']);self.assertEqual(self.r.sent,[])
