import copy
from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import time
import unittest
import test_recent_kills as r
import test_admin_history as h


class PlayerKillsTests(unittest.TestCase):
    roster=h.HistoryTests.roster
    setUp=r.RecentKillsTests.setUp
    tearDown=r.RecentKillsTests.tearDown
    add=r.RecentKillsTests.add
    def payload(self,now=1000):return dict(steamId=h.ID,actor=h.admin_payload()['actor'],requestedAt=now)
    def query(self,now=1000,payload=None):return self.engine.player_kills.query(self.payload(now) if payload is None else payload,now=now)
    def test_empty_readonly_no_remote_or_jobs(self):
        result=self.query();self.assertEqual(result['events'],[]);self.assertFalse(result['hasMore']);self.assertEqual(result['limit'],20)
        self.assertTrue(result['complete']);self.assertFalse(result['coverage']['historyComplete']);self.assertIsNone(result['coverage']['recordsSince'])
        self.assertEqual(self.fake.calls,[]);self.assertEqual(self.engine.queue_depth(),0);self.assertEqual(self.engine.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
    def test_full_history_not_five_minutes_exact_target_only(self):
        self.add(1,100,event_id='old',eventTime=43.125,killerFaction='Manticore',victimFaction='Lonestar')
        self.add(2,999,event_id='other',killerSteamId=h.OTHER,killerName='Fixture')
        self.add(3,1001,event_id='future')
        result=self.query();self.assertEqual(len(result['events']),1)
        event=result['events'][0];self.assertEqual(event['eventId'],'old');self.assertEqual(event['gameTimeSeconds'],43.125)
        self.assertEqual(event['receivedAt'],100);self.assertEqual(result['timeBasis'],'archive_received_at');self.assertEqual(event['relationship'],'enemy')
    def test_defaults_twenty_optional_fifty_and_stable_order(self):
        for i in range(55):self.add(i+1,999,event_id=str(i))
        result=self.query();self.assertEqual(len(result['events']),20);self.assertTrue(result['hasMore']);self.assertEqual(result['events'][0]['archiveId'],'55')
        result=self.query(payload=dict(self.payload(),limit=50));self.assertEqual(len(result['events']),50);self.assertTrue(result['hasMore'])
    def test_arbitrary_integer_limits_one_through_fifty(self):
        self.add()
        for limit in range(1,51):
            result=self.query(payload=dict(self.payload(),limit=limit));self.assertEqual(result['limit'],limit);self.assertEqual(len(result['events']),1)
    def test_exact_limit_has_no_more_and_received_order_precedes_sequence(self):
        for i in range(20):self.add(i+1,999-i,event_id=str(i))
        result=self.query();self.assertFalse(result['hasMore']);self.assertEqual(result['events'][0]['archiveId'],'1')
    def test_same_event_across_servers_retained(self):
        self.add(1,999,'a');self.add(2,999,'b');self.assertEqual([e['archiveId'] for e in self.query()['events']],['2','1'])
    def test_relationship_suicide_friendly_enemy_unknown_and_conflict(self):
        cases=[({'victimSteamId':h.ID},'self'),({'teamkill':True},'friendly'),({'teamkill':False},'enemy'),({},'unknown'),
            ({'killerFaction':'Manticore','victimFaction':'Manticore'},'friendly'),({'killerFaction':'Manticore','victimFaction':'Lonestar'},'enemy'),
            ({'teamkill':False,'killerFaction':'Manticore','victimFaction':'Manticore'},'unknown'),({'teamkill':'false'},'unknown'),
            ({'killerFaction':'unknown','victimFaction':'unknown'},'unknown')]
        for i,(changes,expected) in enumerate(cases,1):
            self.add(i,999,event_id=str(i),**changes);event=self.query()['events'][0];self.assertEqual(event['relationship'],expected)
    def test_missing_fields_preserve_null_no_name_identity_guess(self):
        self.add(victimSteamId='bad',victimName='OldFixture',cause=None,eventTime='123')
        e=self.query()['events'][0];self.assertIsNone(e['victim']['steamId']);self.assertIsNone(e['weapon']);self.assertIsNone(e['gameTimeSeconds']);self.assertEqual(e['relationship'],'unknown')
    def test_strict_fields_ids_and_limits(self):
        for value in [None,True,0,-1,51,20.0,'20',[],{}]:
            with self.subTest(value=value),self.assertRaises(h.ContractError) as e:self.query(payload=dict(self.payload(),limit=value))
            self.assertEqual(e.exception.status,400)
        for value in [h.ID+'0',123,'Fixture','７'*17]:
            with self.assertRaises(h.ContractError):self.query(payload=dict(self.payload(),steamId=value))
        for key in ['actor','requestedAt','steamId']:
            p=self.payload();del p[key]
            with self.assertRaises(h.ContractError):self.query(payload=p)
        with self.assertRaises(h.ContractError):self.query(payload=dict(self.payload(),offset=20))
    def test_roles_scope_age_and_no_remote_fallback(self):
        for field,value in [('role','member'),('groupId','other'),('botId','other'),('platform','other')]:
            p=self.payload();p['actor'][field]=value
            with self.assertRaises(h.ContractError) as e:self.query(payload=p)
            self.assertEqual(e.exception.status,403)
        with self.assertRaises(h.ContractError) as e:self.query(payload=self.payload(879))
        self.assertEqual(e.exception.status,422);self.assertEqual(self.fake.calls,[])
    def test_freshness_errors_and_caught_up(self):
        self.query(1015)
        with self.assertRaises(h.ContractError):self.query(1015.001)
        for value,error in [(984.99,None),(1001,None),(1000,'TimeoutError'),(None,None)]:
            with closing(sqlite3.connect(self.cache)) as db,db:db.execute('update sync_state set last_success=?,error=?',(value,error))
            with self.assertRaises(h.ContractError) as e:self.query()
            self.assertEqual(e.exception.status,503)
        with closing(sqlite3.connect(self.cache)) as db,db:
            db.execute('update sync_state set last_success=1000,error=NULL');db.execute('update sync_cursor set latest_seq=1')
        with self.assertRaises(h.ContractError) as e:self.query()
        self.assertEqual(e.exception.code,'player_kills_sync_incomplete')
    def test_malformed_storage_and_missing_sequence_fail_closed(self):
        self.add()
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute('delete from event_sequences')
        with self.assertRaises(h.ContractError):self.query()
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute("update events set body='broken'")
        with self.assertRaises(h.ContractError):self.query()
        self.cache.unlink()
        with self.assertRaises(h.ContractError):self.query()
    def test_response_size_bounded_without_silent_truncation(self):
        self.add(victimName='x'*66000)
        with self.assertRaises(h.ContractError) as e:self.query()
        self.assertEqual(e.exception.code,'player_kills_response_too_large')
    def test_readonly_snapshot_survives_concurrent_writer(self):
        from unittest.mock import patch
        import service.player_kills as module
        self.add()
        original=sqlite3.connect
        with closing(original(self.cache)) as db,db:db.execute('PRAGMA journal_mode=WAL')
        base=original(self.cache.resolve().as_uri()+'?mode=ro',uri=True)
        class Wrapped:
            def execute(inner,sql,*args):
                cursor=base.execute(sql,*args)
                if 'FROM sync_state' in sql:
                    rows=cursor.fetchall()
                    with closing(original(self.cache)) as writer,writer:
                        writer.execute('delete from events');writer.execute("update sync_state set error='TimeoutError'")
                    class Rows:
                        def fetchone(inner):return rows[0]
                    return Rows()
                return cursor
            def set_progress_handler(inner,*args):return base.set_progress_handler(*args)
            def close(inner):base.close()
        with patch.object(module.sqlite3,'connect',return_value=Wrapped()):result=self.query()
        self.assertEqual(len(result['events']),1);self.assertTrue(result['complete'])
    def test_http_admin_only(self):
        now=time.time()
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute('update sync_state set last_success=?',(now,))
        server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'));thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            for token,role,status in [('query','admin',401),('report','admin',401),(None,'admin',401),('admin','member',403),('admin','admin',200)]:
                p=self.payload(now);p['actor']['role']=role;headers={'Content-Type':'application/json'}
                if token:headers['Authorization']='Bearer '+token
                conn=http.client.HTTPConnection(*server.server_address,timeout=3);conn.request('POST','/v1/moderation/player-kills',json.dumps(p),headers)
                response=conn.getresponse();body=json.loads(response.read());conn.close();self.assertEqual(response.status,status,body)
        finally:server.shutdown();server.server_close();thread.join()
