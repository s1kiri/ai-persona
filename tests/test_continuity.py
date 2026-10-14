"""Offline continuity/transport regressions. No claims about model behaviour."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.app_server import context_update,remember_context,CONTEXT_MARKER
from persona_runtime.daemon import Limits,budget_available
from persona_runtime.engine import Engine
from persona_runtime.session import Session
from persona_runtime.store import Store
from persona_runtime.telemetry import usage_metrics,Ledger
from test_memory_life import Backend,decision
from test_app_server import Backend as PersistentBackend
from tools.reset_garik import reset


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.store=Store(self.directory/'db');self.store.memory.dense=False
    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_partial_uncached_is_derived_per_receipt_never_from_unmatched_totals(self):
        self.assertEqual(usage_metrics({'input_tokens':26463,'cached_input_tokens':23808,'output_tokens':229})['uncached_input_tokens'],2655)
        self.assertIsNone(usage_metrics({'input_tokens':100})['uncached_input_tokens'])
        self.assertIsNone(usage_metrics({'input_tokens':10,'cached_input_tokens':11})['uncached_input_tokens'])
        ledger=Ledger(self.store);engine=Engine(self.store,Backend())
        for n,usage in enumerate(({'input_tokens':100,'output_tokens':2,'cached_input_tokens':80},{'input_tokens':200,'output_tokens':3})):
            self.store.enqueue(str(n));turn=engine.begin();engine.dispatch(turn)
            observe=ledger.open_call(turn,'s','synthetic',{})
            observe({'type':'usage','usage':usage});ledger.ingest()
            self.store.fail(turn.event['id'],'synthetic')
        total=ledger.totals()
        self.assertEqual(total['uncached_input_tokens'],20)
        self.assertEqual(total['coverage']['uncached_input_tokens'],1)
        self.assertEqual(total['processed_tokens'],305)

    def test_snapshot_deltas_accept_proposal_only_with_host_commit_receipt(self):
        context={'event':{'id':'one'},'state':'state','goals':'goals','activities':[],
            'history':[{'id':'old','text':'text','reply':None}],'memories':[]}
        entry={};remember_context(entry,context,decision(state='new state',goals='new goals'))
        new={**context,'event':{'id':'two'},'state':'new state','goals':'new goals','applied_event':'one'}
        sent=context_update(entry,new)
        self.assertNotIn('state',sent);self.assertNotIn('goals',sent)
        self.assertEqual(sent['applied_proposal']['fields'],['state','goals'])
        self.assertEqual(sent['history'],[])
        without_receipt=context_update(entry,{**new,'applied_event':'other'})
        self.assertEqual(without_receipt['state'],'new state')
        delivered=context_update(entry,{**new,'history':[{'id':'old','text':'text','reply':'delivered'}]})
        self.assertEqual(delivered['history'],[{'id':'old','reply':'delivered'}])

    def test_multiturn_persistent_thread_sends_no_repeated_full_snapshots(self):
        backend=PersistentBackend(self.directory/'adapter');engine=Engine(self.store,backend,web_enabled=False)
        try:
            for text in ('Что у тебя под окнами?','А реальная точка на карте есть?','Оставь карту. Как твоё занятие?'):
                self.store.enqueue(text,register='auto',surface='private')
                engine.step(1)
                for message in self.store.pending_output():self.store.acknowledge(message['id'])
            calls=backend.fake.calls
            self.assertEqual(sum(m=='thread/start' for m,_ in calls),1)
            sent=[json.loads(p['input'][0]['text'].split('\nHOST UPDATE (JSON data)\n')[1]) for m,p in calls if m=='turn/start']
            self.assertIn('state',sent[0]);self.assertNotIn('state',sent[1]);self.assertNotIn('goals',sent[2])
            self.assertEqual(len(sent[2]['history']),1)
            constant=next(p['baseInstructions'] for m,p in calls if m=='thread/start')
            self.assertIn('FICTION AND REALITY',constant);self.assertIn('3427',constant)
        finally:backend.close()

    def test_time_previous_contact_offline_gap_and_utc_plus_three(self):
        self.store.enqueue('prior',now=1000)
        current=self.store.enqueue('new',now=173800)
        event=dict(self.store.db.execute('SELECT * FROM events WHERE id=?',(current,)).fetchone())
        with self.store.db:self.store.put('offline_gap_seconds',172000)
        clock=Engine(self.store,Backend()).time_context(event,now=173800)
        self.assertEqual(clock['seconds_since_previous_user_message'],172800)
        self.assertTrue(clock['now_local'].endswith('+03:00'))
        self.assertEqual(clock['offline_gap_seconds'],172000)

    def test_rolling_budget_waits_then_recovers_without_lifetime_cap(self):
        ledger=Ledger(self.store)
        now=200000
        for n in range(12):
            self.store.db.execute('INSERT INTO call_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(n),None,'synthetic','s','wake','synthetic',now-10,None,'failed','{}','{}',0,1))
        self.store.db.commit()
        with patch('persona_runtime.telemetry.time.time',return_value=now):
            self.assertFalse(budget_available(ledger,Limits(max_hourly_calls=12),now))
        with patch('persona_runtime.telemetry.time.time',return_value=now+3601):
            self.assertTrue(budget_available(ledger,Limits(max_hourly_calls=12),now+3601))
        with patch('persona_runtime.telemetry.time.time',return_value=now):
            self.assertFalse(budget_available(ledger,Limits(max_daily_calls=10),now+3601))

    def test_disabled_budgets_do_not_block_or_erase_usage(self):
        limits=Limits().validate()
        class LargeLedger:
            def totals(self,**kwargs):return {'total_tokens':99999999,'attempts':9999}
        ledger=LargeLedger()
        self.assertTrue(budget_available(ledger,limits))
        self.assertFalse(budget_available(ledger,Limits(max_daily_calls=10)))
        self.assertFalse(budget_available(ledger,Limits(max_hourly_known_tokens=1000)))
        self.assertEqual(ledger.totals()['attempts'],9999)
        for value in (-1,True,999):
            with self.assertRaises(ValueError):Limits(max_daily_known_tokens=value).validate()

    def test_continuous_session_passes_twenty_turns_and_hundreds_of_idle_ticks_call_no_model(self):
        backend=Backend(*[decision(reply=None)]*25)
        engine=Engine(self.store,backend)
        session=Session(self.store,engine,max_calls=0,max_wakes=0,max_minutes=0,continuous=True,output=lambda _:None)
        for n in range(25):session.message('Synthetic '+str(n))
        self.assertEqual(session.calls,25);self.assertFalse(session.stopped)
        for n in range(200):self.assertFalse(session.tick(now=0))
        self.assertEqual(session.calls,25)
        session.line('/pause');self.assertFalse(session.tick(now=10**10))
        session.line('/quit');self.assertTrue(session.stopped)
        self.assertEqual(Limits().max_calls,0)

    def test_offline_activity_artifacts_finish_switch_and_outreach_cooldown(self):
        engine=Engine(self.store,Backend())
        def work(key,status,title):
            event_id=self.store.enqueue('scheduled',kind='wake',person='self',surface='private')
            event=self.store.claim()
            value=decision(reply=None,activity={'key':key,'title':title,'kind':'fiction','status':status,
                'next_step':'finished' if status=='done' else 'write the next scene','reason':'curiosity','due_after_minutes':1},
                journal=[{'kind':'story','title':title,'text':'Synthetic artifact, not a real-world action.'}])
            self.store.complete(event,value,{})
        work('a','active','A');work('a','done','A completed');work('b','active','B')
        self.assertEqual(self.store.db.execute('SELECT status FROM activities WHERE key="a"').fetchone()[0],'done')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],3)
        self.assertEqual(self.store.pending_output(),[])

    def test_reset_preserves_verified_original_and_removes_only_active_memory_mapping(self):
        engine=Engine(self.store,Backend(decision()))
        self.store.enqueue('private message',now=1000)
        engine.step(1)
        previous_usage=Ledger(self.store).totals()['total_tokens']
        mapping=self.store.directory/'codex-session/sessions.json';mapping.parent.mkdir();mapping.write_text('{"scopes":{"own":"thread"}}')
        (self.store.directory/'sentinel').write_text('preserve')
        self.store.close()
        with patch('tools.reset_garik.control',return_value=None):result=reset(self.directory/'db',self.directory/'backups')
        backup=Path(result['backup']);manifest=json.loads((backup/'manifest.json').read_text())
        self.assertTrue(manifest['verified']);self.assertTrue(manifest['applied'])
        self.assertEqual((backup/'original/sentinel').read_text(),'preserve')
        self.assertEqual((backup/'original/codex-session/sessions.json').read_text(),mapping.read_text() if mapping.exists() else '{"scopes":{"own":"thread"}}')
        self.store=Store(self.directory/'db')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
        self.assertEqual(self.store.get('previous_user_message_at'),'1000.0')
        self.assertFalse(mapping.exists())
        self.assertEqual(Ledger(self.store).totals()['total_tokens'],previous_usage)
        with sqlite3.connect(backup/'snapshot/runtime.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

if __name__=='__main__':unittest.main()
