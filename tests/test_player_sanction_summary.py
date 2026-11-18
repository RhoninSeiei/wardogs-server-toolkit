import json,sqlite3,time,threading,http.client,unittest
from http.server import ThreadingHTTPServer
from contextlib import closing
from unittest.mock import patch
import test_admin_history as h
from service.player_sanction_summary import PlayerSanctionSummary

class SummaryTests(unittest.TestCase):
 roster=h.HistoryTests.roster
 setUp=h.HistoryTests.setUp
 tearDown=h.HistoryTests.tearDown
 def payload(self,now=1000,sid=h.ID):return {'steamId':sid,'actor':h.admin_payload()['actor'],'requestedAt':now}
 def query(self,payload=None,now=1000):return self.engine.player_sanction_summary.query(payload or self.payload(now),now=now)
 def ban(self,at=100,key='ban1',sid=h.ID):self.engine.kick_store.restrict(sid,'Fixture','fixture','manual',at,action_id=key)
 def unban(self,at=200,key='u1',native='already_unbanned',local='released',status='completed',outcome='unbanned',native_started=0,sid=h.ID):
  guard=self.engine.kick_store;guard.release(sid,'fixture',at-1,action_id='admin-unban:'+key)
  p=h.admin_payload(now=at-2,request_id=key,identity=sid)
  with self.engine.db:self.engine.db.execute('''INSERT INTO admin_unbans(id,request_id,steam_id,payload_hash,payload_json,status,unban_status,native_status,local_status,created,updated,native_started,native_verified_at,released_at)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(key,key,sid,key,json.dumps(p),status,outcome,native,local,at-2,at,native_started,at,at-1))
 def test_empty_is_lower_bound_not_claim_never(self):
  r=self.query();self.assertEqual((r['banCount'],r['unbanCount'],r['unknownItems']),(0,0,0));self.assertTrue(r['complete']);self.assertTrue(r['countsAreLowerBounds']);self.assertFalse(r['historyComplete']);self.assertIsNone(r['recordsSince'])
  self.assertEqual(r['countingRule'],'verified_sanction_cycles_v1');self.assertEqual(r['coverage'],'retained_local_sanction_ledger');self.assertEqual(self.fake.calls,[]);self.assertEqual(self.engine.queue_depth(),0)
 def test_same_cycle_duplicate_local_registrations(self):
  self.ban();self.ban(110,'ban2');self.ban(120,'ban3');self.assertEqual(self.query()['banCount'],1)
 def test_rejoin_kicks_do_not_add_cycles(self):
  self.ban()
  for i in range(5):self.engine.kick_store.confirm(h.ID,'Fixture','same','rejoin','permanent',110+i,event_id='rejoin'+str(i),continuation_event_id='ban1' if i==0 else 'rejoin'+str(i-1))
  self.assertEqual(self.query()['banCount'],1)
 def test_two_bans_only_after_complete_actual_release(self):
  self.ban();self.unban();self.ban(300,'second');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(2,1));self.assertEqual(r['alerts'],{'repeatedBan':True,'repeatedUnban':False})
 def test_two_complete_actual_unbans_alert(self):
  self.ban();self.unban();self.ban(300,'second');self.unban(400,'u2');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(2,2));self.assertTrue(all(r['alerts'].values()))
 def test_duplicate_unban_same_id_idempotent_receipt(self):
  self.ban();self.unban();self.engine.kick_store.release(h.ID,'retry',300,action_id='admin-unban:u1');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1))
 def test_already_unbanned_no_restriction_is_not_unban(self):
  self.unban(local='not_restricted');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(0,0))
 def test_repeat_verification_after_release_not_new_count(self):
  self.ban();self.unban();self.unban(300,'verify',local='already_released');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1))
 def test_partial_release_does_not_split_ban_cycle(self):
  self.ban();self.unban(native='unknown',status='partial',outcome='partial');self.ban(300,'second');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,0));self.assertGreater(r['unknownItems'],0)
 def test_failed_unban_does_not_split(self):
  self.ban();self.unban(status='failed',outcome='failed',native='failed');self.ban(300,'second');self.assertEqual(self.query()['banCount'],1)
 def test_native_only_full_release_proves_cycle(self):
  self.unban(native='unbanned',local='not_restricted',native_started=1);r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1))
 def test_native_only_two_nonoverlapping_releases(self):
  self.unban(native='unbanned',local='not_restricted',native_started=1);self.unban(400,'u2',native='unbanned',local='already_released',native_started=1);r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(2,2))
 def test_native_creation_and_local_registration_same_cycle(self):
  self.ban()
  self.native_operation('b1',150)
  r=self.query();self.assertEqual(r['banCount'],1)
 def native_operation(self,key,at,native='banned',local='not_attempted',status='completed'):
  receipt={'nativeBanStatus':native,'localRestrictionStatus':local,'nativeVerifiedAt':at,'localVerifiedAt':None}
  with self.engine.db:self.engine.db.execute('INSERT INTO admin_bans VALUES(?,?,?,?,?,?,?,?,?,NULL)',(key,key,h.ID,key,json.dumps(h.admin_payload(now=at-1,request_id=key)),status,at-1,at,json.dumps(receipt)))
 def test_native_completion_after_unban_is_second_cycle(self):
  self.ban();self.unban();self.native_operation('b1',300);self.assertEqual(self.query()['banCount'],2)
 def test_native_already_banned_observation_not_dated_new_cycle(self):
  self.ban();self.unban();self.native_operation('b1',300,native='already_banned');self.assertEqual(self.query()['banCount'],1)
 def test_failed_unknown_request_no_effect_does_not_count(self):
  self.native_operation('b1',300,native='unknown',status='unknown');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(0,0));self.assertGreater(r['unknownItems'],0)
 def test_unknown_or_unconfirmed_kick_not_used(self):
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('INSERT INTO confirmed_kicks VALUES(?,?,?,?,?,?,?,?)',('unknown',h.ID,'Fixture','fixture','manual',None,100,'permanent'))
  r=self.query();self.assertEqual(r['banCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_confirmed_permanent_kick_root_counts_once(self):
  self.engine.kick_store.confirm(h.ID,'Fixture','fixture','manual','permanent',100,event_id='kick1');self.assertEqual(self.query()['banCount'],1)
 def test_legacy_scope_and_import_not_assumed_permanent(self):
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('INSERT INTO confirmed_kicks VALUES(?,?,?,?,?,?,?,?)',('legacy',h.ID,'Fixture','fixture','verified_conversation_receipt',None,100,'old-round'))
  self.assertEqual(self.query()['banCount'],0)
 def test_incomplete_native_release_not_counted_without_started(self):
  self.unban(native='unbanned',local='not_restricted',native_started=0);r=self.query();self.assertEqual(r['unbanCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_unlinked_release_receipt_not_trusted(self):
  self.ban();self.unban()
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('delete from restriction_releases')
  r=self.query();self.assertEqual(r['unbanCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_bad_previous_snapshot_is_unknown_not_success(self):
  self.ban();self.unban()
  with self.engine.kick_store.db:self.engine.kick_store.db.execute("update restriction_releases set previous_state='invalid'")
  r=self.query();self.assertEqual(r['unbanCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_cross_player_receipt_rejected(self):
  self.ban();self.unban()
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('update restriction_releases set steam_id=?',(h.OTHER,))
  self.assertEqual(self.query()['unbanCount'],0)
 def test_other_player_bans_and_unbans_not_exposed(self):
  self.ban(sid=h.OTHER);self.unban(sid=h.OTHER);r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(0,0));self.assertNotIn(h.OTHER,json.dumps(r))
 def test_actual_local_release_without_native_verification_not_unban(self):
  self.ban();self.engine.kick_store.release(h.ID,'manual local only',200);r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,0));self.assertGreater(r['unknownItems'],0)
 def test_stale_same_origin_release_not_second_unban(self):
  self.ban();self.unban()
  self.engine.kick_store.db.execute("INSERT INTO rejoin_restrictions VALUES(?,?,'permanent',?,?,?,'active')",(h.ID,'ban1','Fixture','replayed',300));self.engine.kick_store.db.commit()
  self.unban(400,'u2');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1))
 def test_strict_contract_role_scope_and_time(self):
  for change in [{'steamId':'Fixture'},{'limit':20},{'steamId':'７'*17}]:
   with self.assertRaises(h.ContractError):self.query(dict(self.payload(),**change))
  for field,value in [('role','member'),('groupId','other'),('botId','other'),('platform','other')]:
   p=self.payload();p['actor'][field]=value
   with self.assertRaises(h.ContractError) as e:self.query(p)
   self.assertEqual(e.exception.status,403)
  with self.assertRaises(h.ContractError) as e:self.query(self.payload(879))
  self.assertEqual(e.exception.status,422)
 def test_missing_storage_fails_503_no_fake_zero(self):
  self.engine.player_sanction_summary.guard_path=self.path.parent/'missing.db'
  with self.assertRaises(h.ContractError) as e:self.query()
  self.assertEqual(e.exception.status,503)
 def test_overlapping_and_equal_boundary_verifications_dedup(self):
  self.unban(native='unbanned',local='not_restricted',native_started=1)
  self.unban(201,'u2',native='unbanned',local='already_released',native_started=1)
  r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1));self.assertGreater(r['unknownItems'],0)
 def test_partial_then_verified_absence_can_bound_later_new_ban(self):
  self.ban();self.unban(native='unknown',status='partial',outcome='partial');self.unban(250,'verify',local='already_released');self.ban(300,'new')
  r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(2,0))
 def test_rejoin_after_unban_never_creates_a_dated_cycle(self):
  self.ban();self.unban();self.engine.kick_store.confirm(h.ID,'Fixture','old','rejoin','permanent',300,event_id='repeat')
  r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(1,1))
 def test_historical_native_observation_only_proves_one_undated_cycle(self):
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('insert into ban_statistics_evidence values(?,?,NULL,?)',('native:'+h.ID,h.ID,100))
  r=self.query();self.assertEqual(r['banCount'],1);self.assertIsNone(r['recordsSince']);self.assertGreater(r['unknownItems'],0)
 def test_bad_native_verified_time_does_not_count(self):
  self.native_operation('bad',300)
  with self.engine.db:self.engine.db.execute('update admin_bans set receipt=?',(json.dumps({'nativeBanStatus':'banned','nativeVerifiedAt':None}),))
  r=self.query();self.assertEqual(r['banCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_unknown_kick_does_not_erase_committed_local_ban(self):
  self.ban()
  with self.engine.kick_store.db:self.engine.kick_store.db.execute("update rejoin_restrictions set status='unknown'")
  r=self.query();self.assertEqual(r['banCount'],1);self.assertGreater(r['unknownItems'],0)
 def test_per_table_limit_refuses_partial_count(self):
  self.ban()
  import service.player_sanction_summary as module
  with patch.object(module,'MAX_ROWS',0),self.assertRaises(h.ContractError) as e:self.query()
  self.assertEqual(e.exception.code,'player_sanction_summary_too_large')
 def test_readonly_no_table_ledger_or_job_changes(self):
  self.ban();self.unban()
  before=self.engine.db.total_changes;guard=self.engine.kick_store.db.total_changes
  self.query();self.query()
  self.assertEqual(self.engine.db.total_changes,before);self.assertEqual(self.engine.kick_store.db.total_changes,guard);self.assertEqual(self.fake.calls,[])
 def test_same_snapshot_when_other_connection_commits(self):
  self.ban()
  import service.player_sanction_summary as module
  original=sqlite3.connect
  base=original(self.path.resolve().as_uri()+'?mode=ro',uri=True)
  class Wrapped:
   def execute(inner,sql,*args):
    cursor=base.execute(sql,*args)
    if sql.startswith('SELECT (SELECT count(*)'):
     rows=cursor.fetchall()
     with closing(original(self.path.parent/'kick_guard.sqlite3')) as writer,writer:
      writer.execute('delete from rejoin_restrictions');writer.execute('delete from permanent_ban_actions')
     class Result:
      def fetchone(inner):return rows[0]
     return Result()
    return cursor
   def set_progress_handler(inner,*args):return base.set_progress_handler(*args)
   @property
   def row_factory(inner):return base.row_factory
   @row_factory.setter
   def row_factory(inner,value):base.row_factory=value
   def close(inner):base.close()
  with patch.object(module.sqlite3,'connect',return_value=Wrapped()):r=self.query()
  self.assertEqual(r['banCount'],1)
 def test_future_effective_time_not_assumed_valid(self):
  self.ban(2000);self.unban(300);r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(0,0));self.assertGreater(r['unknownItems'],0)
 def test_many_registrations_and_rejoins_with_five_complete_cycles(self):
  for cycle in range(5):
   at=20+cycle*150
   for repeat in range(3):self.ban(at+repeat,'ban-'+str(cycle)+'-'+str(repeat))
   self.unban(at+100,'u-'+str(cycle))
  self.ban(900,'last');r=self.query();self.assertEqual((r['banCount'],r['unbanCount']),(6,5))
 def test_unrecognized_orphan_action_not_assumed_effective(self):
  with self.engine.kick_store.db:self.engine.kick_store.db.execute('insert into permanent_ban_actions values(?,?,?,?,?,?)',('untrusted',h.ID,'Fixture','unknown','unrecognized_import',100))
  r=self.query();self.assertEqual(r['banCount'],0);self.assertGreater(r['unknownItems'],0)
 def test_http_admin_only(self):
  now=time.time();server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'));thread=threading.Thread(target=server.serve_forever);thread.start()
  try:
   for token,status in [('query',401),('report',401),('admin',200)]:
    conn=http.client.HTTPConnection(*server.server_address,timeout=3);conn.request('POST','/v1/moderation/player-sanction-summary',json.dumps(self.payload(now)),{'Authorization':'Bearer '+token,'Content-Type':'application/json'});resp=conn.getresponse();body=json.loads(resp.read());conn.close();self.assertEqual(resp.status,status,body)
  finally:server.shutdown();server.server_close();thread.join()
