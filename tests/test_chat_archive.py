import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from email.message import Message
from event_receiver.local_cache import Cache
SID='76561190000000001';OTHER='76561190000000002'
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat().replace('+00:00','Z')
def rmsg(seq=1,sent=1000,text='Fixture chat',**kw):
 d={'seq':seq,'sentUtc':iso(sent),'steamId':SID,'realName':'Fixture','hiddenName':'Shadow','anonymous':False,'ping':False,'channel':'Global','faction':'Lonestar','text':text,'rawText':text};d.update(kw);return d
def event(eid='a',text='Fixture chat',**kw):
 d={'type':'chat_message','eventId':eid,'eventTime':50,'playerSteamId':SID,'playerName':'Fixture','hiddenName':'Shadow','anonymous':False,'ping':False,'channel':'Global','faction':'Lonestar','text':text,'rawText':text};d.update(kw);return d
class ChatArchiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.cache=Cache(Path(self.tmp.name)/'db');self.db=self.cache.archive.db;self.seq=0
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def rcon(self,messages,server='server-a',latest=None,now=1003,after=0):
  self.cache.apply_chat({'serverId':server,'after':after,'chat':{'messages':messages,'count':len(messages),'latestSeq':latest if latest is not None else max([m['seq'] for m in messages] or [0])}},now)
 def callback(self,obj=None,received=1002,server='server-a'):
  self.seq+=1;self.cache.apply_events({'generation':'remote','nextAfter':str(self.seq),'latestSequence':str(self.seq),'hasMore':False,'events':[{'sequence':str(self.seq),'serverId':server,'receivedAt':received,'event':obj or event()}]},1003);self.cache.index_chat_events(1003)
 def counts(self):return tuple(self.db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ['chat_observations','chat_links'])
 def test_both_arrival_orders_merge_without_losing_evidence(self):
  self.rcon([rmsg()]);self.callback();self.assertEqual(self.counts(),(2,1))
  self.callback(event('b',text='Other'),1005);self.rcon([rmsg(2,1004,'Other')]);self.assertEqual(self.counts(),(4,2))
 def test_same_source_retries_are_idempotent(self):
  self.rcon([rmsg()]);self.rcon([rmsg()],now=1004);self.callback();self.assertEqual(self.counts(),(2,1));self.cache.index_chat_events(1005);self.assertEqual(self.counts(),(2,1))
 def test_same_event_id_across_servers_not_merged(self):
  self.rcon([rmsg()]);self.callback(server='server-b');self.assertEqual(self.counts(),(2,0))
 def test_shared_nickname_not_identity(self):
  self.rcon([rmsg()]);self.callback(event(playerSteamId=OTHER));self.assertEqual(self.counts(),(2,0))
 def test_text_whitespace_channel_faction_flags_strict(self):
  for i,change in enumerate([{'text':'Fixture chat '},{'channel':'Team'},{'faction':'Manticore'},{'anonymous':True},{'ping':True},{'rawText':'changed'}]):
   self.callback(event(str(i),**change))
  self.rcon([rmsg()]);self.assertEqual(self.counts(),(7,0))
 def test_timing_edges_and_outside(self):
  for i,delta in enumerate([-2,10,-2.001,10.001],1):
   text='message'+str(i);self.rcon([rmsg(i,1000,text)]);self.callback(event(str(i),text=text),1000+delta)
  self.assertEqual(self.counts(),(8,2))
 def test_late_duplicate_revokes_earlier_unique_link(self):
  self.rcon([rmsg()]);self.callback();self.assertEqual(self.counts(),(2,1));self.rcon([rmsg(2,1001)]);self.assertEqual(self.counts(),(3,0));self.assertEqual(self.db.execute("select candidate_count from chat_observations where source='callback'").fetchone()[0],2)
 def test_duplicate_callbacks_are_ambiguous_not_collapsed(self):
  self.rcon([rmsg()]);self.callback();self.callback(event('second'),1003);self.assertEqual(self.counts(),(3,0))
 def test_sequence_reuse_after_restart_keeps_two_messages(self):
  self.rcon([rmsg()],now=1003);self.rcon([rmsg(1,2000)],now=2003);self.assertEqual(self.counts(),(2,0));self.assertEqual(self.cache.chats.cursor()['after'],'0')
 def test_same_sequence_timestamp_conflict_fails_without_overwrite(self):
  self.rcon([rmsg()])
  with self.assertRaises(ValueError):self.rcon([rmsg(text='Changed')])
  self.assertEqual(self.counts(),(1,0));self.assertEqual(self.db.execute('select text from chat_observations').fetchone()[0],'Fixture chat')
 def test_unknown_identity_not_inferred_from_nickname(self):
  self.rcon([rmsg(steamId='bad')]);self.callback(event(playerSteamId=None));self.assertEqual(self.counts(),(2,0));self.assertEqual(self.db.execute('select count(*) from chat_aliases').fetchone()[0],0)
 def test_pagination_caught_up_and_reset_cursor(self):
  self.rcon([rmsg(1)],latest=2);self.assertFalse(self.cache.chats.meta('rcon')['caughtUp']);self.assertEqual(self.cache.chats.cursor()['after'],'1')
  self.rcon([rmsg(2,1001)],latest=2,after=1);self.assertTrue(self.cache.chats.meta('rcon')['caughtUp']);self.assertEqual(self.cache.chats.cursor()['after'],'0')
  self.rcon([],latest=1,after=2);self.assertFalse(self.cache.chats.meta('rcon')['caughtUp']);self.assertEqual(self.cache.chats.cursor()['after'],'0')
 def test_bad_utc_and_partial_pages_rejected(self):
  for value in [None,'2026-11-14T01:00:00','bad']:
   with self.assertRaises(ValueError):self.rcon([rmsg(sentUtc=value)])
  with self.assertRaises(ValueError):self.cache.apply_chat({'serverId':'s','chat':{'latestSeq':1,'count':1,'messages':[rmsg()],'complete':False}},1003)
 def test_chat_index_failure_does_not_rollback_kill_or_event_sync(self):
  def fetch(kind,params):return {'generation':'g','nextAfter':'1','latestSequence':'1','hasMore':False,'events':[{'sequence':'1','serverId':'s','receivedAt':1000,'event':event(text=None)}]}
  self.cache.cycle(fetch,now=1003,kinds=('events',));self.assertEqual(self.db.execute("select last_success,error from sync_state where kind='events'").fetchone(),(1003,None));self.assertEqual(self.db.execute('select count(*) from events').fetchone()[0],1);self.assertEqual(self.cache.chats.meta('callback_error'),'ValueError')
 def test_chat_429_cannot_be_cleared_by_successful_sibling_group(self):
  headers=Message();headers['Retry-After']='60'
  def fetch(kind,params):
   self.cache.defer('players',headers)
   return {'serverId':'s','after':0,'chat':{'latestSeq':0,'count':0,'messages':[]}}
  self.cache.cycle(fetch,kinds=('chat',));self.assertTrue(self.cache.deferred('players'));self.assertTrue(self.cache.deferred('chat'))
 def test_missing_raw_text_is_not_assumed_equal_to_provided_raw(self):
  self.rcon([rmsg(rawText=None)]);self.callback();self.assertEqual(self.counts(),(2,0))
 def test_index_backfill_is_bounded_and_reports_progress(self):
  self.cache.apply_events({'generation':'remote','nextAfter':'0','latestSequence':'0','hasMore':False,'events':[]},1003)
  with self.db:
   for i in range(3):self.db.execute('INSERT INTO events VALUES(?,?,?,?,?)',('s','id:'+str(i),1000,'chat_message',json.dumps(event(str(i),text=str(i)))))
   self.cache.chats.index_callbacks(1003,limit=2)
  after=self.cache.chats.meta('callback')['afterRowid'];self.assertLess(after,self.db.execute('select max(rowid) from events').fetchone()[0])
  with self.db:self.cache.chats.index_callbacks(1004,limit=2)
  self.assertEqual(self.db.execute('select count(*) from chat_observations').fetchone()[0],3)
 def test_chat_fetcher_only_gets_and_fences_server_identity(self):
  from unittest.mock import patch
  import event_receiver.local_cache as module
  calls=[]
  class Response:
   def __init__(self,value):self.value=value
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,*a):return json.dumps(self.value).encode()
  class Opener:
   def open(inner,request,**kw):
    calls.append((request.get_method(),request.full_url))
    return Response({'serverId':'server-a'} if request.full_url.endswith('/server-id') else {'latestSeq':0,'count':0,'messages':[]})
  with patch.object(module,'build_opener',return_value=Opener()):
   result=module.fetcher('fixture-events','fixture-rcon')('chat',{'after':'0','limit':'1000'})
  self.assertEqual(result['serverId'],'server-a');self.assertEqual([method for method,url in calls],['GET','GET','GET']);self.assertEqual(len(calls),3)
 def test_restarted_server_during_fetch_rejects_mixed_identity(self):
  from unittest.mock import patch
  import event_receiver.local_cache as module
  n=[0]
  class Response:
   def __init__(self,value):self.value=value
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,*a):return json.dumps(self.value).encode()
  class Opener:
   def open(inner,request,**kw):
    n[0]+=1;return Response({'serverId':'server-a' if n[0]==1 else 'server-b'} if request.full_url.endswith('/server-id') else {'latestSeq':0,'count':0,'messages':[]})
  with patch.object(module,'build_opener',return_value=Opener()):
   with self.assertRaises(ValueError):module.fetcher('fixture-events','fixture-rcon')('chat',{'after':'0','limit':'1000'})
