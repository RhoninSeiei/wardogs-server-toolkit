import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import test_admin_history as h
from service.steam_profiles import SteamProfiles, SteamTransport, SteamFailure

SID=h.ID
OTHER=h.OTHER


def summary(sid=SID,**kw):
    row={'steamid':sid,'personaname':'Fixture','communityvisibilitystate':3,'loccountrycode':'JP','locstatecode':'01','loccityid':123,'timecreated':12345,'avatarfull':'https://avatars.steamstatic.com/fake.jpg'}
    row.update(kw);return row


def ban(sid=SID,**kw):
    row={'SteamId':sid,'CommunityBanned':False,'VACBanned':True,'NumberOfVACBans':1,'DaysSinceLastBan':100,'NumberOfGameBans':2,'EconomyBan':'none'}
    row.update(kw);return row


class FakeSteam:
    def __init__(self):
        self.calls=[];self.fail={};self.summaries=[summary()];self.bans=[ban()]
    def fetch(self,kind,ids):
        self.calls.append((kind,list(ids)))
        if kind in self.fail:raise self.fail[kind]
        return {'response':{'players':self.summaries}} if kind=='profile' else {'players':self.bans}


class SteamProfileTests(unittest.TestCase):
    roster=h.HistoryTests.roster
    def setUp(self):
        h.HistoryTests.setUp(self)
        self.transport=FakeSteam()
        self.profiles=SteamProfiles(self.engine,self.path.parent/'steam-profiles.sqlite3',transport=self.transport)
    tearDown=h.HistoryTests.tearDown
    def payload(self):return {'steamIds':[SID],'actor':h.admin_payload()['actor'],'requestedAt':1000}
    def query(self,now=1000,payload=None):return self.profiles.query(self.payload() if payload is None else payload,now=now)

    def test_profile_and_bans_exact_identity_no_game_actions(self):
        r=self.query();self.assertTrue(r['complete']);p=r['players'][0]
        self.assertEqual(p['steamId'],SID);self.assertEqual(p['profile']['countryCode'],'JP')
        self.assertEqual(p['profile']['countrySource'],'self_reported_profile')
        self.assertTrue(p['bans']['vacBanned']);self.assertEqual(p['bans']['numberOfGameBans'],2)
        self.assertEqual(self.fake.calls,[]);self.assertEqual(self.engine.queue_depth(),0)
        self.assertEqual(p['profileUrl'],'https://steamcommunity.com/profiles/'+SID+'/')

    def test_private_or_unset_region_stays_null(self):
        self.transport.summaries=[summary(communityvisibilitystate=1,loccountrycode=None,locstatecode=None,loccityid=None)]
        p=self.query()['players'][0]['profile']
        self.assertIsNone(p['countryCode']);self.assertEqual(p['visibilityState'],1)

    def test_batch_identity_order_not_nickname(self):
        self.transport.summaries=[summary(OTHER,personaname='same'),summary(SID,personaname='same')]
        self.transport.bans=[ban(OTHER,VACBanned=False),ban(SID)]
        p=self.payload();p['steamIds']=[SID,OTHER]
        r=self.query(payload=p)['players'];self.assertEqual([x['steamId'] for x in r],[SID,OTHER])
        self.assertTrue(r[0]['bans']['vacBanned']);self.assertFalse(r[1]['bans']['vacBanned'])

    def test_cache_survives_instance_restart_and_expires(self):
        self.query();self.assertEqual(len(self.transport.calls),2)
        self.profiles=SteamProfiles(self.engine,self.path.parent/'steam-profiles.sqlite3',transport=self.transport)
        p=self.payload();p['requestedAt']=1100
        r=self.query(1100,p);self.assertTrue(r['players'][0]['bans']['cacheHit']);self.assertEqual(len(self.transport.calls),2)
        p['requestedAt']=1601;self.query(1601,p);self.assertEqual(len(self.transport.calls),4)

    def test_partial_failure_never_fakes_clean_bans(self):
        self.transport.fail['bans']=SteamFailure('steam_timeout')
        r=self.query();self.assertFalse(r['complete']);p=r['players'][0]
        self.assertEqual(p['profile']['status'],'ok');self.assertEqual(p['bans']['status'],'unavailable')
        self.assertNotIn('vacBanned',p['bans'])

    def test_missing_profile_and_ban_not_inferred_false(self):
        self.transport.summaries=[];self.transport.bans=[]
        r=self.query();self.assertEqual(r['players'][0]['profile']['status'],'not_found')
        self.assertEqual(r['players'][0]['bans']['status'],'unavailable');self.assertFalse(r['complete'])

    def test_wrong_or_duplicate_response_identity_rejected(self):
        for rows in [[summary(OTHER)],[summary(),summary()]]:
            self.transport.summaries=rows
            r=self.query();self.assertEqual(r['players'][0]['profile']['status'],'unavailable')
            self.assertTrue(r['players'][0]['bans']['vacBanned'])

    def test_invalid_ban_type_no_truthiness_cast(self):
        self.transport.bans=[ban(VACBanned='false')]
        self.assertEqual(self.query()['players'][0]['bans']['status'],'unavailable')

    def test_all_upstream_failures_are_503(self):
        self.transport.fail={'profile':SteamFailure('steam_timeout'),'bans':SteamFailure('steam_http_error')}
        with self.assertRaises(h.app.ContractError) as error:self.query()
        self.assertEqual((error.exception.status,error.exception.code),(503,'steam_profiles_unavailable'))

    def test_rate_limit_is_persistent_and_no_immediate_retry(self):
        self.transport.fail['profile']=SteamFailure('steam_rate_limited',retry_after=30)
        with self.assertRaises(h.app.ContractError):self.query()
        self.assertEqual(len(self.transport.calls),1)
        self.profiles=SteamProfiles(self.engine,self.path.parent/'steam-profiles.sqlite3',transport=self.transport)
        with self.assertRaises(h.app.ContractError):self.query()
        self.assertEqual(len(self.transport.calls),1)

    def test_invalid_body_ids_size_and_duplicates(self):
        cases=[{'steamIds':[]},{'steamIds':[SID,SID]},{'steamIds':['bad']},{'steamIds':[123]},{'steamIds':[SID]*101},{'extra':True},{'steamIds':SID}]
        for change in cases:
            p=self.payload();p.update(change)
            with self.subTest(change=change),self.assertRaises(h.app.ContractError):self.query(payload=p)
        self.assertEqual(self.transport.calls,[])

    def test_member_scope_and_stale_denied_before_fetch(self):
        for field,value in [('role','member'),('groupId','wrong')]:
            p=self.payload();p['actor'][field]=value
            with self.assertRaises(h.app.ContractError):self.query(payload=p)
        p=self.payload();p['requestedAt']=1
        with self.assertRaises(h.app.ContractError):self.query(payload=p)
        self.assertEqual(self.transport.calls,[])

    def test_missing_key_is_503_without_network(self):
        self.profiles=SteamProfiles(self.engine,self.path.parent/'steam-profiles.sqlite3',key_file=self.path.parent/'missing-key')
        with self.assertRaises(h.app.ContractError) as error:self.query()
        self.assertEqual(error.exception.code,'steam_api_not_configured')

    def test_expired_request_while_fetching_is_not_returned(self):
        original=self.transport.fetch
        def fetch(kind,ids):
            result=original(kind,ids)
            self.engine.config['admin_moderation_scopes']=[]
            return result
        self.transport.fetch=fetch
        with self.assertRaises(h.app.ContractError):self.query()


class SteamHTTPTests(unittest.TestCase):
    roster=h.HistoryTests.roster
    def setUp(self):
        h.HistoryTests.setUp(self)
        self.transport=FakeSteam();self.engine.steam_profiles=SteamProfiles(self.engine,self.path.parent/'steam-profiles.sqlite3',transport=self.transport)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();h.HistoryTests.tearDown(self)
    def request(self,key='admin',role='admin'):
        actor=h.admin_payload()['actor'];actor['role']=role
        c=http.client.HTTPConnection(*self.server.server_address)
        c.request('POST','/v1/moderation/steam-profiles',json.dumps({'steamIds':[SID],'actor':actor,'requestedAt':time.time()}),{'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        response=c.getresponse();code=response.status;data=json.loads(response.read());c.close();return code,data
    def test_admin_200_query_and_report_401_member_403(self):
        self.assertEqual(self.request()[0],200)
        for token in ['query','report']:self.assertEqual(self.request(token)[0],401)
        self.assertEqual(self.request(role='member')[0],403)
        self.assertEqual(self.fake.calls,[])


class SteamTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'key';self.path.write_text('a'*32)
        self.transport=SteamTransport(self.path)
    def tearDown(self):self.tmp.cleanup()
    def fetch(self,status=200,body=b'{}',header=None):
        from unittest.mock import patch,MagicMock
        connection=MagicMock();response=connection.getresponse.return_value
        response.status=status;response.read.return_value=body;response.getheader.return_value=header
        with patch('service.steam_profiles.http.client.HTTPSConnection',return_value=connection) as factory:
            try:result=self.transport.fetch('profile',[SID])
            finally:
                self.assertEqual(factory.call_args.args,('api.steampowered.com',443))
                self.assertEqual(factory.call_args.kwargs,{'timeout':4})
                self.assertTrue(connection.close.called)
            return result,connection
    def test_fixed_https_path_no_redirects_or_arbitrary_url(self):
        result,c=self.fetch(body=b'{"response":{"players":[]}}')
        self.assertEqual(result,{'response':{'players':[]}})
        method,path=c.request.call_args.args
        self.assertEqual(method,'GET');self.assertTrue(path.startswith('/ISteamUser/GetPlayerSummaries/v2/?'))
        self.assertIn('steamids='+SID,path)
        with self.assertRaises(SteamFailure) as e:self.fetch(status=302)
        self.assertEqual(e.exception.code,'steam_http_error')
    def test_429_retry_after_and_secret_not_in_error(self):
        with self.assertRaises(SteamFailure) as e:self.fetch(status=429,header='17')
        self.assertEqual(e.exception.retry_after,17);self.assertNotIn('a'*32,str(e.exception))
    def test_403_invalid_json_and_response_limit(self):
        for status,body,code in [(403,b'','steam_auth_failed'),(200,b'invalid','steam_upstream_error'),(200,b'x'*2097153,'steam_response_too_large')]:
            with self.subTest(status=status,size=len(body)),self.assertRaises(SteamFailure) as e:self.fetch(status,body)
            self.assertEqual(e.exception.code,code)
    def test_missing_or_bad_key_never_connects(self):
        from unittest.mock import patch
        for text in ['', 'bad']:
            self.path.write_text(text)
            with patch('service.steam_profiles.http.client.HTTPSConnection') as connection:
                with self.assertRaises(SteamFailure):self.transport.fetch('profile',[SID])
                connection.assert_not_called()
    def test_http_date_retry_after(self):
        from service.steam_profiles import retry_seconds
        self.assertEqual(retry_seconds('Thu, 01 Jan 1970 00:17:10 GMT',1000),30)
        self.assertEqual(retry_seconds('nonsense',1000),60)


class SteamBanTimeoutTests(unittest.TestCase):
    def test_ban_endpoint_has_longer_bounded_timeout(self):
        from unittest.mock import patch,MagicMock
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*32)
            connection=MagicMock();connection.getresponse.return_value.status=200
            connection.getresponse.return_value.read.return_value=b'{"players":[]}'
            with patch('service.steam_profiles.http.client.HTTPSConnection',return_value=connection) as factory:
                self.assertEqual(SteamTransport(key).fetch('bans',[SID]),{'players':[]})
            self.assertEqual(factory.call_args.kwargs['timeout'],15)
