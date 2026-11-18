import copy,http.client,json,sqlite3,tempfile,threading,unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import test_monitor as f
from service.kick_guard import KickStore
from service.ban_statistics import BanStatistics,StatisticsUnavailable
ID='76561190000000001';OTHER='76561190000000002'
class StatisticsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'guard.db';self.now=200000.
  self.clock=patch('time.time',side_effect=lambda:self.now);self.clock.start();self.g=KickStore(self.path)
 def tearDown(self):self.g.close();self.clock.stop();self.tmp.cleanup()
 def stats(self):return self.g.ban_statistics(now=self.now)
 def test_empty_and_exact_contract(self):
  s=self.stats();self.assertEqual(set(s),{'observedAt','countingRule','totalPlayers','last24HoursPlayers','last12HoursPlayers','unknownTimePlayers','recordsSince','historyComplete'});self.assertEqual(s['totalPlayers'],0);self.assertEqual(s['countingRule'],'unique_first_effective_ban_v1');self.assertFalse(s['historyComplete']);self.assertIsNone(s['recordsSince'])
 def test_overlapping_sources_and_repeated_removals_count_once(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now-100,action_id='first');self.g.confirm(ID,'Fixture','reason','monitor','permanent',self.now-50);self.g.request_native_bans([ID],self.now-40);self.g.native_ban_update(ID,'confirmed');self.g.confirm(ID,'Fixture','reason','rejoin','permanent',self.now)
  s=self.stats();self.assertEqual((s['totalPlayers'],s['last12HoursPlayers'],s['unknownTimePlayers']),(1,1,0))
 def test_repeated_kick_does_not_refresh_first_time(self):
  self.g.confirm(ID,'Fixture','reason','monitor','permanent',self.now-90000);self.g.confirm(ID,'Fixture','reason','rejoin','permanent',self.now);self.assertEqual(self.stats()['last24HoursPlayers'],0)
 def test_rolling_boundaries_and_cutoff_inclusive(self):
  for i,age in enumerate([0,43200,43200.001,86400,86400.001,-1]):self.g.restrict(str(76561190000000100+i),'Fixture','reason','manual',self.now-age)
  s=self.stats();self.assertEqual((s['totalPlayers'],s['last24HoursPlayers'],s['last12HoursPlayers']),(5,4,2))
 def test_unknown_history_dominates_later_known(self):
  self.g.confirm(ID,'Fixture','reason','verified_history',None,self.now-100,historical=True);self.g.confirm(ID,'Fixture','reason','rejoin','permanent',self.now)
  s=self.stats();self.assertEqual((s['totalPlayers'],s['unknownTimePlayers'],s['last24HoursPlayers']),(1,1,0))
 def test_offline_restriction_counts_without_kick(self):
  self.g.restrict(ID,'Fixture','reason','admin_offline',self.now);self.assertEqual(self.stats()['totalPlayers'],1);self.assertEqual(self.g.db.execute('select count(*) from confirmed_kicks').fetchone()[0],0)
 def test_pending_unknown_and_failed_native_only_not_effective(self):
  for i,status in enumerate(['pending','dispatching','unknown','failed']):
   with self.g.db:self.g.db.execute('insert into native_ban_sync values(?,?,?,?,NULL)',(str(76561190000000100+i),self.now,status,self.now))
  self.assertEqual(self.stats()['totalPlayers'],0)
 def test_first_native_confirmation_is_persisted_without_updated_at_refresh(self):
  with self.g.db:self.g.db.execute("insert into native_ban_sync values(?,?,'pending',?,NULL)",(ID,self.now,self.now))
  self.g.native_ban_update(ID,'confirmed');self.now+=90000;self.g.native_ban_update(ID,'confirmed');self.g.close();self.g=KickStore(self.path)
  s=self.stats();self.assertEqual(s['totalPlayers'],1);self.assertEqual(s['last24HoursPlayers'],0);self.assertEqual(s['unknownTimePlayers'],0)
 def test_legacy_confirmed_native_unknown_not_import_time(self):
  with self.g.db:self.g.db.execute("insert into native_ban_sync values(?,?,'confirmed',?,NULL)",(ID,self.now-100,self.now-1))
  self.g.close();self.g=KickStore(self.path);s=self.stats();self.assertEqual((s['totalPlayers'],s['unknownTimePlayers'],s['last24HoursPlayers']),(1,1,0))
 def test_future_native_confirmation_not_counted_until_cutoff(self):
  with self.g.db:self.g.db.execute("insert into native_ban_sync values(?,?,'pending',?,NULL)",(ID,self.now,self.now))
  self.now+=1;self.g.native_ban_update(ID,'confirmed');self.now-=1;self.assertEqual(self.stats()['totalPlayers'],0)
 def test_first_receipt_survives_status_change_and_restart(self):
  with self.g.db:self.g.db.execute("insert into native_ban_sync values(?,?,'pending',?,NULL)",(ID,self.now,self.now))
  self.g.native_ban_update(ID,'confirmed');self.g.native_ban_update(ID,'unknown');self.g.close();self.g=KickStore(self.path);self.assertEqual(self.stats()['totalPlayers'],1)
 def test_unlinked_legacy_restriction_time_unknown(self):
  with self.g.db:self.g.db.execute("insert into rejoin_restrictions values(?,?,'permanent',?,?,?,'active')",(ID,'missing','Fixture','reason',self.now))
  s=self.stats();self.assertEqual(s['unknownTimePlayers'],1);self.assertEqual(s['last24HoursPlayers'],0)
 def test_legacy_temporary_scope_not_trusted_as_permanent_start(self):
  with self.g.db:self.g.db.execute('insert into confirmed_kicks values(?,?,?,?,?,?,?,?)',('old',ID,'Fixture','reason','monitor',self.now-100,self.now,'old-map'))
  self.g.close();self.g=KickStore(self.path);self.assertEqual(self.stats()['unknownTimePlayers'],1)
 def test_corrupt_missing_source_returns_error_not_zero(self):
  self.g.db.execute('drop table confirmed_kicks');self.assertRaises(sqlite3.Error,self.stats)
 def test_invalid_id_or_time_in_ledger_fails_closed(self):
  with self.g.db:self.g.db.execute('insert into permanent_ban_actions values(?,?,?,?,?,?)',('bad','invalid','Fixture','reason','manual',self.now))
  self.assertRaises(ValueError,self.stats)
 def test_external_current_native_unknown_and_deduplicated(self):
  self.g.observe_native_bans([{'steamId':ID,'bannedAtUtc':'0001-01-01T00:00:00Z'}],self.now);self.g.observe_native_bans([{'steamId':ID}],self.now);self.assertEqual(self.stats()['totalPlayers'],1);self.assertEqual(self.stats()['unknownTimePlayers'],1)
 def test_unknown_native_does_not_override_known_native_first_confirmation(self):
  with self.g.db:self.g.db.execute("insert into native_ban_sync values(?,?,'pending',?,NULL)",(ID,self.now,self.now))
  self.g.native_ban_update(ID,'confirmed');self.g.observe_native_bans([{'steamId':ID}],self.now);self.assertEqual(self.stats()['unknownTimePlayers'],0)
 def test_snapshot_clock_read_once_and_count_invariants(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now);self.g.confirm(OTHER,'Fixture','reason','history',None,self.now,historical=True)
  with patch('service.ban_statistics.time.time',return_value=self.now) as clock:s=self.g.ban_statistics();self.assertEqual(clock.call_count,1)
  self.assertGreaterEqual(s['totalPlayers'],s['last24HoursPlayers']);self.assertGreaterEqual(s['last24HoursPlayers'],s['last12HoursPlayers']);self.assertLessEqual(s['unknownTimePlayers'],s['totalPlayers']-s['last24HoursPlayers'])

 def test_linked_restriction_retry_time_does_not_become_unknown_or_recent(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now-90000,action_id='old')
  self.g.failed(ID,'permanent',self.now,False)
  s=self.stats();self.assertEqual((s['totalPlayers'],s['last24HoursPlayers'],s['unknownTimePlayers']),(1,0,0))
 def test_legacy_action_marked_import_does_not_count_recent(self):
  with self.g.db:self.g.db.execute('insert into permanent_ban_actions values(?,?,?,?,?,?)',('imported',ID,'Fixture','reason','legacy_import',self.now))
  self.assertEqual(self.stats()['unknownTimePlayers'],1);self.assertEqual(self.stats()['last12HoursPlayers'],0)
 def test_future_unknown_import_not_counted_early(self):
  self.g.confirm(ID,'Fixture','reason','history',None,self.now+10,historical=True)
  self.assertEqual(self.stats()['totalPlayers'],0);self.now+=10;self.assertEqual(self.stats()['unknownTimePlayers'],1)
 def test_external_unknown_older_history_dominates_known_local_time(self):
  self.g.observe_native_bans([{'steamId':ID}],self.now-100);self.g.confirm(ID,'Fixture','reason','monitor','permanent',self.now)
  self.assertEqual(self.stats()['unknownTimePlayers'],1);self.assertEqual(self.stats()['last12HoursPlayers'],0)
 def test_legacy_backfill_is_idempotent(self):
  self.g.confirm(ID,'Fixture','reason','history',None,self.now,historical=True)
  a=self.stats();before=self.g.db.execute('select count(*) from ban_statistics_evidence').fetchone()[0];b=self.stats();self.assertEqual(a,b);self.assertEqual(before,self.g.db.execute('select count(*) from ban_statistics_evidence').fetchone()[0])
 def test_current_native_sync_pending_with_effective_local_still_counts(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now);self.g.request_native_bans([ID],self.now)
  self.assertEqual((self.stats()['totalPlayers'],self.stats()['last12HoursPlayers']),(1,1))

 def test_concurrent_writer_cannot_split_count_snapshot(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now-90000)
  entered=threading.Event();release=threading.Event();written=threading.Event();result=[];errors=[]
  from service import ban_statistics as module
  real=module.backfill
  def gated(store,now):entered.set();release.wait(2);return real(store,now)
  def reader():
   try:result.append(self.stats())
   except Exception as ex:errors.append(ex)
  def writer():
   try:self.g.restrict(OTHER,'Fixture','reason','manual',self.now);written.set()
   except Exception as ex:errors.append(ex)
  with patch('service.ban_statistics.backfill',side_effect=gated):
   a=threading.Thread(target=reader);a.start();self.assertTrue(entered.wait(1));b=threading.Thread(target=writer);b.start()
   try:self.assertFalse(written.wait(.03))
   finally:release.set();a.join(2);b.join(2)
  self.assertFalse(errors);self.assertEqual((result[0]['totalPlayers'],result[0]['last24HoursPlayers']),(1,0));self.assertEqual((self.stats()['totalPlayers'],self.stats()['last24HoursPlayers']),(2,1))
 def test_cumulative_receipt_survives_active_row_deletion(self):
  self.g.restrict(ID,'Fixture','reason','manual',self.now)
  with self.g.db:self.g.db.execute('delete from rejoin_restrictions');self.g.db.execute('delete from permanent_ban_actions')
  self.g.close();self.g=KickStore(self.path);self.assertEqual(self.stats()['totalPlayers'],1)

class NativeReader(f.FakeRcon):
 def __init__(self):super().__init__();self.bans=[];self.calls=[];self.bad=False
 def request(self,method,path,body=None):
  self.calls.append((method,path))
  if path=='/v1/bans':
   if self.bad:raise TimeoutError('secret must not leak')
   return {'bans':self.bans}
  return super().request(method,path,body)
class StatisticsHttpTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.r=NativeReader();self.e=f.app.Engine(Path(self.tmp.name)/'state.db',self.r,dict(f.app.DEFAULT_CONFIG,rejoin_guard_enabled=True));self.server=ThreadingHTTPServer(('127.0.0.1',0),f.app.api_handler(self.e,'query-key','report-key','admin-key'));self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.e.close();self.tmp.cleanup()
 def request(self,key='query-key'):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3);headers={} if key is None else {'Authorization':'Bearer '+key};c.request('GET','/v1/moderation/ban-statistics',headers=headers);r=c.getresponse();status=r.status;b=json.loads(r.read());c.close();return status,b
 def test_query_only_auth_and_no_player_details(self):
  self.r.bans=[{'steamId':ID}]
  for key in [None,'wrong','admin-key','report-key']:self.assertEqual(self.request(key)[0],401)
  status,b=self.request();self.assertEqual(status,200);self.assertEqual(b['totalPlayers'],1);self.assertNotIn(ID,json.dumps(b));self.assertEqual(self.r.calls,[('GET','/v1/bans')])
 def test_disabled_or_native_failure_or_db_failure_not_fake_zero(self):
  self.r.bad=True;status,b=self.request();self.assertEqual(status,503);self.assertNotIn('secret',json.dumps(b));self.assertNotIn('totalPlayers',b)
  self.r.bad=False;self.e.kick_store.db.execute('drop table confirmed_kicks');self.assertEqual(self.request()[0],503)
 def test_not_configured_and_malformed_native_list(self):
  self.r.bans=[{'steamId':'invalid'}];self.assertEqual(self.request()[0],503)
  old=self.e.kick_store;self.e.kick_store=None
  try:self.assertEqual(self.request()[0],503)
  finally:self.e.kick_store=old

 def test_disabled_configuration_and_business_failure_not_success(self):
  self.e.config['rejoin_guard_enabled']=False;self.assertEqual(self.request()[0],503);self.e.config['rejoin_guard_enabled']=True
  with patch.object(self.e.rcon,'request',return_value={'ok':False,'bans':[]}):self.assertEqual(self.request()[0],503)
