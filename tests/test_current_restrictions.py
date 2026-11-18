"""Current-state provenance; never infer current bans from a historical kick."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_monitor as fixtures
import test_admin_tools as tools
from test_admin_moderation import ID, admin_payload, config
from test_burst_guard import Rcon
from service.moderation_ops import ContractError

OTHER = '76561190000000002'


class NativeFake(Rcon):
    def __init__(self):
        super().__init__();self.calls=[];self.generation='2026-10-28T01:00:00Z';self.fail_read=None
    def request(self, method, path, body=None):
        self.calls.append((method,path))
        if method=='GET' and path==self.fail_read:raise TimeoutError('private')
        if method=='GET' and path=='/v1/bans':
            return {'bans':[{'steamId':sid,'bannedAtUtc':self.generation,'bannedBy':'private-actor','reason':'fixture'} for sid in self.bans]}
        return super().request(method,path,body)


class CurrentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db';self.cfg=config();self.cfg.update(rejoin_guard_enabled=True,auto_native_ban_enabled=True)
        self.fake=NativeFake();self.e=fixtures.app.Engine(self.path,self.fake,self.cfg);self.g=self.e.kick_store
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def payload(self, ids=None):return {'steamIds':ids or [ID],'actor':admin_payload()['actor'],'requestedAt':1000}
    def query(self, ids=None):return self.e.current_restrictions.query(self.payload(ids),now=1001)
    def row(self):return self.query()['players'][0]
    def automatic(self, source='auto_native'):
        self.g.restrict(ID,'Fixture','r',source,900,action_id='origin')
    def test_empty_is_complete_offline_unrestricted_not_automatic(self):
        r=self.query();p=r['players'][0]
        self.assertTrue(r['complete']);self.assertFalse(p['online']);self.assertFalse(p['localRestricted']);self.assertFalse(p['nativeBanned']);self.assertIs(p['currentAutomaticRestriction'],False)
    def test_current_exact_automatic_local_action(self):
        self.automatic();r=self.row();self.assertTrue(r['localRestricted']);self.assertTrue(r['currentAutomaticRestriction']);self.assertEqual(r['localSource'],{'category':'automatic','code':'auto_native'})
    def test_manual_current_is_not_automatic_despite_old_automatic(self):
        self.automatic();self.g.restrict(ID,'Fixture','manual','admin_offline',950,action_id='manual')
        self.assertFalse(self.row()['currentAutomaticRestriction'])
    def test_release_removes_current_automatic_classification(self):
        self.automatic();self.g.release(ID,'unban',950);p=self.row()
        self.assertFalse(p['localRestricted']);self.assertFalse(p['currentAutomaticRestriction']);self.assertEqual(p['localSource']['category'],'none')
    def test_old_auto_kick_does_not_make_new_manual_restriction_automatic(self):
        self.g.confirm(ID,'Fixture','r','auto_kd','permanent',900,event_id='auto')
        self.g.release(ID,'unban',950);self.g.restrict(ID,'Fixture','r','manual',980,action_id='manual')
        self.assertFalse(self.row()['currentAutomaticRestriction'])
    def test_legacy_native_or_rejoin_source_is_unknown(self):
        for source in ['monitor','native_ban','rejoin','untrusted-source']:
            self.g.confirm(ID,'Fixture','r',source,'permanent',900,event_id=source)
            r=self.row();self.assertIsNone(r['currentAutomaticRestriction']);self.assertFalse(r['sourceComplete']);self.assertEqual(r['localSource'],{'category':'unknown','code':None})
    def test_legacy_exact_current_action_without_marker_is_resolvable(self):
        self.automatic()
        with self.g.db:self.g.db.execute('DELETE FROM restriction_provenance')
        self.assertTrue(self.row()['currentAutomaticRestriction'])
    def test_unrelated_other_player_same_event_id_does_not_prove_source(self):
        self.automatic()
        with self.g.db:
            self.g.db.execute('DELETE FROM restriction_provenance')
            self.g.db.execute('UPDATE permanent_ban_actions SET steam_id=?',(OTHER,))
        self.assertIsNone(self.row()['currentAutomaticRestriction'])
    def test_rejoin_confirmation_preserves_explicit_origin(self):
        self.automatic();self.g.confirm(ID,'Fixture','r','rejoin','permanent',1000,event_id='new',continuation_event_id='origin')
        self.assertTrue(self.row()['currentAutomaticRestriction'])
    def test_wrong_continuation_does_not_carry_automatic_origin(self):
        self.automatic();self.g.confirm(ID,'Fixture','r','rejoin','permanent',1000,event_id='new',continuation_event_id='wrong')
        self.assertIsNone(self.row()['currentAutomaticRestriction'])
    def test_manual_confirm_replaces_auto_origin(self):
        self.automatic();self.g.confirm(ID,'Fixture','r','manual','permanent',1000,event_id='new')
        self.assertFalse(self.row()['currentAutomaticRestriction'])
    def test_release_watermark_invalidates_recreated_old_pointer(self):
        self.automatic();self.g.release(ID,'unban',950)
        with self.g.db:self.g.db.execute("INSERT INTO rejoin_restrictions VALUES(?,?,'permanent','Fixture','r',900,'active')",(ID,'origin'))
        self.assertIsNone(self.row()['currentAutomaticRestriction'])
    def test_native_membership_not_sync_receipt_controls_banned(self):
        self.automatic();self.g.request_native_bans([ID],900);self.g.native_ban_update(ID,'confirmed')
        r=self.row();self.assertFalse(r['nativeBanned']);self.assertTrue(r['localRestricted'])
    def test_native_only_unattributed_is_banned_unknown(self):
        self.fake.bans=[ID];p=self.row();self.assertTrue(p['nativeBanned']);self.assertFalse(p['localRestricted']);self.assertIsNone(p['currentAutomaticRestriction'])
    def test_native_success_has_generation_bound_automatic_provenance(self):
        self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()]
        with patch('service.kick_guard.time.time',return_value=1000):self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent')
        p=self.row();self.assertTrue(p['nativeBanned']);self.assertEqual(p['nativeSource'],{'category':'automatic','code':'auto_native'});self.assertTrue(p['currentAutomaticRestriction'])
        self.fake.generation='2026-10-28T02:00:00Z'
        self.assertEqual(self.row()['nativeSource']['category'],'unknown')
    def test_preexisting_native_ban_not_claimed_as_automatic(self):
        self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()];self.fake.bans=[ID]
        with patch('service.kick_guard.time.time',return_value=1000):self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent')
        self.assertEqual(self.row()['nativeSource']['category'],'unknown')
    def test_release_clears_native_origin_even_if_remote_ban_remains(self):
        self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()]
        with patch('service.kick_guard.time.time',return_value=1000):self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent')
        self.assertEqual(self.row()['nativeSource']['category'],'automatic')
        self.g.release(ID,'unban',1000.5)
        p=self.row();self.assertFalse(p['localRestricted']);self.assertTrue(p['nativeBanned']);self.assertIsNone(p['currentAutomaticRestriction'])
    def test_native_without_generation_cannot_be_attributed(self):
        self.fake.generation='';self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()]
        self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent');self.assertEqual(self.row()['nativeSource']['category'],'unknown')
    def test_all_automatic_sources_are_explicit_whitelist(self):
        for i,source in enumerate(['auto_native','auto_kd','auto_burst','auto_weapon']):
            self.g.restrict(ID,'Fixture','r',source,900+i,action_id=source)
            self.assertTrue(self.row()['currentAutomaticRestriction'])
    def test_current_target_order_and_online_only_exact_roster(self):
        self.fake.players=[fixtures.player(identity=OTHER)]
        p=self.query([OTHER,ID])['players'];self.assertEqual([x['steamId'] for x in p],[OTHER,ID]);self.assertTrue(p[0]['online']);self.assertFalse(p[1]['online'])
    def test_roster_and_native_failures_are_503(self):
        for path in ['/v1/players','/v1/bans']:
            self.fake.fail_read=path
            with self.assertRaises(ContractError) as e:self.query()
            self.assertEqual(e.exception.status,503)
    def test_invalid_or_partial_roster_never_becomes_offline(self):
        original=self.fake.request
        for body in [{'players':[{'steamId':'bad'}]},{'players':[fixtures.player(),fixtures.player()]},{'players':[],'pending':True}]:
            def read(method,path,payload=None):return body if path=='/v1/players' else original(method,path,payload)
            with patch.object(self.fake,'request',side_effect=read):
                with self.assertRaises(ContractError):self.query()
    def test_ids_strict_and_bounded(self):
        for ids in [[],[ID,ID],['bad'],[True],['７'*17],[str(76561190000000000+i) for i in range(101)]]:
            with self.assertRaises(ContractError):self.e.current_restrictions.query(dict(self.payload(),steamIds=ids),now=1001)
    def test_roles_scopes_time_and_extra_fields(self):
        for key,value in [('role','member'),('platform','other'),('botId','other'),('groupId','other')]:
            p=copy.deepcopy(self.payload());p['actor'][key]=value
            with self.assertRaises(ContractError):self.e.current_restrictions.query(p,now=1001)
        for p in [dict(self.payload(),requestedAt=800),dict(self.payload(),extra=True)]:
            with self.assertRaises(ContractError):self.e.current_restrictions.query(p,now=1001)
    def test_readonly_query_does_not_mutate_or_leak(self):
        self.automatic();before=(self.g.db.total_changes,self.e.db.total_changes);p=self.row()
        self.assertEqual(before,(self.g.db.total_changes,self.e.db.total_changes));self.assertTrue(all(m=='GET' for m,path in self.fake.calls))
        text=json.dumps(p);self.assertNotIn('private-actor',text);self.assertNotIn('actor',text);self.assertNotIn('Fixture',text)
    def test_local_change_during_network_lookup_is_not_complete(self):
        original=self.fake.request
        def read(method,path,payload=None):
            if path=='/v1/bans':self.g.restrict(ID,'Fixture','r','manual',950,action_id='changed')
            return original(method,path,payload)
        self.fake.request=read
        with self.assertRaises(ContractError) as e:self.query()
        self.assertEqual(e.exception.code,'current_restrictions_changing')
    def test_unknown_or_held_target_lease_prevents_safe_classification(self):
        for outcome in ['held','unknown']:
            with self.e.db:self.e.db.execute('INSERT OR REPLACE INTO moderation_targets VALUES(?,?,?,NULL)',(ID,'fixture',outcome))
            with self.assertRaises(ContractError):self.query()
    def test_scope_revocation_after_lookup_rejected(self):
        original=self.fake.request
        def read(method,path,payload=None):
            self.e.config['admin_moderation_scopes']=[];return original(method,path,payload)
        self.fake.request=read
        with self.assertRaises(ContractError):self.query()
    def test_over_15_seconds_rejected(self):
        stamp=[1000];original=self.fake.request
        def read(method,path,payload=None):stamp[0]=1016;return original(method,path,payload)
        self.fake.request=read
        with patch('service.current_restrictions.time.time',side_effect=lambda:stamp[0]):
            with self.assertRaises(ContractError):self.e.current_restrictions.query(self.payload())

    def test_native_timeout_readback_does_not_prove_automatic_creation(self):
        self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()]
        original=self.fake.request
        def read(method,path,body=None):
            result=original(method,path,body)
            if method=='POST' and path=='/v1/bans':raise TimeoutError('uncertain')
            return result
        with patch.object(self.fake,'request',side_effect=read),patch('service.kick_guard.time.time',return_value=1000):
            self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent')
        self.assertTrue(self.row()['nativeBanned']);self.assertEqual(self.row()['nativeSource']['category'],'unknown')
        self.assertEqual(sum(m=='POST' and p=='/v1/bans' for m,p in self.fake.calls),1)

    def test_native_current_generation_survives_manual_local_override(self):
        self.automatic();self.g.request_native_bans([ID],900);self.fake.players=[fixtures.player()]
        with patch('service.kick_guard.time.time',return_value=1000):self.e.rcon._kick(ID,{'reason':'r'},expected_scope='permanent')
        self.g.restrict(ID,'Fixture','manual','manual',1000.5,action_id='new-manual')
        p=self.row();self.assertEqual(p['localSource']['category'],'manual');self.assertEqual(p['nativeSource']['category'],'automatic');self.assertTrue(p['currentAutomaticRestriction'])

    def test_old_writer_overwrite_does_not_reuse_stale_marker(self):
        self.automatic()
        with self.g.db:
            self.g.db.execute("INSERT INTO confirmed_kicks VALUES(?,?,'Fixture','r','rejoin',1000,1000,'permanent')",('old-writer',ID))
            self.g.db.execute('UPDATE rejoin_restrictions SET event_id=? WHERE steam_id=?',('old-writer',ID))
        self.assertIsNone(self.row()['currentAutomaticRestriction'])

    def test_query_after_restart_preserves_only_current_marker(self):
        self.automatic();self.e.close();self.e=fixtures.app.Engine(self.path,self.fake,self.cfg);self.g=self.e.kick_store
        self.assertTrue(self.row()['currentAutomaticRestriction'])
        self.g.release(ID,'unban',950);self.e.close();self.e=fixtures.app.Engine(self.path,self.fake,self.cfg);self.g=self.e.kick_store
        self.assertFalse(self.row()['currentAutomaticRestriction'])

    def test_unknown_local_kick_outcome_still_has_registered_policy(self):
        self.automatic()
        with self.g.db:self.g.db.execute("UPDATE rejoin_restrictions SET status='unknown' WHERE steam_id=?",(ID,))
        p=self.row();self.assertTrue(p['localRestricted']);self.assertEqual(p['localRestrictionStatus'],'unknown');self.assertTrue(p['currentAutomaticRestriction'])

    def test_target_only_no_other_players_or_raw_receipts_returned(self):
        self.fake.players=[fixtures.player(identity=OTHER)];self.fake.bans=[OTHER]
        text=json.dumps(self.query());self.assertNotIn(OTHER,text);self.assertNotIn('private-actor',text);self.assertNotIn('bannedBy',text);self.assertNotIn('reason',text)

    def test_partial_or_count_mismatched_native_list_not_empty_bans(self):
        original=self.fake.request
        for body in [{'bans':[],'complete':False},{'bans':[],'hasMore':True},{'bans':[],'count':1}]:
            def read(method,path,payload=None):return body if path=='/v1/bans' else original(method,path,payload)
            with patch.object(self.fake,'request',side_effect=read):
                with self.assertRaises(ContractError):self.query()

    def test_unrelated_target_change_does_not_invalidate_requested_ids(self):
        original=self.fake.request
        def read(method,path,payload=None):
            if path=='/v1/bans':self.g.restrict(OTHER,'Other','r','manual',950,action_id='other-action')
            return original(method,path,payload)
        self.fake.request=read;self.assertTrue(self.query()['complete'])

    def test_batch_upper_boundary_and_no_truncation(self):
        ids=[str(76561190000000100+i) for i in range(100)]
        result=self.query(ids);self.assertEqual([p['steamId'] for p in result['players']],ids);self.assertLess(len(json.dumps(result)),65536)


class CurrentHTTPTests(unittest.TestCase):
    setUp=tools.ToolsHTTPTests.setUp
    tearDown=tools.ToolsHTTPTests.tearDown
    request=tools.ToolsHTTPTests.request
    def payload(self):return {'steamIds':[ID],'actor':admin_payload()['actor'],'requestedAt':time.time()}
    def test_http_admin_only_and_read_only_success(self):
        for key in ['query','report','wrong']:
            self.assertEqual(self.request('POST','/v1/moderation/current-restrictions',self.payload(),key)[0],401)
        status,data=self.request('POST','/v1/moderation/current-restrictions',self.payload())
        self.assertEqual(status,200);self.assertTrue(data['complete']);self.assertEqual(data['players'][0]['steamId'],ID);self.assertIsNone(data['players'][0]['currentAutomaticRestriction'])
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM moderation_operations').fetchone()[0],0)
    def test_http_member_and_extra_fields_fail(self):
        p=self.payload();p['actor']['role']='member';self.assertEqual(self.request('POST','/v1/moderation/current-restrictions',p)[0],403)
        p=self.payload();p['extra']=1;self.assertEqual(self.request('POST','/v1/moderation/current-restrictions',p)[0],400)
