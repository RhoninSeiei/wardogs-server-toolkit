import json,sqlite3,tempfile,time,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import test_monitor as f
from service.burst_guard import BurstGuard
SID='76561190000000001'
class Rcon(f.FakeRcon):
 def __init__(self):super().__init__();self.reserved=[];self.bans=[];self.fail_reserved=False;self.fail_ban=False;self.score=50
 def request(self,method,path,body=None):
  if path=='/v1/reserved-slots':
   if self.fail_reserved:raise TimeoutError()
   return {'reservedSlots':self.reserved}
  if path=='/v1/status':return {'factionScores':[{'name':'A','score':self.score},{'name':'B','score':0}]}
  if path=='/v1/bans':
   if method=='GET':return {'bans':[{'steamId':i} for i in self.bans]}
   if self.fail_ban:raise TimeoutError()
   self.bans.append(body['steamId']);self.players=[p for p in self.players if p['steamId']!=body['steamId']];return {'ok':True,'pending':False}
  return super().request(method,path,body)
class BurstTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.r=Rcon();cfg=dict(f.app.DEFAULT_CONFIG,rejoin_guard_enabled=True,burst_ban_enabled=True);self.e=f.app.Engine(self.path,self.r,cfg)
  self.cache=self.path.parent/'local-cache/events.sqlite3';self.cache.parent.mkdir()
  with closing(sqlite3.connect(self.cache)) as c,c:c.executescript("CREATE TABLE events(server TEXT,event_key TEXT,received REAL,type TEXT,body TEXT); CREATE TABLE export_meta(id INTEGER,generation TEXT);INSERT INTO export_meta VALUES(1,'gen');CREATE TABLE sync_state(kind TEXT,last_success REAL,error TEXT);INSERT INTO sync_state VALUES('events',1000,NULL);")
  with closing(sqlite3.connect(self.cache)) as c,c:
   c.execute('CREATE TABLE source_snapshots(kind TEXT PRIMARY KEY,body TEXT,observed REAL)');c.execute('INSERT INTO source_snapshots VALUES(?,?,?)',('status',json.dumps({'factionScores':[{'name':'A','score':50},{'name':'B','score':0}]}),1000))
  self.b=self.e.burst_guard;self.b.tick(now=1000)
 def tearDown(self):self.e.close();self.tmp.cleanup()
 def add(self,key,t,sid=SID,match='m',victim='76561190000000002',cause='Id.Item.TAR21',received=1001,teamkill=True,tags=None):
  o={'type':'killed','eventId':key,'killerSteamId':sid,'killerName':'Fixture','victimSteamId':victim,'victimName':'Victim','eventTime':t,'matchId':match,'cause':cause,'teamkill':teamkill,'contextTags':tags or []}
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute('INSERT INTO events VALUES(?,?,?,?,?)',('server',key,received,'killed',json.dumps(o)))
 def detect(self):self.b.ingest(now=1002)
 def count(self):return self.e.db.execute('select count(*) from burst_actions').fetchone()[0]
 def three(self,times=(10,12,15),**kw):
  for i,t in enumerate(times):self.add(str(i),t,**kw)
  self.detect()
 def test_boundary_includes_three_and_teamkills(self):self.three();self.assertEqual(self.count(),1)
 def test_more_than_five_seconds_not_eligible(self):self.three((10,12,15.001));self.assertEqual(self.count(),0)
 def test_enemy_explosives_and_simultaneous_events_count(self):self.three((10,10,10),cause='Id.Item.C4Explosive',teamkill=False);self.assertEqual(self.count(),1)
 def test_self_kills_excluded(self):self.three(victim=SID);self.assertEqual(self.count(),0)
 def test_distinct_match_and_player_not_combined(self):
  self.add('a',10);self.add('b',11,match='other');self.add('c',12,sid='76561190000000003');self.detect();self.assertEqual(self.count(),0)
 def test_duplicate_event_not_counted(self):
  for t in (10,11,12):self.add('same',t)
  self.detect();self.assertEqual(self.count(),0)
 def test_out_of_order_and_restart_retains_window(self):
  self.add('a',14);self.add('b',10);self.detect();self.b=BurstGuard(self.e,self.cache);self.add('c',12);self.detect();self.assertEqual(self.count(),1)
 def test_historical_rows_before_first_arm_skipped(self):
  self.e.db.execute('DELETE FROM burst_meta');self.e.db.commit();self.three(received=900);self.assertEqual(self.count(),0)
 def test_delayed_old_received_event_skipped(self):self.three(received=999);self.assertEqual(self.count(),0)
 def test_generation_reset_rearms_without_replay(self):
  self.three();self.e.db.execute('delete from burst_actions');self.e.db.commit()
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute("update export_meta set generation='new'")
  self.detect();self.assertEqual(self.count(),0)
 def test_stale_feed_fails_closed(self):
  self.three()
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute('update sync_state set last_success=900')
  self.assertRaises(ValueError,self.b.tick,now=1002);self.assertEqual(self.r.bans,[])
 def test_fresh_reserved_exemption(self):
  self.three();self.r.reserved=[SID];self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertEqual(self.r.bans,[]);self.assertEqual(self.e.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
 def test_reserved_failure_no_restriction_or_ban(self):
  self.three();self.r.fail_reserved=True;self.b.process_one(now=1002);self.assertEqual(self.r.bans,[]);self.assertEqual(self.e.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
 def test_online_native_ban_verified_and_announcements(self):
  self.three();self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertIn(SID,self.r.bans);self.assertEqual(self.e.kick_store.native_ban_state(SID),'confirmed');self.assertEqual(self.e.db.execute("select count(*) from jobs where kind='management'").fetchone()[0],3)
 def test_offline_queue_not_fake_removal(self):
  self.three();self.b.process_one(now=1002);self.assertEqual(self.e.kick_store.native_ban_state(SID),'pending');self.assertEqual(self.e.db.execute('select status from burst_actions').fetchone()[0],'offline');self.assertEqual(self.e.kick_store.db.execute('select count(*) from confirmed_kicks').fetchone()[0],0)
 def test_uncertain_native_ban_not_replayed(self):
  self.three();self.r.players=[f.player(identity=SID)];self.r.fail_ban=True;self.b.process_one(now=1002);self.assertEqual(self.e.db.execute('select status from burst_actions').fetchone()[0],'unknown');self.b.process_one(now=1100);self.assertEqual(self.r.bans,[])
 def test_recovery_does_not_replay_dispatch(self):
  self.three();self.e.db.execute("update burst_actions set status='dispatching'");self.e.db.commit();BurstGuard(self.e,self.cache);self.assertEqual(self.e.db.execute('select status from burst_actions').fetchone()[0],'unknown')
 def test_existing_unknown_target_not_bypassed(self):
  self.three();self.e.db.execute("insert into moderation_targets values(?,?,'unknown',NULL)",(SID,'prior'));self.e.db.commit();self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertEqual(self.r.bans,[])

 def test_no_repeat_action_after_more_kills_same_match(self):
  self.three();self.add('fourth',15.1);self.detect();self.assertEqual(self.count(),1)
 def test_invalid_event_fields_ignored(self):
  self.three((True,float('nan'),float('inf')));self.assertEqual(self.count(),0)
 def test_policy_disabled_no_actions(self):
  self.e.config['burst_ban_enabled']=False
  for i in range(3):self.add(str(i),10)
  self.b.tick(now=1002);self.assertEqual(self.count(),0)
 def test_behavior_exemption(self):
  self.three()
  with self.e.kick_store.db:self.e.kick_store.db.execute('INSERT INTO behavior_players VALUES(?,?,1,0,0,0,0,0)',(SID,'Fixture'))
  self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertEqual(self.r.bans,[]);self.assertEqual(self.e.db.execute('select status from burst_actions').fetchone()[0],'exempt')
 def test_deferred_request_recovers_after_preflight_failure(self):
  self.three();self.r.fail_reserved=True;self.b.process_one(now=1002);self.r.fail_reserved=False;self.r.players=[f.player(identity=SID)];self.b.process_one(now=1063);self.assertEqual(self.r.bans,[SID])

 def score(self,value,observed=1000):
  with closing(sqlite3.connect(self.cache)) as c,c:c.execute('UPDATE source_snapshots SET body=?,observed=? WHERE kind="status"',(json.dumps({'factionScores':[{'name':'A','score':value}]}),observed))
 def test_all_friendly_c4_excluded(self):self.three(cause='Id.Item.C4Explosive');self.assertEqual(self.count(),0)
 def test_all_friendly_vehicle_weapon_excluded(self):self.three(cause='Id.Vehicle.WeaponExtension.STN_03.MainBarrel');self.assertEqual(self.count(),0)
 def test_all_friendly_helicopter_excluded(self):self.three(cause='Vehicle.Variant.Air.Rotary.Transport');self.assertEqual(self.count(),0)
 def test_all_friendly_rifle_still_triggers(self):self.three();self.assertEqual(self.count(),1)
 def test_mixed_friendly_enemy_c4_triggers(self):
  self.add('a',10,cause='Id.Item.C4Explosive');self.add('b',11,cause='Id.Item.C4Explosive');self.add('c',12,cause='Id.Item.C4Explosive',teamkill=False);self.detect();self.assertEqual(self.count(),1)
 def test_more_than_three_does_not_hide_enemy_after_friendly_window(self):
  for i in range(3):self.add(str(i),10+i,cause='Id.Item.C4Explosive')
  self.add('enemy',13,cause='Id.Item.C4Explosive',teamkill=False);self.detect();self.assertEqual(self.count(),1)
 def test_high_score_skips_and_advances_without_replay(self):
  self.score(99);self.three();self.assertEqual(self.count(),0);self.score(50);self.detect();self.assertEqual(self.count(),0)
 def test_score_exact_98_allowed(self):self.score(98);self.three();self.assertEqual(self.count(),1)
 def test_score_unknown_and_stale_stop_detection(self):
  for value,observed in [(None,1000),(50,900)]:
   self.score(value,observed);self.assertRaises(ValueError,self.detect)
 def test_score_changes_before_action_no_ban(self):
  self.three();self.r.score=99;self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertEqual(self.r.bans,[]);self.assertEqual(self.e.db.execute('select status from burst_actions').fetchone()[0],'ignored_high_score')

 def explosion(self,key,t=10,teamkill=False,cause='Vehicle.Variant.Air.Rotary.ROT_04.Default',victim='76561190000000002'):
  self.add(key,t,teamkill=teamkill,cause=cause,victim=victim,tags=['Meta.Progression.Context.Player.KillContext.VehicleExplosion'])
 def test_mixed_team_helicopter_explosion_exempt(self):
  for i in range(4):self.explosion(str(i),teamkill=i%2==0)
  self.explosion('self',victim=SID);self.detect();self.assertEqual(self.count(),0)
 def test_mixed_team_land_vehicle_explosion_exempt(self):
  for i in range(3):self.explosion(str(i),cause='Vehicle.Variant.Land.Wheeled.Kodiak.Default',teamkill=i==0)
  self.detect();self.assertEqual(self.count(),0)
 def test_explosion_tag_applies_to_c4_and_rifle_causes(self):
  for i,cause in enumerate(['Id.Item.C4Explosive','Id.Item.TAR21','Id.Item.C4Explosive']):self.explosion(str(i),cause=cause)
  self.detect();self.assertEqual(self.count(),0)
 def test_two_rifle_kills_plus_explosion_not_three(self):
  self.add('a',10);self.add('b',11);self.explosion('c',12);self.detect();self.assertEqual(self.count(),0)
 def test_three_rifle_kills_plus_explosion_evidence_only_rifles(self):
  self.explosion('x',10)
  for i in range(3):self.add(str(i),11+i)
  self.detect();self.assertEqual(self.count(),1)
  evidence=json.loads(self.e.db.execute('select evidence from burst_actions').fetchone()[0]);self.assertEqual(len(evidence),3);self.assertTrue(all(not e['contextTags'] for e in evidence))
 def test_untagged_enemy_vehicle_gunfire_still_counts(self):
  self.three(cause='Id.Vehicle.WeaponExtension.STN_03.MainBarrel',teamkill=False);self.assertEqual(self.count(),1)
 def test_retained_explosion_events_not_reused_in_late_window(self):
  self.add('a',10);self.add('b',14);self.detect()
  body={'cause':'Vehicle.Variant.Air.Rotary.ROT_04.Default','contextTags':['Meta.Progression.Context.Player.KillContext.VehicleExplosion'],'teamkill':False}
  with self.e.db:self.e.db.execute('INSERT INTO burst_events VALUES(?,?,?,?,?,?,?)',('server','old-explosion',SID,'m',12,1001,json.dumps(body)))
  self.explosion('late',11);self.add('outside',19.1);self.detect();self.assertEqual(self.count(),0)
 def test_queued_explosion_evidence_cancelled_without_writes(self):
  self.three();row=self.e.db.execute('select id,evidence from burst_actions').fetchone();evidence=json.loads(row[1])
  evidence[0]['contextTags']=['Meta.Progression.Context.Player.KillContext.VehicleExplosion']
  with self.e.db:self.e.db.execute('update burst_actions set evidence=? where id=?',(json.dumps(evidence),row[0]))
  self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002)
  self.assertEqual(self.r.bans,[]);self.assertEqual(self.e.db.execute('select status,error from burst_actions').fetchone(),('cancelled','vehicle_explosion_excluded'))
  self.assertEqual(self.e.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
  self.assertEqual(self.e.db.execute('select count(*) from jobs').fetchone()[0],0)
 def test_queued_four_events_with_three_rifles_still_executes(self):
  self.three();row=self.e.db.execute('select id,evidence from burst_actions').fetchone();evidence=json.loads(row[1]);extra=dict(evidence[0],contextTags=['Meta.Progression.Context.Player.KillContext.VehicleExplosion']);evidence.append(extra)
  with self.e.db:self.e.db.execute('update burst_actions set evidence=? where id=?',(json.dumps(evidence),row[0]))
  self.r.players=[f.player(identity=SID)];self.b.process_one(now=1002);self.assertEqual(self.r.bans,[SID])
