"""Controlled retries use fake executors only; never real or historical requests."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
import test_admin_moderation as h
from service.rcon import RconError,Rcon
from service.moderation_ops import Coordinator

ID=h.ID
PATH='/v1/players/'+ID+'/kick'

def not_found():return RconError(404,method='POST',path=PATH)


class Fake(h.fixtures.FakeRcon):
    def __init__(self):
        super().__init__();self.players=[h.fixtures.player()];self.reserved=[];self.outcomes=[];self.posts=0;self.reads=[];self.post_callback=None
    def request(self,method,path,body=None):
        if method=='GET':self.reads.append(path)
        if path=='/v1/reserved-slots':return {'reservedSlots':copy.deepcopy(self.reserved)}
        if path.endswith('/kick'):
            self.posts+=1
            if self.post_callback:self.post_callback(self.posts)
            value=self.outcomes.pop(0) if self.outcomes else None
            if isinstance(value,BaseException):raise value
        return super().request(method,path,body)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.cfg=h.config();self.cfg['rejoin_guard_enabled']=True
        self.fake=Fake();self.e=h.app.Engine(self.path,self.fake,self.cfg);self.waits=[]
        self.e.moderation._retry_sleep=self.waits.append
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def submit(self):return self.e.moderation.submit_admin(h.admin_payload(),'fake-retry-key',now=1000)['operationId']
    def process(self,op):return self.e.moderation.process(op,now=1001)
    def test_404_then_success_same_operation_one_notice_batch(self):
        self.fake.outcomes=[not_found()];op=self.submit();result=self.process(op)
        self.assertEqual(result['operationId'],op);self.assertEqual(result['kickStatus'],'confirmed');self.assertEqual(self.fake.posts,2)
        self.assertEqual(self.waits,[1]);self.assertEqual(len(result['announcementJobIds']),3)
        self.assertIn('/v1/reserved-slots',self.fake.reads);self.assertEqual(self.e.db.execute('SELECT count(*) FROM moderation_operations').fetchone()[0],1)
        again=self.e.moderation.submit_admin(h.admin_payload(),'fake-retry-key',now=1002);self.assertEqual(again['operationId'],op);self.process(op);self.assertEqual(self.fake.posts,2)
    def test_two_404_then_success_is_last_allowed_attempt(self):
        self.fake.outcomes=[not_found(),not_found()];result=self.process(self.submit())
        self.assertEqual(result['kickStatus'],'confirmed');self.assertEqual(self.fake.posts,3);self.assertEqual(self.waits,[1,2])
    def test_retry_exhaustion_is_failed_without_notices_or_restriction(self):
        self.fake.outcomes=[not_found(),not_found(),not_found()];result=self.process(self.submit())
        self.assertEqual(result['status'],'failed');self.assertEqual(result['kickStatus'],'failed');self.assertEqual(result['error']['code'],'rcon_http_404')
        self.assertEqual(self.fake.posts,3);self.assertEqual(self.waits,[1,2]);self.assertEqual(result['announcementJobIds'],[])
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM rejoin_restrictions').fetchone()[0],0)
    def test_offline_after_404_does_not_rekick_or_restrict(self):
        self.fake.outcomes=[not_found()]
        self.e.moderation._retry_sleep=lambda delay:self.fake.players.clear()
        result=self.process(self.submit());self.assertEqual(result['status'],'offline');self.assertEqual(result['kickStatus'],'not_needed');self.assertEqual(result['error']['code'],'player_offline')
        self.assertEqual(self.fake.posts,1);self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM permanent_ban_actions').fetchone()[0],0)
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM rejoin_restrictions').fetchone()[0],0)
    def test_unknown_result_or_non404_never_replayed(self):
        for error in [TimeoutError(),RconError(500,method='POST',path=PATH),RconError(503,method='POST',path=PATH),RconError(403,method='POST',path=PATH),RconError(404),RconError(404,method='GET',path='/v1/players'),RconError(404,method='POST',path='/v1/bans')]:
            with self.subTest(error=type(error).__name__,status=getattr(error,'status',None)),tempfile.TemporaryDirectory() as folder:
                fake=Fake();fake.outcomes=[error];engine=h.app.Engine(Path(folder)/'state.db',fake,self.cfg)
                engine.moderation._retry_sleep=lambda delay:self.fail('unexpected retry')
                try:
                    op=engine.moderation.submit_admin(h.admin_payload(),'key',now=1000)['operationId'];result=engine.moderation.process(op,now=1001)
                    self.assertEqual(fake.posts,1);self.assertIn(result['status'],['unknown','failed'])
                    if isinstance(error,TimeoutError) or getattr(error,'status',0)>=500:self.assertEqual(result['kickStatus'],'unknown')
                finally:engine.close()
    def test_second_attempt_timeout_stops_and_keeps_unknown_quarantine(self):
        self.fake.outcomes=[not_found(),TimeoutError()];op=self.submit();result=self.process(op)
        self.assertEqual(result['kickStatus'],'unknown');self.assertEqual(self.fake.posts,2)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.assertFalse(self.e.moderation.process_one(now=1002));self.assertEqual(self.fake.posts,2)
    def test_new_reserved_target_is_stopped_before_retry(self):
        self.fake.outcomes=[not_found()];self.e.moderation._retry_sleep=lambda delay:self.fake.reserved.append(ID)
        result=self.process(self.submit());self.assertEqual(result['status'],'cancelled');self.assertEqual(result['error']['code'],'reserved_player_exempt');self.assertEqual(self.fake.posts,1)
    def test_new_behavior_exemption_is_stopped_before_retry(self):
        self.fake.outcomes=[not_found()];self.e.moderation._retry_sleep=lambda delay:self.e.behavior.add(ID,'Fixture',1001)
        result=self.process(self.submit());self.assertEqual(result['status'],'cancelled');self.assertEqual(result['error']['code'],'behavior_list_kick_exempt');self.assertEqual(self.fake.posts,1)
    def test_scope_revoked_during_backoff_is_not_retried(self):
        self.fake.outcomes=[not_found()];self.e.moderation._retry_sleep=lambda delay:self.e.config.update(admin_moderation_scopes=[])
        result=self.process(self.submit());self.assertEqual(result['status'],'rejected');self.assertEqual(self.fake.posts,1)
    def test_pending_visible_and_parallel_worker_cannot_claim_same_operation(self):
        self.fake.outcomes=[not_found()];op=self.submit()
        def wait(delay):
            pending=self.e.moderation.get(op);self.assertEqual(pending['status'],'resolving');self.assertEqual(pending['kickStatus'],'not_started');self.assertIsNone(pending['error'])
            self.assertEqual(self.process(op)['status'],'resolving');self.assertEqual(self.fake.posts,1)
        self.e.moderation._retry_sleep=wait
        self.assertEqual(self.process(op)['kickStatus'],'confirmed');self.assertEqual(self.fake.posts,2)
    def test_lost_target_lease_stops_retry(self):
        self.fake.outcomes=[not_found()];op=self.submit()
        def wait(delay):
            with self.e.lock,self.e.db:self.e.db.execute('UPDATE moderation_targets SET operation_id=? WHERE steam_id=?',('new-owner',ID))
        self.e.moderation._retry_sleep=wait
        self.assertEqual(self.process(op)['status'],'superseded');self.assertEqual(self.fake.posts,1)
        self.assertEqual(self.e.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?',(ID,)).fetchone()[0],'new-owner')
    def test_crash_after_second_known_404_preserves_total_budget(self):
        class Crash(BaseException):pass
        self.fake.outcomes=[not_found(),not_found(),not_found()];op=self.submit()
        def wait(delay):
            if delay==2:raise Crash()
        self.e.moderation._retry_sleep=wait
        self.assertRaises(Crash,self.process,op);self.assertEqual(self.fake.posts,2)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg);self.e.moderation._retry_sleep=lambda delay:None
        result=self.e.moderation.process(op,now=1002)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.fake.posts,3)
    def test_restart_fences_still_running_retry_worker(self):
        entered=threading.Event();release=threading.Event();errors=[];self.fake.outcomes=[not_found()];op=self.submit()
        self.e.moderation._retry_sleep=lambda delay:(entered.set(),release.wait(4))
        def old_worker():
            try:self.process(op)
            except Exception as e:errors.append(e)
        thread=threading.Thread(target=old_worker);thread.start();other=None
        try:
            self.assertTrue(entered.wait(2));other=h.app.Engine(self.path,self.fake,self.cfg);other.moderation._retry_sleep=lambda delay:None
            self.assertEqual(other.moderation.process(op,now=1002)['kickStatus'],'confirmed')
        finally:
            release.set();thread.join(5)
            if other:other.close()
        self.assertEqual(errors,[]);self.assertFalse(thread.is_alive());self.assertEqual(self.fake.posts,2)
    def test_crash_during_retry_dispatch_remains_unknown_without_replay(self):
        class Crash(BaseException):pass
        self.fake.outcomes=[not_found(),Crash()];op=self.submit();self.assertRaises(Crash,self.process,op)
        self.e.close();self.e=h.app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.e.moderation.get(op)['kickStatus'],'unknown');self.assertFalse(self.e.moderation.process_one(now=1002));self.assertEqual(self.fake.posts,2)
    def test_report_does_not_receive_admin_retry_policy(self):
        from test_moderation_ops import report
        self.fake.outcomes=[not_found()];op=self.e.moderation.submit(report(),'report-key',now=1000)['operationId'];result=self.process(op)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.fake.posts,1);self.assertEqual(self.waits,[])


    def test_expired_permission_during_wait_stops_retry(self):
        self.fake.outcomes=[not_found()]
        self.e.moderation._retry_sleep=lambda delay:self.e.config.update(admin_moderation_max_age_seconds=.5)
        result=self.process(self.submit());self.assertEqual(result['status'],'rejected');self.assertEqual(result['error']['code'],'admin_request_stale_or_future');self.assertEqual(self.fake.posts,1)
    def test_failed_fresh_reserved_lookup_stops_before_second_post(self):
        self.fake.outcomes=[not_found()];original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/reserved-slots':raise TimeoutError()
            return original(method,path,body)
        self.fake.request=request
        result=self.process(self.submit());self.assertEqual(result['status'],'failed');self.assertEqual(result['kickStatus'],'not_started');self.assertEqual(result['error']['code'],'reserved_list_unavailable');self.assertEqual(self.fake.posts,1)
    def test_fresh_player_lookup_404_is_not_a_kick_retry(self):
        self.fake.outcomes=[not_found()];original=self.fake.request
        def request(method,path,body=None):
            if self.fake.posts and path=='/v1/players':raise RconError(404,method='GET',path=path)
            return original(method,path,body)
        self.fake.request=request
        result=self.process(self.submit());self.assertEqual(result['status'],'failed');self.assertEqual(result['kickStatus'],'not_started');self.assertEqual(self.fake.posts,1)
    def test_confirmed_retry_uses_fresh_name_and_one_actual_kick_receipt(self):
        self.fake.outcomes=[not_found()];self.e.moderation._retry_sleep=lambda delay:self.fake.players[0].update(name='FreshFixture')
        result=self.process(self.submit());self.assertEqual(result['name'],'FreshFixture');self.assertEqual(self.fake.posts,2)
        self.assertEqual(self.e.kick_store.db.execute('SELECT count(*) FROM confirmed_kicks').fetchone()[0],1)
        for (message,) in self.e.db.execute('SELECT message FROM jobs'):self.assertIn('FreshFixture',message)
    def test_automatic_kicks_do_not_use_admin_retry(self):
        self.fake.outcomes=[not_found()]
        result=self.e.moderation.process_auto('fake-auto-job',ID,'r',lambda p:True,lambda p:['a','b','c'],now=1001)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.fake.posts,1);self.assertEqual(self.waits,[])
    def test_http_query_observes_pending_without_new_submit_or_dispatch(self):
        self.fake.outcomes=[not_found()];op=self.submit()
        server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.e,'query','report','admin'))
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def wait(delay):
            connection=http.client.HTTPConnection(*server.server_address,timeout=3)
            connection.request('GET','/v1/moderation/operations/'+op,headers={'Authorization':'Bearer admin'})
            response=connection.getresponse();body=json.loads(response.read());connection.close()
            self.assertEqual(response.status,200);self.assertEqual(body['operationId'],op);self.assertEqual(body['status'],'resolving');self.assertEqual(body['kickStatus'],'not_started')
            self.assertNotIn('attempts',body);self.assertEqual(self.fake.posts,1)
        self.e.moderation._retry_sleep=wait
        try:self.assertEqual(self.process(op)['kickStatus'],'confirmed')
        finally:server.shutdown();server.server_close();thread.join()


class ErrorMetadataTests(unittest.TestCase):
    def test_rcon_http_error_labels_actual_method_and_path(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('fixture-token');response=Mock(status=404);response.read.return_value=b'{}';response.getheader.return_value='30';connection=Mock();connection.getresponse.return_value=response
            with patch('service.rcon.http.client.HTTPConnection',return_value=connection):
                with self.assertRaises(RconError) as e:Rcon('fixture.invalid',1,key)._request('POST',PATH,{'reason':'fixture'})
            self.assertEqual(e.exception.method,'POST');self.assertEqual(e.exception.path,PATH);self.assertEqual(e.exception.status,404)
