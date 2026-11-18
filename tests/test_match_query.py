import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from service.match_query import MatchQuery, MatchUnavailable
import test_monitor as fixtures

class Fake:
    def __init__(self):
        self.status={'map':'ExampleMap','experiences':['Example_KOTH_01'],'players':{'current':3,'max':98},
            'factionScores':[{'name':'Lonestar','score':120},{'name':'Manticore','score':0}]}
        self.players={'players':[{'faction':'Lonestar','kills':999},{'faction':'Lonestar','kills':500},{'faction':'Manticore','kills':400}]}
        self.calls=[];self.error=None;self.after=None
    def request(self,method,path,body=None):
        self.calls.append((method,path,body))
        if method!='GET':raise AssertionError('read-only')
        if self.error:raise self.error
        if self.after:self.after()
        return copy.deepcopy(self.status if path=='/v1/status' else self.players)

class MatchTests(unittest.TestCase):
    def setUp(self):self.now=1000;self.fake=Fake();self.query=MatchQuery(self.fake,clock=lambda:self.now)
    def test_authoritative_scores_not_kills(self):
        r=self.query.get()
        self.assertEqual(r['playerCount'],3);self.assertEqual(r['maxPlayers'],98)
        self.assertEqual(r['mode'],'Example_KOTH_01');self.assertEqual(r['map'],'ExampleMap')
        self.assertEqual(r['teams'],[{'id':'Lonestar','name':'Lonestar','score':120,'playerCount':2},{'id':'Manticore','name':'Manticore','score':0,'playerCount':1}])
        self.assertTrue(r['scoreAvailable']);self.assertEqual(r['scoreKind'],'faction_score')
        self.assertEqual(self.fake.calls,[('GET','/v1/status',None),('GET','/v1/players',None)])
    def test_missing_scores_are_null_not_zero(self):
        self.fake.status.pop('factionScores');r=self.query.get()
        self.assertFalse(r['scoreAvailable']);self.assertIsNone(r['scoreKind'])
        self.assertTrue(all(t['score'] is None for t in r['teams']))
    def test_invalid_or_partial_score_never_fabricated(self):
        for bad in [None,True,-1,float('nan'),float('inf'),'120']:
            with self.subTest(bad=bad):
                self.fake.status['factionScores'][0]['score']=bad
                r=MatchQuery(self.fake,clock=lambda:1000).get()
                self.assertIsNone(r['teams'][0]['score']);self.assertFalse(r['scoreAvailable'])
    def test_missing_optional_fields_and_player_list_fallback(self):
        self.fake.status={'factionScores':[{'name':'Lonestar'}]};r=self.query.get()
        self.assertEqual(r['playerCount'],3)
        self.assertIsNone(r['maxPlayers']);self.assertIsNone(r['map']);self.assertIsNone(r['mode'])
        self.assertFalse(r['scoreAvailable'])
    def test_missing_player_details_leave_team_counts_unknown(self):
        self.fake.players={};r=self.query.get()
        self.assertEqual(r['playerCount'],3)
        self.assertTrue(all(t['playerCount'] is None for t in r['teams']))
    def test_no_authoritative_player_count_fails_instead_of_zero(self):
        self.fake.status.pop('players');self.fake.players={}
        with self.assertRaises(MatchUnavailable) as e:self.query.get()
        self.assertEqual(e.exception.code,'match_invalid_upstream')
    def test_real_empty_server_zero_counts(self):
        self.fake.players={'players':[]};self.fake.status['players']['current']=0
        r=self.query.get();self.assertEqual(r['playerCount'],0)
        self.assertTrue(all(t['playerCount']==0 for t in r['teams']))
    def test_cache_expiry_does_not_serve_stale_on_failure(self):
        r=self.query.get();self.now=1010;self.assertEqual(self.query.get(),r)
        self.assertEqual(len(self.fake.calls),2)
        self.now=1016;self.fake.error=TimeoutError()
        with self.assertRaises(MatchUnavailable) as e:self.query.get()
        self.assertEqual(e.exception.code,'match_upstream_unavailable')
        self.fake.error=None;self.fake.status['factionScores'][0]['score']=200
        self.assertEqual(self.query.get()['teams'][0]['score'],200)
    def test_slow_upstream_explicitly_stale(self):
        self.fake.after=lambda:setattr(self,'now',self.now+10)
        with self.assertRaises(MatchUnavailable) as e:self.query.get()
        self.assertEqual(e.exception.code,'match_data_stale')
    def test_response_copy_does_not_mutate_cache(self):
        r=self.query.get();r['teams'][0]['score']=555
        self.assertEqual(self.query.get()['teams'][0]['score'],120)
    def test_invalid_team_rows_fall_back_to_unknown_score(self):
        self.fake.status['factionScores']=[{'name':'Lonestar','score':10},{'score':999}]
        r=self.query.get();self.assertFalse(r['scoreAvailable'])
        self.assertTrue(all(t['score'] is None for t in r['teams']))

class HttpMatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.fake=Fake()
        self.engine=fixtures.app.Engine(Path(self.temp.name)/'state.db',self.fake,fixtures.app.DEFAULT_CONFIG)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),fixtures.app.api_handler(self.engine,'query','report','admin'))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.engine.close();self.temp.cleanup()
    def request(self,key):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Authorization':'Bearer '+key} if key else {}
        c.request('GET','/v1/match',headers=headers);r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result
    def test_query_key_only_and_readonly(self):
        for key in [None,'report','admin']:
            self.assertEqual(self.request(key)[0],401)
        status,r=self.request('query');self.assertEqual(status,200);self.assertTrue(r['scoreAvailable'])
        self.assertTrue(all(m=='GET' for m,p,b in self.fake.calls))
    def test_upstream_failure_explicit_503(self):
        self.fake.error=TimeoutError();status,r=self.request('query')
        self.assertEqual(status,503);self.assertEqual(r['error']['code'],'match_upstream_unavailable')
    def test_missing_scores_http_fallback(self):
        self.fake.status.pop('factionScores');status,r=self.request('query')
        self.assertEqual(status,200);self.assertFalse(r['scoreAvailable'])
        self.assertTrue(all(t['score'] is None for t in r['teams']))
    def test_stale_acquisition_http_error(self):
        now=[1000];self.engine.match_query.clock=lambda:now[0]
        self.fake.after=lambda:now.__setitem__(0,now[0]+10)
        status,r=self.request('query')
        self.assertEqual(status,503);self.assertEqual(r['error']['code'],'match_data_stale')
    def test_invalid_upstream_http_error(self):
        self.fake.status.pop('players');self.fake.players={}
        status,r=self.request('query')
        self.assertEqual(status,503);self.assertEqual(r['error']['code'],'match_invalid_upstream')

if __name__=='__main__':unittest.main()
