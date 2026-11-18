import json,sqlite3,tempfile,unittest,threading,http.client,time
from pathlib import Path
from unittest.mock import patch
import test_monitor as f
from test_admin_moderation import config,admin_payload,ID
from test_chat_archive import rmsg,event,OTHER
from event_receiver.local_cache import Cache
from service.moderation_ops import ContractError

class PlayerChatsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'local-cache/events.sqlite3';self.path.parent.mkdir();self.cache=Cache(self.path);self.fake=f.FakeRcon();self.e=f.app.Engine(self.root/'state.db',self.fake,config());self.seq=0
  self.cache.apply_events({'generation':'remote','nextAfter':'0','latestSequence':'0','hasMore':False,'events':[]},1003);self.cache.apply_chat({'serverId':'s','chat':{'latestSeq':0,'count':0,'messages':[]}},1003)
 def tearDown(self):self.e.close();self.cache.archive.db.close();self.tmp.cleanup()
 def p(self,**kw):return dict(steamId=ID,actor=admin_payload()['actor'],requestedAt=1003,**kw)
 def query(self,p=None,now=1003):return self.e.player_chats.query(self.p() if p is None else p,now=now)
 def rcon(self,messages,server='s'):
  self.cache.apply_chat({'serverId':server,'chat':{'latestSeq':max([x['seq'] for x in messages] or [0]),'messages':messages,'count':len(messages)}},1003)
 def callback(self,obj=None,received=1002,server='s'):
  self.seq+=1;self.cache.apply_events({'generation':'remote','nextAfter':str(self.seq),'latestSequence':str(self.seq),'hasMore':False,'events':[{'sequence':str(self.seq),'serverId':server,'receivedAt':received,'event':obj or event()}]},1003);self.cache.index_chat_events(1003)
 def test_empty_readonly_no_remote_or_jobs(self):
  r=self.query();self.assertEqual(r['messages'],[]);self.assertTrue(r['complete']);self.assertFalse(r['hasMore']);self.assertFalse(r['coverage']['historyComplete']);self.assertEqual(self.fake.paths,[]);self.assertEqual(self.e.queue_depth(),0)
 def test_merge_has_both_evidence_and_sent_time(self):
  self.rcon([rmsg()]);self.callback();r=self.query();self.assertEqual(len(r['messages']),1);m=r['messages'][0];self.assertEqual(m['sources'],['rcon','callback']);self.assertEqual(m['timestamp'],1000);self.assertEqual(m['timeBasis'],'rcon_sent_utc');self.assertEqual(m['correlation']['status'],'time_correlated');self.assertEqual(len(m['evidence']),2);self.assertNotIn('actor',r)
 def test_callback_only_has_received_time_not_game_time(self):
  self.callback(event(eventTime=777));m=self.query()['messages'][0];self.assertEqual(m['timestamp'],1002);self.assertEqual(m['timeBasis'],'callback_archive_received_at');self.assertEqual(m['evidence'][0]['gameTimeSeconds'],777)
 def test_ambiguous_short_repeats_not_falsely_collapsed(self):
  self.rcon([rmsg(),rmsg(2,1001)]);self.callback();m=self.query()['messages'];self.assertEqual(len(m),3);self.assertTrue(all(x['correlation']['status']=='ambiguous' for x in m))
 def test_name_and_past_alias_resolve_exact_identity(self):
  self.rcon([rmsg(realName='曾用名')]);p=self.p();del p['steamId'];p['query']='曾用';r=self.query(p);self.assertEqual(r['steamId'],ID)
  with self.e.db:self.e.player_history.observe(ID,'Old Alias','fixture',900,900)
  p['query']='old alias';self.assertEqual(self.query(p)['steamId'],ID)
 def test_shared_nickname_returns_candidates_without_messages(self):
  self.rcon([rmsg(),rmsg(2,1001,steamId=OTHER)]);p=self.p();del p['steamId'];p['query']='fixture';r=self.query(p);self.assertEqual(r['status'],'candidates');self.assertEqual(len(r['players']),2);self.assertEqual(r['messages'],[])
 def test_unknown_source_identity_never_inferred_from_name(self):
  self.rcon([rmsg(steamId='bad')]);self.assertEqual(self.query()['messages'],[]);p=self.p();del p['steamId'];p['query']='Fixture'
  with self.assertRaises(ContractError) as ex:self.query(p)
  self.assertEqual(ex.exception.status,404)
 def test_id_string_query_supported(self):
  self.rcon([rmsg()]);p=self.p();del p['steamId'];p['query']=ID;self.assertEqual(self.query(p)['steamId'],ID)
 def test_limits_has_more_channel_and_latest_order(self):
  self.rcon([rmsg(i+1,900+i,text=str(i),channel='Global' if i%2 else 'Team') for i in range(55)])
  r=self.query();self.assertEqual(len(r['messages']),20);self.assertTrue(r['hasMore']);self.assertEqual(r['messages'][0]['text'],'54')
  r=self.query(self.p(limit=50,channel='Team'));self.assertEqual(len(r['messages']),28);self.assertFalse(r['hasMore']);self.assertTrue(all(m['channel']=='Team' for m in r['messages']))
 def test_cross_server_observations_retained(self):
  self.rcon([rmsg()]);self.callback(server='other');self.assertEqual(len(self.query()['messages']),2)
 def test_invalid_fields_limits_filters(self):
  for value in [True,0,-1,51,'20',20.0,[]]:
   with self.assertRaises(ContractError) as ex:self.query(self.p(limit=value))
   self.assertEqual(ex.exception.status,400)
  for value in [True,None,[],{},'all','global']:
   with self.assertRaises(ContractError) as ex:self.query(self.p(channel=value))
   self.assertEqual(ex.exception.status,400)
  for p in [dict(self.p(),query='Fixture'),dict(self.p(),offset=1),dict(self.p(),steamId='bad')]:
   with self.assertRaises(ContractError) as ex:self.query(p)
   self.assertEqual(ex.exception.status,400)
 def test_roles_scopes_and_expiry(self):
  for key,value in [('role','member'),('botId','bad'),('groupId','bad'),('platform','bad')]:
   p=self.p();p['actor'][key]=value
   with self.assertRaises(ContractError) as ex:self.query(p)
   self.assertEqual(ex.exception.status,403)
  p=self.p();p['requestedAt']=1
  with self.assertRaises(ContractError) as ex:self.query(p)
  self.assertEqual(ex.exception.status,422)
 def test_stale_source_and_errors_not_old_success(self):
  self.query(now=1018)
  with self.assertRaises(ContractError):self.query(now=1018.001)
  with self.cache.archive.db:self.cache.archive.db.execute("update sync_state set error='HTTPError' where kind='chat'")
  with self.assertRaises(ContractError) as ex:self.query()
  self.assertEqual(ex.exception.code,'player_chats_sources_unavailable')
 def test_pending_callback_index_and_rcon_pages_fail_closed(self):
  self.cache.chats.setmeta('callback_error','ValueError');self.cache.archive.db.commit()
  with self.assertRaises(ContractError) as ex:self.query()
  self.assertEqual(ex.exception.code,'player_chats_index_incomplete')
  self.cache.chats.setmeta('callback_error',None);self.cache.chats.setmeta('rcon',{'caughtUp':False});self.cache.archive.db.commit()
  with self.assertRaises(ContractError):self.query()
 def test_pending_remote_cursor_fails_closed(self):
  with self.cache.archive.db:self.cache.archive.db.execute('update sync_cursor set latest_seq=1')
  with self.assertRaises(ContractError) as ex:self.query()
  self.assertEqual(ex.exception.code,'player_chats_sync_incomplete')
 def test_response_cap_does_not_silently_trim_text(self):
  self.rcon([rmsg(i+1,900+i,'x'*8192) for i in range(10)])
  with self.assertRaises(ContractError) as ex:self.query()
  self.assertEqual(ex.exception.code,'player_chats_response_too_large')
 def test_http_admin_only_and_no_network_fallback(self):
  now=time.time()
  with self.cache.archive.db:self.cache.archive.db.execute('update sync_state set last_success=?',(now,))
  server=f.app.ThreadingHTTPServer(('127.0.0.1',0),f.app.api_handler(self.e,'query','report','admin'));thread=threading.Thread(target=server.serve_forever);thread.start()
  try:
   for token,role,status in [('query','admin',401),('report','admin',401),(None,'admin',401),('admin','member',403),('admin','admin',200)]:
    p=self.p();p['requestedAt']=now;p['actor']['role']=role;headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    con=http.client.HTTPConnection(*server.server_address,timeout=3);con.request('POST','/v1/moderation/player-chats',json.dumps(p),headers);res=con.getresponse();body=json.loads(res.read());con.close();self.assertEqual(res.status,status,body)
  finally:server.shutdown();server.server_close();thread.join()
  self.assertEqual(self.fake.paths,[])
 def test_corrupt_metadata_fails_503_not_http_disconnect(self):
  with self.cache.archive.db:self.cache.chats.setmeta('rcon',None)
  with self.assertRaises(ContractError) as ex:self.query()
  self.assertEqual(ex.exception.status,503)
 def test_candidate_list_truncated_not_claimed_complete(self):
  self.rcon([rmsg(i+1,900+i,realName='Shared',steamId=str(76561190000000100+i)) for i in range(21)])
  p=self.p();del p['steamId'];p['query']='shared';r=self.query(p);self.assertEqual(r['status'],'candidates');self.assertEqual(len(r['players']),20);self.assertTrue(r['truncated']);self.assertFalse(r['complete'])
 def test_stale_at_query_finish_rejected(self):
  with patch('service.player_chats.time.time',side_effect=[1003,1003,1020,1020]):
   with self.assertRaises(ContractError):self.e.player_chats.query(self.p())
