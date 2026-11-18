"""No production network: all unban targets and executors are fixtures."""
import json,tempfile,unittest,time,threading,http.client
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import test_admin_history as h
from test_admin_moderation import config,admin_payload,ID
from service.moderation_ops import ContractError
from service.rcon import RconError

class Fake(h.Rcon):
    def __init__(self):super().__init__();self.bans=[{'steamId':ID,'name':'BannedFixture'}];self.delete_error=None;self.delete_applies=True;self.delete_count=0;self.read_error=False
    def request(self,method,path,body=None):
        if path=='/v1/bans':
            self.calls.append((method,path))
            if self.read_error:raise TimeoutError()
            return {'bans':[dict(x) for x in self.bans]}
        if method=='DELETE':
            self.calls.append((method,path));self.delete_count+=1
            if self.delete_applies:self.bans=[]
            if self.delete_error:raise self.delete_error
            return {'ok':True,'pending':False}
        return super().request(method,path,body)

class UnbanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.cfg=config();self.cfg['rejoin_guard_enabled']=True
        self.fake=Fake();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.e.kick_store.confirm(ID,'Fixture','r','auto','permanent',900,event_id='old-confirmed')
        self.e.kick_store.request_native_bans([ID],900)
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def submit(self,p=None,key='unban-key',now=1000):return self.e.admin_unban.submit(p or admin_payload(),key,now=now)
    def process(self,op=None,now=1001):return self.e.admin_unban.process(op or self.submit()['operationId'],now=now)
    def test_full_release_keeps_history_stats_and_cycle(self):
        self.e.behavior.add(ID,'Fixture',900);before=self.e.kick_store.ban_statistics(1000)['totalPlayers'];result=self.process()
        self.assertEqual(result['unbanStatus'],'unbanned');self.assertEqual(result['nativeUnbanStatus'],'unbanned');self.assertEqual(result['localReleaseStatus'],'released');self.assertEqual(result['status'],'completed')
        self.assertEqual(self.fake.delete_count,1);self.assertIsNone(self.e.kick_store.native_ban_state(ID));self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM rejoin_restrictions').fetchone()[0],0)
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM confirmed_kicks').fetchone()[0],1);self.assertEqual(self.e.kick_store.ban_statistics(1001)['totalPlayers'],before)
        self.assertTrue(self.e.behavior.snapshot()['players'][0]['enabled']);self.assertEqual(self.e.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.e.kick_store.promote_history();self.assertEqual(self.e.kick_store.candidates([{'steamId':ID}],'permanent',2000),[])
    def test_native_already_absent_no_delete(self):
        self.fake.bans=[];result=self.process();self.assertEqual(result['nativeUnbanStatus'],'already_unbanned');self.assertEqual(result['unbanStatus'],'unbanned');self.assertEqual(self.fake.delete_count,0)
    def test_local_already_released_is_explicit(self):
        self.e.kick_store.release(ID,'earlier fixture',950);self.fake.bans=[];result=self.process();self.assertEqual(result['localReleaseStatus'],'already_released');self.assertEqual(result['nativeUnbanStatus'],'already_unbanned')
    def test_delete_timeout_readback_absent_proves_unbanned(self):
        self.fake.delete_error=TimeoutError();result=self.process();self.assertEqual(result['unbanStatus'],'unbanned');self.assertEqual(self.fake.delete_count,1)
    def test_delete_timeout_still_banned_partial_no_replay(self):
        self.fake.delete_error=TimeoutError();self.fake.delete_applies=False;op=self.submit()['operationId'];result=self.process(op)
        self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(result['nativeUnbanStatus'],'unknown');self.assertEqual(result['localReleaseStatus'],'released')
        self.process(op);self.submit(now=2000);self.assertEqual(self.fake.delete_count,1)
    def test_explicit_delete_rejection_partial_without_retry(self):
        self.fake.delete_error=RconError(403);self.fake.delete_applies=False;result=self.process();self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(result['nativeUnbanStatus'],'failed');self.assertEqual(self.fake.delete_count,1)
    def test_initial_read_failure_does_not_release(self):
        self.fake.read_error=True;result=self.process();self.assertEqual(result['status'],'failed');self.assertEqual(self.fake.delete_count,0);self.assertIsNotNone(self.e.kick_store.native_ban_state(ID))
    def test_idempotency_and_cross_action_conflicts(self):
        first=self.submit();self.assertEqual(self.submit(now=2000)['operationId'],first['operationId'])
        for submit in [self.e.moderation.submit_admin,self.e.admin_history.submit,self.e.admin_cycle.submit]:
            with self.assertRaises(ContractError):submit(admin_payload(),'other-key',now=1000)
    def test_shared_unknown_lease_stops_unban(self):
        with self.e.db:self.e.db.execute("INSERT INTO moderation_targets VALUES(?,?,'unknown',NULL)",(ID,'other-op'))
        result=self.process();self.assertEqual(result['status'],'superseded');self.assertEqual(self.fake.delete_count,0)
    def test_roles_scope_ttl_and_extra_fields(self):
        for field,value in [('role','member'),('groupId','other'),('botId','other'),('platform','other')]:
            p=admin_payload();p['actor'][field]=value
            with self.assertRaises(ContractError):self.submit(p)
        p=admin_payload();p['extra']=True
        with self.assertRaises(ContractError):self.submit(p)
        with self.assertRaises(ContractError):self.submit(admin_payload(now=879))
    def test_unknown_identity_is_not_synthesized(self):
        p=admin_payload(identity=h.OTHER);result=self.process(self.submit(p)['operationId']);self.assertEqual(result['error']['code'],'player_not_known');self.assertEqual(result['status'],'rejected');self.assertEqual(self.fake.delete_count,0)

    def test_failed_final_read_is_partial_unknown(self):
        original=self.fake.request
        def request(method,path,body=None):
            result=original(method,path,body)
            if method=='DELETE':self.fake.read_error=True
            return result
        self.fake.request=request;result=self.process();self.assertEqual(result['nativeUnbanStatus'],'unknown');self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(self.fake.delete_count,1)
    def test_restart_after_local_release_does_not_issue_native_delete(self):
        class Crash(BaseException):pass
        release=self.e.kick_store.release
        def crash(*args,**kwargs):release(*args,**kwargs);raise Crash()
        op=self.submit()['operationId']
        with patch.object(self.e.kick_store,'release',side_effect=crash):
            with self.assertRaises(Crash):self.process(op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg);result=self.e.admin_unban.get(op)
        self.assertEqual(result['localReleaseStatus'],'released');self.assertEqual(result['nativeUnbanStatus'],'not_attempted');self.assertEqual(result['unbanStatus'],'partial');self.assertFalse(self.e.admin_unban.process_one(now=1002));self.assertEqual(self.fake.delete_count,0)
    def test_restart_during_delete_is_unknown_and_never_replayed(self):
        class Crash(BaseException):pass
        self.fake.delete_error=Crash();op=self.submit()['operationId']
        with self.assertRaises(Crash):self.process(op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg);result=self.e.admin_unban.get(op)
        self.assertEqual(result['nativeUnbanStatus'],'unknown');self.assertEqual(result['localReleaseStatus'],'released');self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(self.fake.delete_count,1)
        self.process(op);self.assertEqual(self.fake.delete_count,1)
    def test_restart_after_verified_delete_uses_receipts_without_replay(self):
        class Crash(BaseException):pass
        op=self.submit()['operationId']
        with patch.object(self.e.admin_unban,'finish',side_effect=Crash()):
            with self.assertRaises(Crash):self.process(op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg);result=self.e.admin_unban.get(op)
        self.assertEqual(result['unbanStatus'],'unbanned');self.assertEqual(result['nativeUnbanStatus'],'unbanned');self.assertEqual(self.fake.delete_count,1)
    def test_scope_revoked_after_ban_read_does_not_release(self):
        original=self.fake.request
        def request(method,path,body=None):
            result=original(method,path,body)
            if path=='/v1/bans':self.e.config['admin_moderation_scopes']=[]
            return result
        self.fake.request=request;result=self.process();self.assertEqual(result['status'],'rejected');self.assertEqual(result['localReleaseStatus'],'not_attempted');self.assertEqual(self.fake.delete_count,0)
    def test_scope_revoked_after_local_commit_reports_partial(self):
        release=self.e.kick_store.release
        def change(*a,**kw):result=release(*a,**kw);self.e.config['admin_moderation_scopes']=[];return result
        with patch.object(self.e.kick_store,'release',side_effect=change):result=self.process()
        self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(result['localReleaseStatus'],'released');self.assertEqual(result['nativeUnbanStatus'],'not_attempted');self.assertEqual(self.fake.delete_count,0)
    def test_local_failure_never_dispatches_native_delete(self):
        with patch.object(self.e.kick_store,'release',side_effect=RuntimeError('fixture')):result=self.process()
        self.assertEqual(result['unbanStatus'],'unknown');self.assertEqual(self.fake.delete_count,0);self.assertIsNotNone(self.e.kick_store.native_ban_state(ID))
    def test_native_malformed_or_duplicate_ids_are_not_treated_as_absent(self):
        for entries in [[{'steamId':'bad'}],[{'steamId':ID},{'steamId':ID}]]:
            self.fake.bans=entries
            p=admin_payload(request_id=str(len(entries)));op=self.submit(p,key=str(len(entries)))['operationId'];result=self.process(op)
            self.assertEqual(result['status'],'failed');self.assertEqual(self.fake.delete_count,0)
    def test_release_stable_receipt_does_not_clear_future_offenses(self):
        guard=self.e.kick_store;guard.release(ID,'fixture',1000,action_id='stable-fixture')
        guard.confirm(ID,'Fixture','new','auto','permanent',1002,event_id='new-confirmed')
        guard.release(ID,'fixture',1003,action_id='stable-fixture')
        self.assertEqual(guard.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(ID,)).fetchone()[0],'new-confirmed')
    def test_existing_punishment_dispatching_blocks_release(self):
        with self.e.kick_store.db:self.e.kick_store.db.execute("UPDATE native_ban_sync SET status='dispatching' WHERE steam_id=?",(ID,))
        result=self.process();self.assertEqual(result['status'],'rejected');self.assertEqual(result['error']['code'],'punishment_dispatch_in_progress');self.assertEqual(self.fake.delete_count,0)
    def test_pending_visible_and_same_operation_not_claimed_twice(self):
        op=self.submit()['operationId'];original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/bans' and self.fake.delete_count==0:
                self.assertEqual(self.e.admin_unban.get(op)['status'],'resolving');self.assertEqual(self.process(op)['status'],'resolving')
            return original(method,path,body)
        self.fake.request=request;self.assertEqual(self.process(op)['unbanStatus'],'unbanned');self.assertEqual(self.fake.delete_count,1)
    def test_all_four_action_queues_and_request_ids_are_shared(self):
        self.e.config['moderation_pending_limit']=1;self.submit()
        for submit in [self.e.moderation.submit_admin,self.e.admin_history.submit,self.e.admin_cycle.submit,self.e.admin_unban.submit]:
            with self.assertRaises(ContractError) as error:submit(admin_payload(request_id='other'),'other-key',now=1000)
            self.assertEqual(error.exception.status,429)
    def test_old_automatic_job_cancelled_but_new_job_still_eligible(self):
        old=self.e.add_job('fixture',900,recipient=ID,kind='kill_cap_kick');self.e.db.commit();self.e.kick_store.release(ID,'fixture',1000)
        result=self.e.moderation.process_auto(old,ID,'fixture',lambda p:True,lambda p:['a','b','c'],now=1001)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(result['error']['code'],'auto_precedes_unban')
        fresh=self.e.add_job('fixture',1002,recipient=ID,kind='kill_cap_kick');self.e.db.commit();self.fake.players=[h.fixtures.player()]
        result=self.e.moderation.process_auto(fresh,ID,'fixture',lambda p:True,lambda p:['a','b','c'],now=1003)
        self.assertEqual(result['kickStatus'],'confirmed')
    def test_old_burst_action_cancelled_after_release(self):
        with self.e.db:self.e.db.execute('INSERT INTO burst_actions VALUES(?,?,?,?,?,?,?,?,NULL)',('old-burst',ID,'Fixture','{}','accepted',900,900,0))
        self.e.kick_store.release(ID,'fixture',1000);self.assertTrue(self.e.burst_guard.process_one(now=1001))
        self.assertEqual(self.e.db.execute('SELECT status FROM burst_actions').fetchone()[0],'cancelled');self.assertEqual(self.fake.delete_count,0)
    def test_other_player_restriction_is_not_removed(self):
        self.e.kick_store.restrict(h.OTHER,'OtherFixture','r','manual',900);self.process()
        self.assertIsNotNone(self.e.kick_store.db.execute('SELECT 1 FROM rejoin_restrictions WHERE steam_id=?',(h.OTHER,)).fetchone())
    def test_restart_recovery_fences_worker_while_delete_inflight(self):
        gate=threading.Event();release=threading.Event();errors=[];original=self.fake.request;op=self.submit()['operationId']
        def request(method,path,body=None):
            if method=='DELETE':gate.set();release.wait(5)
            return original(method,path,body)
        self.fake.request=request
        def work():
            try:self.process(op)
            except BaseException as e:errors.append(type(e).__name__)
        t=threading.Thread(target=work);t.start();other=None
        try:
            self.assertTrue(gate.wait(3));other=h.app.Engine(self.path,self.fake,self.cfg)
            self.assertEqual(other.admin_unban.get(op)['nativeUnbanStatus'],'unknown')
        finally:
            release.set();t.join(5)
            if other:other.close()
        self.assertEqual(errors,[]);self.assertFalse(t.is_alive());self.assertEqual(self.fake.delete_count,1);self.assertEqual(self.e.admin_unban.get(op)['nativeUnbanStatus'],'unknown')

    def test_native_already_absent_local_failure_is_partial_not_full(self):
        self.fake.bans=[]
        with patch.object(self.e.kick_store,'release',side_effect=RuntimeError('fixture')):result=self.process()
        self.assertEqual(result['unbanStatus'],'partial');self.assertEqual(result['nativeUnbanStatus'],'already_unbanned');self.assertEqual(result['localReleaseStatus'],'unknown')
    def test_cross_action_request_id_conflict_other_actions_first(self):
        for index,submit in enumerate([self.e.moderation.submit_admin,self.e.admin_history.submit,self.e.admin_cycle.submit]):
            p=admin_payload(request_id='other-'+str(index));submit(p,'other-'+str(index),now=1000)
            with self.assertRaises(ContractError):self.submit(p,key='new-'+str(index))
    def test_release_receipt_sid_mismatch_is_not_accepted(self):
        self.e.kick_store.release(ID,'fixture',1000,action_id='same-receipt')
        with self.assertRaises(ValueError):self.e.kick_store.release(h.OTHER,'fixture',1001,action_id='same-receipt')

if __name__=='__main__':unittest.main()
