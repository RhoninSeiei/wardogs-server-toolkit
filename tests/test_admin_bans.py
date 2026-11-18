from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
import test_admin_history as h
from service.rcon import RconError


class BanTests(unittest.TestCase):
    roster=h.HistoryTests.roster
    def setUp(self):
        h.HistoryTests.setUp(self);self.bans=[];self.posts=0;self.applies=True;self.failure=None;self.online=True;self.reserved=[]
        self.fake.players=[h.fixtures.player()];original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/reserved-slots':return {'reservedSlots':self.reserved}
            if path=='/v1/bans':
                if method=='GET':return {'bans':list(self.bans)}
                self.posts+=1
                if self.applies:
                    self.bans.append({'steamId':body['steamId'],'bannedAtUtc':'2026-10-29T13:00:00Z','reason':body['reason']});self.fake.players=[]
                if self.failure:raise self.failure
                return {'ok':True}
            return original(method,path,body)
        self.fake.request=request
    tearDown=h.HistoryTests.tearDown
    def submit(self,now=1000,payload=None,key='ban-key'):return self.engine.admin_bans.submit(payload or h.admin_payload(),key,now=now)
    def process(self,opid=None):return self.engine.admin_bans.process(opid or self.submit()['operationId'],now=1001)
    def test_online_native_and_local_with_separate_receipts_no_kick_or_notice(self):
        r=self.process();self.assertEqual(r['banStatus'],'banned');self.assertEqual(r['nativeBanStatus'],'banned');self.assertEqual(r['localRestrictionStatus'],'restricted')
        self.assertTrue(r['onlineBefore']);self.assertEqual(r['removalStatus'],'confirmed');self.assertEqual(r['kickStatus'],'not_attempted');self.assertEqual(self.posts,1)
        self.assertEqual(self.fake.kicked,[]);self.assertEqual(self.engine.queue_depth(),0)
        self.assertEqual(self.engine.kick_store.db.execute('select count(*) from confirmed_kicks').fetchone()[0],0)
    def test_offline_can_register_native_and_local_without_claiming_kick(self):
        self.fake.players=[];r=self.process();self.assertEqual(r['banStatus'],'banned');self.assertFalse(r['onlineBefore']);self.assertEqual(r['removalStatus'],'not_needed')
    def test_offline_native_404_only_local_partial_no_retry(self):
        self.fake.players=[];self.failure=RconError(404);self.applies=False
        op=self.submit()['operationId'];r=self.process(op);self.assertEqual(r['banStatus'],'partial');self.assertEqual(r['nativeBanStatus'],'failed');self.assertEqual(r['localRestrictionStatus'],'restricted')
        self.process(op);self.submit(now=2000);self.assertEqual(self.posts,1);self.assertEqual(self.fake.kicked,[])
    def test_timeout_without_readback_proof_unknown_lease_and_no_replay(self):
        self.failure=TimeoutError();self.applies=False;op=self.submit()['operationId'];r=self.process(op)
        self.assertEqual(r['nativeBanStatus'],'unknown');self.assertEqual(r['banStatus'],'partial');self.assertEqual(self.engine.db.execute('select outcome from moderation_targets').fetchone()[0],'unknown')
        self.process(op);self.submit(now=2000);self.assertEqual(self.posts,1)
    def test_timeout_with_readback_membership_proves_ban(self):
        self.failure=TimeoutError();r=self.process();self.assertEqual(r['nativeBanStatus'],'banned');self.assertEqual(r['banStatus'],'banned')
    def test_existing_native_and_local_are_not_rewritten(self):
        guard=self.engine.kick_store;guard.restrict(h.ID,'Fixture','earlier','admin',900,action_id='old');self.bans=[{'steamId':h.ID}]
        r=self.process();self.assertEqual(r['nativeBanStatus'],'already_banned');self.assertEqual(r['localRestrictionStatus'],'already_restricted');self.assertEqual(self.posts,0)
        self.assertEqual(guard.db.execute('select event_id from rejoin_restrictions').fetchone()[0],'old')
    def test_protected_reserved_and_behavior_no_writes(self):
        self.reserved=[h.ID];r=self.process();self.assertEqual(r['localRestrictionStatus'],'exempt');self.assertEqual(self.posts,0)
        self.assertEqual(self.engine.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
    def test_behavior_exempt(self):
        with self.engine.kick_store.db:self.engine.kick_store.db.execute('insert into behavior_players values(?,?,1,?,?,?,0,0)',(h.ID,'Fixture',900,900,900))
        self.process();self.assertEqual(self.posts,0);self.assertEqual(self.engine.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
    def test_unknown_lease_and_local_outcome_not_overwritten(self):
        with self.engine.db:self.engine.db.execute("insert into moderation_targets values(?,?,'unknown',NULL)",(h.ID,'other'))
        r=self.process();self.assertEqual(r['error']['code'],'target_already_coordinated');self.assertEqual(self.posts,0)
    def test_unknown_identity_offline_rejected(self):
        self.fake.players=[];p=h.admin_payload(identity=h.OTHER);r=self.process(self.submit(payload=p)['operationId'])
        self.assertEqual(r['error']['code'],'player_not_known');self.assertEqual(self.posts,0)
    def test_idempotency_payload_and_cross_action_conflict(self):
        op=self.submit();self.assertEqual(self.submit(now=2000)['operationId'],op['operationId'])
        with self.assertRaises(h.ContractError):self.submit(payload=h.admin_payload(identity=h.OTHER))
        for submit in [self.engine.moderation.submit_admin,self.engine.admin_history.submit,self.engine.admin_cycle.submit,self.engine.admin_unban.submit]:
            with self.assertRaises(h.ContractError):submit(h.admin_payload(),'new-key',now=1000)
    def test_role_scope_and_age(self):
        for key,value in [('role','member'),('botId','untrusted'),('groupId','untrusted')]:
            p=h.admin_payload();p['actor'][key]=value
            with self.assertRaises(h.ContractError):self.submit(payload=p)
        with self.assertRaises(h.ContractError):self.submit(now=2000)
    def test_process_twice_dispatch_once(self):
        op=self.submit()['operationId'];self.process(op);self.process(op);self.assertEqual(self.posts,1)
    def test_crash_dispatch_unknown_not_replayed(self):
        op=self.submit()['operationId']
        with self.engine.db:
            self.engine.db.execute("update admin_bans set status='ban_dispatching' where id=?",(op,));self.engine.db.execute("insert into moderation_targets values(?,?,'held',NULL)",(h.ID,op))
        from service.admin_bans import AdminBans
        self.engine.admin_bans=AdminBans(self.engine);r=self.process(op)
        self.assertEqual(r['nativeBanStatus'],'unknown');self.assertEqual(self.posts,0);self.assertEqual(self.engine.db.execute('select outcome from moderation_targets').fetchone()[0],'unknown')
    def test_rejoin_worker_respects_held_and_unknown_ownership(self):
        from service.kick_guard import check_rejoins,TrackingRcon
        guard=self.engine.kick_store;guard.restrict(h.ID,'Fixture','earlier','admin',900,action_id='existing')
        self.engine.rcon=TrackingRcon(self.fake,guard)
        for outcome in ['held','unknown']:
            with self.engine.db:self.engine.db.execute('insert or replace into moderation_targets values(?,?,?,NULL)',(h.ID,'owner',outcome))
            with self.engine.lock:check_rejoins(self.engine,self.fake.players,1001)
            self.assertEqual(self.fake.kicked,[])
        with self.engine.db:self.engine.db.execute('delete from moderation_targets')
        with self.engine.lock:check_rejoins(self.engine,self.fake.players,1001)
        self.assertEqual(len(self.fake.kicked),1)
    def test_pending_native_sync_is_not_replayed(self):
        guard=self.engine.kick_store;guard.restrict(h.ID,'Fixture','earlier','admin',900,action_id='existing');guard.request_native_bans([h.ID],900)
        r=self.process();self.assertEqual(r['error']['code'],'punishment_outcome_unresolved');self.assertEqual(self.posts,0)
    def test_preflight_failure_performs_no_mutation(self):
        original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players':raise TimeoutError()
            return original(method,path,body)
        self.fake.request=request;r=self.process();self.assertEqual(r['banStatus'],'failed');self.assertEqual(self.posts,0)
        self.assertEqual(self.engine.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
    def test_concurrent_process_dispatch_once(self):
        op=self.submit()['operationId'];threads=[threading.Thread(target=self.process,args=(op,)) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(self.posts,1);self.assertEqual(self.engine.admin_bans.get(op)['banStatus'],'banned')
    def test_partial_native_list_prevents_any_ban(self):
        original=self.fake.request
        def request(method,path,body=None):
            result=original(method,path,body)
            if method=='GET' and path=='/v1/bans':result['complete']=False
            return result
        self.fake.request=request;r=self.process();self.assertEqual(r['banStatus'],'failed');self.assertEqual(self.posts,0)
        self.assertEqual(self.engine.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
    def test_count_mismatch_roster_prevents_any_ban(self):
        original=self.fake.request
        def request(method,path,body=None):
            result=original(method,path,body)
            if method=='GET' and path=='/v1/players':result['count']=999
            return result
        self.fake.request=request;r=self.process();self.assertEqual(r['banStatus'],'failed');self.assertEqual(self.posts,0)
    def test_http_admin_only_and_same_operation_get(self):
        now=time.time();server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'));thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            for token,status in [('query',401),('report',401),('admin',202)]:
                conn=http.client.HTTPConnection(*server.server_address,timeout=3);conn.request('POST','/v1/moderation/admin-bans',json.dumps(h.admin_payload(now=now)),{'Authorization':'Bearer '+token,'Idempotency-Key':'http-ban','Content-Type':'application/json'})
                resp=conn.getresponse();body=json.loads(resp.read());conn.close();self.assertEqual(resp.status,status,body)
                if status==202:
                    op=body['operationId'];conn=http.client.HTTPConnection(*server.server_address,timeout=3);conn.request('GET','/v1/moderation/operations/'+op,headers={'Authorization':'Bearer admin'});resp=conn.getresponse();result=json.loads(resp.read());conn.close();self.assertEqual(resp.status,200);self.assertEqual(result['action'],'ban')
        finally:server.shutdown();server.server_close();thread.join()
