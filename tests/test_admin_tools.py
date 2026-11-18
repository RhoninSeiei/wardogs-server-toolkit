"""Administrator read-only queries and HTTP integration with fake executors."""
import copy,json,time,tempfile,unittest,threading,http.client
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import test_admin_history as h
from test_admin_unban import Fake
from test_admin_moderation import config,admin_payload,ID
from service.moderation_ops import ContractError

class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cfg=config();self.cfg['rejoin_guard_enabled']=True;self.fake=Fake()
        self.e=h.app.Engine(Path(self.tmp.name)/'state.db',self.fake,self.cfg)
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def payload(self):return {'actor':admin_payload()['actor'],'requestedAt':1000}
    def search(self,query):return self.e.admin_tools.unban_search(dict(self.payload(),query=query),now=1001)
    def test_list_enabled_only_exact_ids_and_fresh_online_states(self):
        self.e.behavior.add(ID,'StoredFixture',900);self.e.behavior.add(h.OTHER,'DisabledFixture',900);self.e.behavior.disable(h.OTHER)
        self.fake.players=[dict(h.fixtures.player(),name='OnlineFixture')];r=self.e.admin_tools.cycle_list(self.payload(),now=1001)
        self.assertTrue(r['complete']);self.assertEqual(r['observedAt'],1001);self.assertEqual(r['players'],[{'steamId':ID,'name':'OnlineFixture','online':True}])
        self.fake.players=[];r=self.e.admin_tools.cycle_list(self.payload(),now=1001);self.assertFalse(r['players'][0]['online']);self.assertEqual(r['players'][0]['name'],'StoredFixture')
    def test_policy_matches_existing_intervals_and_first_join_semantics(self):
        p=self.e.admin_tools.cycle_list(self.payload(),now=1001)['policy']
        self.assertEqual(p['teamIntervalSeconds'],[30,300]);self.assertEqual(p['killIntervalSeconds'],[90,600]);self.assertEqual(p['workerCheckIntervalSeconds'],1)
        self.assertTrue(p['firstObservationActionsDueImmediately']);self.assertTrue(p['subsequentJoinReschedulesTimers']);self.assertFalse(p['equipmentPurchaseTriggered']);self.assertFalse(p['inventoryClearingSupported'])
        self.assertEqual(p['noticeTemplate'],'哈哈 {name} [{steamId}]');self.assertTrue(p['noticeAfterConfirmedAction'])
    def test_list_roster_failure_and_duplicate_ids_are_unavailable(self):
        for roster in [[{'steamId':'bad'}],[h.fixtures.player(),h.fixtures.player()]]:
            self.fake.players=roster
            with self.assertRaises(ContractError) as error:self.e.admin_tools.cycle_list(self.payload(),now=1001)
            self.assertEqual(error.exception.status,503)
    def test_list_and_search_are_role_scope_age_strict(self):
        for function,base in [(self.e.admin_tools.cycle_list,self.payload()),(self.e.admin_tools.unban_search,dict(self.payload(),query=ID))]:
            for field,value in [('role','member'),('platform','other'),('botId','other'),('groupId','other')]:
                p=copy.deepcopy(base);p['actor'][field]=value
                with self.assertRaises(ContractError):function(p,now=1001)
            p=copy.deepcopy(base);p['requestedAt']=800
            with self.assertRaises(ContractError):function(p,now=1001)
            p=copy.deepcopy(base);p['extra']=True
            with self.assertRaises(ContractError):function(p,now=1001)
    def test_native_only_identity_and_name_search_are_supported(self):
        r=self.search('BannedFixture');self.assertEqual(r['players'][0]['steamId'],ID);self.assertTrue(r['players'][0]['nativeBanned']);self.assertFalse(r['players'][0]['localRestricted']);self.assertIsNone(r['players'][0]['online'])
        self.assertEqual(self.search(ID)['players'][0]['steamId'],ID)
    def test_local_restriction_and_native_pending_identity_supported(self):
        self.fake.bans=[];self.e.kick_store.restrict(ID,'LocalFixture','r','manual',900);self.e.kick_store.request_native_bans([ID],900)
        r=self.search('LocalFixture')['players'][0];self.assertTrue(r['localRestricted']);self.assertFalse(r['nativeBanned']);self.assertEqual(r['nativeSyncState'],'pending')
    def test_same_nickname_keeps_separate_steam_candidates(self):
        self.fake.bans=[{'steamId':ID,'name':'SameFixture'},{'steamId':h.OTHER,'name':'SameFixture'}]
        self.assertEqual(len(self.search('SameFixture')['players']),2)
    def test_unknown_id_not_guessed_from_nickname(self):
        self.fake.bans=[];self.assertEqual(self.search('76561190000000099')['players'],[]);self.assertEqual(self.search('ID:76561190000000001')['players'],[])
    def test_historical_released_exact_id_remains_searchable(self):
        self.fake.bans=[];self.e.kick_store.confirm(ID,'OldFixture','r','auto','permanent',900);self.e.kick_store.release(ID,'fixture',950)
        r=self.search(ID)['players'][0];self.assertEqual(r['steamId'],ID);self.assertEqual(r['name'],'OldFixture');self.assertFalse(r['localRestricted'])
    def test_search_truncates_candidates_explicitly(self):
        self.fake.bans=[{'steamId':str(76561190000000000+i),'name':'ManyFixture'} for i in range(30)]
        r=self.search('ManyFixture');self.assertEqual(len(r['players']),20);self.assertTrue(r['truncated'])
    def test_native_failure_not_treated_as_empty_and_no_permissions_changed(self):
        self.fake.read_error=True
        with self.assertRaises(ContractError) as error:self.search(ID)
        self.assertEqual(error.exception.status,503);self.assertEqual(self.e.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
    def test_queries_do_not_modify_policy_or_dispatch_mutations(self):
        self.e.behavior.add(ID,'StoredFixture',900);before=self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchall()
        self.search(ID);self.e.admin_tools.cycle_list(self.payload(),now=1001)
        self.assertEqual(before,self.e.kick_store.db.execute('SELECT * FROM behavior_players').fetchall());self.assertTrue(all(method=='GET' for method,path in self.fake.calls))
    def test_authorization_rechecked_after_read(self):
        original=self.fake.request
        def request(method,path,body=None):
            result=original(method,path,body);self.e.config['admin_moderation_scopes']=[];return result
        self.fake.request=request
        with self.assertRaises(ContractError):self.e.admin_tools.cycle_list(self.payload(),now=1001)

    def test_elapsed_roster_or_ban_snapshot_over_15_seconds_unavailable(self):
        original=self.fake.request;stamp=[1000]
        def request(method,path,body=None):
            result=original(method,path,body);stamp[0]=1016;return result
        self.fake.request=request
        for function,payload in [(self.e.admin_tools.cycle_list,self.payload()),(self.e.admin_tools.unban_search,dict(self.payload(),query=ID))]:
            stamp[0]=1000
            with patch('service.admin_tools.time.time',side_effect=lambda:stamp[0]):
                with self.assertRaises(ContractError) as error:function(payload)
                self.assertEqual(error.exception.status,503)
    def test_cycle_roster_timeout_is_not_offline_or_empty_list(self):
        with patch.object(self.fake,'request',side_effect=TimeoutError()):
            with self.assertRaises(ContractError) as error:self.e.admin_tools.cycle_list(self.payload(),now=1001)
        self.assertEqual(error.exception.code,'cycle_list_players_unavailable')

class ToolsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cfg=config();self.cfg['rejoin_guard_enabled']=True;self.fake=Fake()
        self.e=h.app.Engine(Path(self.tmp.name)/'state.db',self.fake,self.cfg)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.e,'query','report','admin'));self.t=threading.Thread(target=self.server.serve_forever);self.t.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.t.join();self.e.close();self.tmp.cleanup()
    def request(self,method,path,payload=None,key='admin'):
        c=http.client.HTTPConnection(*self.server.server_address,timeout=3);c.request(method,path,json.dumps(payload) if payload is not None else None,{'Authorization':'Bearer '+key,'Content-Type':'application/json','Idempotency-Key':'http-unban'})
        response=c.getresponse();body=json.loads(response.read());status=response.status;c.close();return status,body
    def test_unban_single_post_then_operation_get(self):
        p=admin_payload(now=time.time());status,r=self.request('POST','/v1/moderation/admin-unbans',p);self.assertEqual(status,202)
        op=r['operationId'];self.assertEqual(self.request('GET','/v1/moderation/operations/'+op)[1]['unbanStatus'],'pending');self.e.admin_unban.process_one()
        status,r=self.request('GET','/v1/moderation/operations/'+op);self.assertEqual(status,200);self.assertEqual(r['unbanStatus'],'unbanned');self.assertEqual(r['action'],'unban');self.assertEqual(r['localReleaseStatus'],'not_restricted')
        self.assertEqual(self.request('GET','/v1/moderation/operations/'+op,key='report')[0],404);self.assertEqual(self.request('GET','/v1/moderation/operations/'+op,key='query')[0],401)
    def test_all_new_endpoints_admin_only(self):
        for path in ['/v1/moderation/admin-unbans','/v1/moderation/unban-search','/v1/moderation/cycle-list']:
            for key in ['query','report','wrong']:
                self.assertEqual(self.request('POST',path,{},key)[0],401)
            p=admin_payload(now=time.time()) if path.endswith('admin-unbans') else {'actor':admin_payload()['actor'],'requestedAt':time.time()}
            if path.endswith('unban-search'):p['query']='Fixture'
            p['actor']['role']='member';self.assertEqual(self.request('POST',path,p)[0],403)
    def test_readonly_queries_success_without_new_operations(self):
        p={'actor':admin_payload()['actor'],'requestedAt':time.time()}
        self.assertEqual(self.request('POST','/v1/moderation/cycle-list',p)[0],200)
        self.assertEqual(self.request('POST','/v1/moderation/unban-search',dict(p,query=ID))[0],200)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM admin_unbans').fetchone()[0],0);self.assertEqual(self.fake.delete_count,0)

if __name__=='__main__':unittest.main()
