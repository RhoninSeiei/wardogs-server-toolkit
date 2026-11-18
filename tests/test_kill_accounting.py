import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
import test_monitor as f
import test_burst_guard as bf
from service.kill_accounting import KillAccounting,barbed_wire,BARBED_WIRE


class AccountingTests(unittest.TestCase):
    tearDown = f.MonitorTests.tearDown
    # Do not inherit the unrelated monitor scenarios a second time.
    def setUp(self):
        f.MonitorTests.setUp(self)
        self.cache=self.path.parent/'local-cache/events.sqlite3';self.cache.parent.mkdir()
        with closing(sqlite3.connect(self.cache)) as c,c:
            c.executescript('''CREATE TABLE events(server TEXT,event_key TEXT,received REAL,type TEXT,body TEXT, UNIQUE(server,event_key));
                CREATE TABLE sync_state(kind TEXT,last_success REAL,error TEXT);
                INSERT INTO sync_state VALUES('events',1000,NULL);
                CREATE TABLE sync_cursor(id INTEGER,generation TEXT,after_seq INTEGER,latest_seq INTEGER);
                INSERT INTO sync_cursor VALUES(1,'gen',1,1);''')
        self.add('start','match_state',{'state':'MatchState.PreMatch'})
        self.accounting=self.engine.kill_accounting

    def add(self,key,kind='killed',extra=None,server='server'):
        body=dict(matchId='m',mapName='map',cause=BARBED_WIRE,killerSteamId=f.player()['steamId'],victimSteamId='76561190000000002')
        body.update(extra or {})
        with closing(sqlite3.connect(self.cache)) as c,c:
            c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(server,key,1000,kind,json.dumps(body)))

    def adjusted(self,kills=15,deaths=1):
        return self.accounting.adjust(f.player(kills,deaths),self.accounting.counts(1000))

    def test_adjust_only_wire_no_mutation_or_death_change(self):
        self.add('wire');self.add('gun',extra={'cause':'Id.Item.SV98'})
        original=f.player(15,2);adjusted=self.accounting.adjust(original,self.accounting.counts(1000))
        self.assertEqual(adjusted['kills'],14);self.assertEqual(adjusted['deaths'],2);self.assertEqual(original['kills'],15)
        self.assertEqual(f.app.statistics(adjusted),(14,2,7))

    def test_exact_cause_only_and_duplicate(self):
        self.add('wire');self.add('wire');self.add('fake',extra={'cause':BARBED_WIRE+'.Fake'})
        self.assertEqual(self.adjusted()['kills'],14)
        self.assertFalse(barbed_wire({'cause':BARBED_WIRE+'.Fake'}))

    def test_self_and_other_identity_excluded(self):
        self.add('self',extra={'victimSteamId':f.player()['steamId']})
        self.add('other',extra={'killerSteamId':'76561190000000003'})
        self.assertEqual(self.adjusted()['kills'],15)

    def test_counts_clamp_at_zero(self):
        self.add('wire');self.assertEqual(self.adjusted(0)['kills'],0)

    def test_same_match_id_next_round_resets(self):
        self.add('old');self.add('new-round','match_state',{'state':'MatchState.PreMatch'})
        self.assertEqual(self.adjusted()['kills'],15)
        self.add('new');self.assertEqual(self.adjusted()['kills'],14)

    def test_different_match_or_map_not_subtracted(self):
        self.add('other-match',extra={'matchId':'other'});self.add('other-map',extra={'mapName':'other'})
        self.assertEqual(self.adjusted()['kills'],15)

    def test_no_round_boundary_fails_closed(self):
        with closing(sqlite3.connect(self.cache)) as c,c:c.execute("DELETE FROM events WHERE type='match_state'")
        self.assertIsNone(self.adjusted()['kills'])
        self.assertFalse(f.app.kick_eligible(self.adjusted(),self.config))
        self.assertFalse(f.app.kill_cap_eligible(self.adjusted()))

    def test_ended_round_fails_closed(self):
        self.add('end','match_ended');self.assertIsNone(self.adjusted()['kills'])

    def test_stale_error_and_catchup_fail_closed(self):
        for sql in ["UPDATE sync_state SET last_success=984.99","UPDATE sync_state SET error='failure'","UPDATE sync_cursor SET latest_seq=2"]:
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.cache)) as c,c:
                    c.execute("UPDATE sync_state SET last_success=1000,error=NULL");c.execute('UPDATE sync_cursor SET latest_seq=1');c.execute(sql)
                self.assertIsNone(self.adjusted()['kills'])

    def test_other_server_fails_closed(self):
        self.add('other',server='other-server');self.assertIsNone(self.adjusted()['kills'])

    def test_thresholds_with_wire_and_normal_kills(self):
        self.add('wire')
        self.assertFalse(f.app.kill_cap_eligible(self.adjusted(15,5)))
        self.assertTrue(f.app.kill_cap_eligible(self.adjusted(16,5)))
        self.assertFalse(f.app.kick_eligible(self.adjusted(7,1),self.config))
        self.assertTrue(f.app.kick_eligible(self.adjusted(8,1),self.config))
        self.assertFalse(f.app.high_kd(self.adjusted(5,1),4))
        self.assertTrue(f.app.high_kd(self.adjusted(6,1),4))

    def test_poll_uses_adjustment_without_changing_public_players(self):
        self.engine.config.update(exclude_barbed_wire_kills=True,high_kill_kick=True,high_kd_kick=True)
        for i in range(9):self.add(str(i))
        self.rcon.players=[f.player(15,1)]
        self.engine.poll(now=1000)
        self.assertEqual(self.engine.players[0]['kills'],15)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind IN ('kd_kick','kill_cap_kick')").fetchone()[0],0)

    def test_queued_cap_is_rechecked_before_dispatch(self):
        self.engine.config.update(exclude_barbed_wire_kills=True,high_kill_kick=True)
        self.rcon.players=[f.player(15,5)]
        job='fake-cap-job'
        with self.engine.db:self.engine.add_job('不符合本服要求',1000,recipient=f.player()['steamId'],kind='kill_cap_kick')
        job=self.engine.db.execute("select id from jobs where kind='kill_cap_kick'").fetchone()[0]
        self.add('wire')
        result=self.engine.moderation.process_auto(job,f.player()['steamId'],'不符合本服要求',f.app.kill_cap_eligible,lambda p:['unused'],now=1000)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(self.rcon.kicked,[])

    def test_adjusted_normal_kd_and_cap_still_execute(self):
        for kind,eligible in [('kd_kick',lambda p:f.app.kick_eligible(p,self.config)),('kill_cap_kick',f.app.kill_cap_eligible)]:
            with self.subTest(kind=kind):
                self.engine.config['exclude_barbed_wire_kills']=True
                sid='76561190000000011' if kind=='kd_kick' else '76561190000000012'
                self.rcon.players=[f.player(16,1,identity=sid)]
                self.add(kind,extra={'killerSteamId':sid})
                with self.engine.db:job=self.engine.add_job('test',1000,recipient=sid,kind=kind)
                result=self.engine.moderation.process_auto(job,sid,'test',eligible,lambda p:['测试','[EN] Test','[JA] テスト'],now=1000)
                self.assertEqual(result['kickStatus'],'confirmed')
        self.assertEqual(len(self.rcon.kicked),2)

class BarbedBurstTests(unittest.TestCase):
    setUp = bf.BurstTests.setUp
    tearDown = bf.BurstTests.tearDown
    add = bf.BurstTests.add
    detect = bf.BurstTests.detect
    count = bf.BurstTests.count
    three = bf.BurstTests.three
    def test_all_wire_never_creates_action(self):
        self.three(cause=BARBED_WIRE);self.assertEqual(self.count(),0)

    def test_two_normal_one_wire_no_action_then_three_normal(self):
        self.add('one',10,cause='Id.Item.SV98');self.add('wire',11,cause=BARBED_WIRE);self.add('two',12,cause='Id.Item.SV98');self.detect()
        self.assertEqual(self.count(),0)
        self.add('three',13,cause='Id.Item.SV98');self.detect();self.assertEqual(self.count(),1)

    def test_queued_wire_evidence_cancelled_no_restriction(self):
        self.three()
        row=self.e.db.execute('select id,evidence from burst_actions').fetchone()
        evidence=json.loads(row[1]);evidence[0]['cause']=BARBED_WIRE
        with self.e.db:self.e.db.execute('update burst_actions set evidence=? where id=?',(json.dumps(evidence),row[0]))
        self.b.process_one(now=1002)
        self.assertEqual(self.e.db.execute('select status,error from burst_actions').fetchone(),('cancelled','barbed_wire_excluded'))
        self.assertEqual(self.r.bans,[])
        self.assertEqual(self.e.kick_store.db.execute('select count(*) from permanent_ban_actions').fetchone()[0],0)
