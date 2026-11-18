"""Virtual identities and fake RCON only; no production actions."""
import copy,json,tempfile,time,unittest,threading,http.client,sqlite3
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import test_admin_history as h
from test_admin_moderation import admin_payload,config,ID
from service.moderation_ops import ContractError

class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.cfg=config();self.cfg['rejoin_guard_enabled']=True
        self.fake=h.Rcon();self.fake.players=[h.fixtures.player()];self.e=h.app.Engine(self.path,self.fake,self.cfg)
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def submit(self,p=None,key='cycle-key',now=1000):return self.e.admin_cycle.submit(p or admin_payload(),key,now=now)
    def run_op(self,op=None,now=1001):return self.e.admin_cycle.process(op or self.submit()['operationId'],now=now)
    def known(self):
        with self.e.lock,self.e.db:self.e.player_history.observe(ID,'HistoryFixture','roster_history',900,900)
    def assert_no_actions(self):
        self.assertFalse(any(method!='GET' for method,path in self.fake.calls))
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM permanent_ban_actions').fetchone()[0],0)
    def test_online_registers_existing_logic_without_actions(self):
        accepted=self.submit();self.assertEqual(accepted['cycleStatus'],'pending');result=self.run_op(accepted['operationId'])
        self.assertEqual(result['action'],'cycle_add');self.assertEqual(result['status'],'completed');self.assertEqual(result['cycleStatus'],'registered')
        self.assertFalse(result['alreadyEnabled']);self.assertTrue(result['onlineAtCheck']);self.assertEqual(result['registeredAt'],1001)
        self.assertEqual(result['kickStatus'],'not_needed');self.assertEqual(result['nativeBanStatus'],'not_requested');self.assertEqual(result['announcementJobIds'],[])
        self.assertTrue(self.e.kick_store.kick_exempt(ID));self.assert_no_actions()
    def test_enabled_record_preserves_timers_first_run_and_online(self):
        self.e.behavior.add(ID,'Fixture',900)
        with self.e.kick_store.db:self.e.kick_store.db.execute('UPDATE behavior_players SET next_team=1200,next_kill=1300,seen=1,online=1 WHERE steam_id=?',(ID,))
        before=self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchone();result=self.run_op();after=self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchone()
        self.assertTrue(result['alreadyEnabled']);self.assertEqual(before[2:],after[2:]);self.assert_no_actions()
    def test_offline_known_registers_without_kick_or_ban(self):
        self.fake.players=[];self.known();result=self.run_op();self.assertEqual(result['cycleStatus'],'registered');self.assertFalse(result['onlineAtCheck']);self.assertEqual(result['name'],'HistoryFixture');self.assert_no_actions()
    def test_unknown_offline_identity_rejected(self):
        self.fake.players=[];result=self.run_op();self.assertEqual(result['error']['code'],'player_not_known');self.assertEqual(result['status'],'rejected');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_reserved_player_cancelled(self):
        self.fake.reserved=[ID];result=self.run_op();self.assertEqual(result['status'],'cancelled');self.assertEqual(result['error']['code'],'reserved_player_exempt');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_idempotency_same_operation_without_timer_changes(self):
        first=self.submit();done=self.run_op(first['operationId']);again=self.submit(key='alias',now=2000)
        self.assertEqual(first['operationId'],again['operationId']);self.assertTrue(again['deduplicated']);self.assertEqual(self.run_op(first['operationId'])['registeredAt'],done['registeredAt'])
        p=admin_payload(identity=h.OTHER)
        with self.assertRaises(ContractError):self.submit(p)
    def test_member_scope_age_and_strict_fields(self):
        variants=[]
        for field,value in [('role','member'),('platform','other'),('botId','other'),('groupId','other')]:
            p=admin_payload();p['actor'][field]=value;variants.append(p)
        p=admin_payload();p['extra']=True;variants.append(p)
        variants.extend([admin_payload(now=879),admin_payload(now=1031),admin_payload(identity='nickname')])
        for p in variants:
            with self.subTest(p=p),self.assertRaises(ContractError):self.submit(p)
        self.assert_no_actions()
    def test_scope_revalidated_after_roster(self):
        original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players':self.e.config['admin_moderation_scopes']=[]
            return original(method,path,body)
        self.fake.request=request;result=self.run_op();self.assertEqual(result['status'],'rejected');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_same_player_lease_conflict(self):
        with self.e.db:self.e.db.execute("INSERT INTO moderation_targets VALUES(?,?,'unknown',NULL)",(ID,'other-operation'))
        result=self.run_op();self.assertEqual(result['status'],'superseded');self.assertEqual(result['relatedOperationId'],'other-operation');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_request_id_cross_action_conflict_both_directions(self):
        first=self.submit()
        for submit in [self.e.moderation.submit_admin,self.e.admin_history.submit]:
            with self.assertRaises(ContractError) as error:submit(admin_payload(),'other-key',now=1000)
            self.assertEqual(error.exception.status,409)
        p=admin_payload(request_id='kick-first');self.e.moderation.submit_admin(p,'kick-key',now=1000)
        with self.assertRaises(ContractError):self.submit(p,key='new-key')
        p=admin_payload(request_id='restriction-first');self.e.admin_history.submit(p,'restriction-key',now=1000)
        with self.assertRaises(ContractError):self.submit(p,key='newer-key')

    def test_disabled_record_reenabled_without_resetting_timers(self):
        self.e.behavior.add(ID,'Fixture',900);self.e.behavior.disable(ID)
        before=self.e.kick_store.db.execute('SELECT next_team,next_kill,seen FROM behavior_players').fetchone()
        result=self.run_op();self.assertFalse(result['alreadyEnabled']);self.assertEqual(result['cycleStatus'],'registered')
        self.assertEqual(self.e.kick_store.db.execute('SELECT next_team,next_kill,seen FROM behavior_players').fetchone(),before)
    def test_reserved_lookup_or_roster_failure_does_not_register(self):
        for mode in ['reserved_timeout','reserved_bad','roster_timeout','roster_duplicate']:
            with self.subTest(mode=mode):
                original=self.fake.request
                def request(method,path,body=None):
                    if path=='/v1/reserved-slots' and mode=='reserved_timeout':raise TimeoutError()
                    if path=='/v1/reserved-slots' and mode=='reserved_bad':return {'reservedSlots':['bad']}
                    if path=='/v1/players' and mode=='roster_timeout':raise TimeoutError()
                    if path=='/v1/players' and mode=='roster_duplicate':return {'players':[h.fixtures.player(),h.fixtures.player()]}
                    return original(method,path,body)
                self.fake.request=request
                op=self.submit(admin_payload(request_id=mode),key=mode)['operationId'];result=self.run_op(op)
                self.assertEqual(result['status'],'failed');self.assertEqual(self.e.behavior.snapshot()['players'],[])
                self.fake.request=original
    def test_atomic_receipt_rolls_back_when_policy_add_fails(self):
        with patch.object(self.e.behavior,'add',side_effect=RuntimeError('fixture')):
            op=self.submit()['operationId'];result=self.run_op(op)
        self.assertEqual(result['status'],'unknown');self.assertEqual(self.e.behavior.snapshot()['players'],[])
        self.assertIsNone(self.e.admin_cycle.receipt(op,ID));self.assertFalse(self.e.admin_cycle.process_one(now=1002));self.assert_no_actions()
    def test_crash_after_policy_commit_recovers_without_second_add(self):
        class Crash(BaseException):pass
        op=self.submit()['operationId']
        with patch.object(self.e.admin_cycle,'finalize',side_effect=Crash()):
            with self.assertRaises(Crash):self.run_op(op)
        self.assertIsNotNone(self.e.admin_cycle.receipt(op,ID));before=self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchone()
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        result=self.e.admin_cycle.get(op);self.assertEqual(result['status'],'completed');self.assertEqual(result['cycleStatus'],'registered')
        self.assertEqual(self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchone(),before);self.assertFalse(self.e.admin_cycle.process_one(now=1002))
    def test_crash_before_policy_write_is_unknown_not_replayed(self):
        class Crash(BaseException):pass
        op=self.submit()['operationId']
        with patch.object(self.e.behavior,'add',side_effect=Crash()):
            with self.assertRaises(Crash):self.run_op(op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.e.admin_cycle.get(op)['cycleStatus'],'unknown');self.assertFalse(self.e.admin_cycle.process_one(now=1002))
        self.assertEqual(self.e.behavior.snapshot()['players'],[]);self.assertIsNone(self.e.admin_cycle.receipt(op,ID))
        again=self.submit(now=2000);self.assertEqual(again['operationId'],op);self.assertEqual(again['cycleStatus'],'unknown')
    def test_pending_visible_and_same_operation_cannot_be_claimed_twice(self):
        original=self.fake.request;op=self.submit()['operationId'];seen=[]
        def request(method,path,body=None):
            if path=='/v1/players':
                seen.append(self.e.admin_cycle.get(op)['status']);self.assertEqual(self.run_op(op)['status'],'resolving')
            return original(method,path,body)
        self.fake.request=request;self.assertEqual(self.run_op(op)['status'],'completed');self.assertEqual(seen,['resolving'])
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM admin_cycle_receipts').fetchone()[0],1)
    def test_recovery_fences_old_worker_before_registration(self):
        gate=threading.Event();release=threading.Event();errors=[];original=self.fake.request;op=self.submit()['operationId']
        def request(method,path,body=None):
            if path=='/v1/players' and threading.current_thread().name=='old-worker':gate.set();release.wait(5)
            return original(method,path,body)
        self.fake.request=request
        def work():
            try:self.run_op(op)
            except BaseException as e:errors.append(type(e).__name__)
        thread=threading.Thread(target=work,name='old-worker');thread.start();other=None
        try:
            self.assertTrue(gate.wait(3));other=h.app.Engine(self.path,self.fake,self.cfg)
            self.assertEqual(other.admin_cycle.process(op,now=1001)['cycleStatus'],'registered')
        finally:
            release.set();thread.join(5)
            if other:other.close()
        self.assertEqual(errors,[]);self.assertFalse(thread.is_alive());self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM admin_cycle_receipts').fetchone()[0],1)
    def test_target_lease_lost_during_roster_read(self):
        original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players':
                with self.e.db:self.e.db.execute('UPDATE moderation_targets SET operation_id=? WHERE steam_id=?',('new-owner',ID))
            return original(method,path,body)
        self.fake.request=request;result=self.run_op();self.assertEqual(result['status'],'superseded');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_queue_limit_counts_all_three_action_types(self):
        self.e.config['moderation_pending_limit']=1;self.submit()
        for submit in [self.e.moderation.submit_admin,self.e.admin_history.submit,self.e.admin_cycle.submit]:
            with self.assertRaises(ContractError) as error:submit(admin_payload(request_id='other'),'other-key',now=1000)
            self.assertEqual(error.exception.status,429)
    def test_receipt_after_commit_exception_is_confirmed_without_replaying(self):
        add=self.e.behavior.add
        def commit_then_error(*args):add(*args);raise RuntimeError('after committed')
        with patch.object(self.e.behavior,'add',side_effect=commit_then_error):result=self.run_op()
        self.assertEqual(result['cycleStatus'],'registered');self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM admin_cycle_receipts').fetchone()[0],1)
    def test_mismatched_receipt_cannot_confirm_registration(self):
        op=self.submit()['operationId']
        with self.e.db:self.e.db.execute("UPDATE admin_cycle_adds SET status='cycle_dispatching' WHERE id=?",(op,))
        with self.e.kick_store.db:self.e.kick_store.db.execute('INSERT INTO admin_cycle_receipts VALUES(?,?,?)',(op,h.OTHER,1001))
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.e.admin_cycle.get(op)['cycleStatus'],'unknown');self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_ttl_and_scope_rechecked_after_reserved_lookup(self):
        original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/reserved-slots':self.e.config['admin_moderation_max_age_seconds']=.5
            return original(method,path,body)
        self.fake.request=request;self.assertEqual(self.run_op()['status'],'rejected');self.assertEqual(self.e.behavior.snapshot()['players'],[])

    def test_partial_policy_write_and_receipt_rollback_together(self):
        def partial(*args):
            self.e.kick_store.db.execute('INSERT INTO behavior_players VALUES(?,?,1,?,?,?,0,0)',(ID,'PartialFixture',1001,1001,1001))
            raise RuntimeError('partial fixture write')
        op=self.submit()['operationId']
        with patch.object(self.e.behavior,'add',side_effect=partial):result=self.run_op(op)
        self.assertEqual(result['cycleStatus'],'unknown');self.assertIsNone(self.e.admin_cycle.receipt(op,ID));self.assertEqual(self.e.behavior.snapshot()['players'],[])
    def test_concurrent_idempotent_submissions_have_one_operation(self):
        results=[];errors=[]
        def submit():
            try:results.append(self.submit()['operationId'])
            except Exception as e:errors.append(type(e).__name__)
        threads=[threading.Thread(target=submit) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join(3)
        self.assertEqual(errors,[]);self.assertEqual(len(results),8);self.assertEqual(len(set(results)),1)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM admin_cycle_adds').fetchone()[0],1)
    def test_held_cycle_blocks_simultaneous_admin_kick(self):
        cycle=self.submit()['operationId'];kick=self.e.moderation.submit_admin(admin_payload(request_id='other-kick'),'kick-key',now=1000)['operationId'];original=self.fake.request;results=[]
        def request(method,path,body=None):
            if path=='/v1/players':results.append(self.e.moderation.process(kick,now=1001))
            return original(method,path,body)
        self.fake.request=request;self.assertEqual(self.run_op(cycle)['cycleStatus'],'registered')
        self.assertEqual(results[0]['status'],'superseded');self.assert_no_actions()
    def test_recovery_before_dispatch_safely_continues_with_same_id(self):
        class Crash(BaseException):pass
        op=self.submit()['operationId']
        with patch.object(self.fake,'request',side_effect=Crash()):
            with self.assertRaises(Crash):self.run_op(op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.e.admin_cycle.get(op)['status'],'accepted');self.assertTrue(self.e.admin_cycle.process_one(now=1001))
        self.assertEqual(self.e.admin_cycle.get(op)['cycleStatus'],'registered');self.assert_no_actions()
    def test_not_configured_rejected_without_registration(self):
        self.e.behavior=None
        with self.assertRaises(ContractError) as error:self.submit()
        self.assertEqual(error.exception.status,503)

class CycleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cfg=config();self.cfg['rejoin_guard_enabled']=True
        self.fake=h.Rcon();self.fake.players=[h.fixtures.player()];self.e=h.app.Engine(Path(self.tmp.name)/'state.db',self.fake,self.cfg)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.e,'query','report','admin'))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.e.close();self.tmp.cleanup()
    def request(self,method,path,key='admin',payload=None,idempotency='http-cycle'):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        headers={'Authorization':'Bearer '+key,'Idempotency-Key':idempotency,'Content-Type':'application/json'}
        connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
        response=connection.getresponse();body=json.loads(response.read());status=response.status;connection.close();return status,body
    def test_single_post_same_get_completion_no_actions(self):
        p=admin_payload(now=time.time());status,body=self.request('POST','/v1/moderation/admin-cycle-adds',payload=p)
        self.assertEqual(status,202);op=body['operationId'];self.assertEqual(body['cycleStatus'],'pending')
        path='/v1/moderation/operations/'+op
        self.assertEqual(self.request('GET',path)[1]['cycleStatus'],'pending');self.e.admin_cycle.process_one()
        status,done=self.request('GET',path);self.assertEqual(status,200);self.assertEqual(done['cycleStatus'],'registered');self.assertEqual(done['operationId'],op)
        self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',payload=p)[1]['operationId'],op)
        self.assertFalse(any(method!='GET' for method,path in self.fake.calls))
    def test_tokens_roles_scope_and_read_isolation(self):
        p=admin_payload(now=time.time())
        for key in ['query','report','wrong']:
            self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',key,p)[0],401)
        q=copy.deepcopy(p);q['actor']['role']='member';self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',payload=q)[0],403)
        q=copy.deepcopy(p);q['actor']['groupId']='wrong';self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',payload=q)[0],403)
        status,body=self.request('POST','/v1/moderation/admin-cycle-adds',payload=p);path='/v1/moderation/operations/'+body['operationId']
        self.assertEqual(self.request('GET',path,'report')[0],404);self.assertEqual(self.request('GET',path,'query')[0],401)
    def test_strict_contract_no_nickname_or_extra_fields(self):
        p=admin_payload(now=time.time());p['steamId']='Fixture'
        self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',payload=p)[0],400)
        p=admin_payload(now=time.time());p['reason']='custom';self.assertEqual(self.request('POST','/v1/moderation/admin-cycle-adds',payload=p)[0],400)

if __name__=='__main__':unittest.main()
