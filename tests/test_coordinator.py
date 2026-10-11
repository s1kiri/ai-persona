"""Deterministic coordination, cancellation, delivery and schema tests; no live calls."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest

from persona_runtime.codex import CodexCancelled
from persona_runtime.engine import Engine
from persona_runtime.protocol import validate
from persona_runtime.session import Session
from persona_runtime.store import Store,StaleTurn
from test_memory_life import Backend,decision,note


def activity(key='research',status='active',next_step='Check a primary source'):
    return {'key':key,'title':'Synthetic interest','kind':'research','status':status,
            'next_step':next_step,'reason':'Personal curiosity','due_after_minutes':5}


class Controlled:
    supports_control=True
    def __init__(self,first,second):
        self.values=[first,second]
        self.started=threading.Event()
        self.count=0
    def decide(self,prompt,cancel=None,**kwargs):
        value=copy.deepcopy(self.values[self.count]);self.count+=1
        if self.count==1:
            self.started.set()
            cancel.wait(2)
        return value,{'input_tokens':10}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        self.store=Store(self.directory);self.store.memory.dense=False
        self.session=None
    def tearDown(self):
        if self.session:self.session.close()
        self.store.close();self.temp.cleanup()
    def complete(self,value,kind='message',now=None,**kwargs):
        event_id=self.store.enqueue('Synthetic input',kind=kind,**kwargs)
        event=self.store.claim();self.store.complete(event,value,{},now=now)
        return event_id
    def async_session(self,backend):
        self.session=Session(self.store,Engine(self.store,backend),output=lambda _:None,max_calls=6)
        self.session.async_mode=True
        self.session.executor=ThreadPoolExecutor(max_workers=1)
        return self.session
    def wait_idle(self,session):
        end=time.monotonic()+3
        while session.future and time.monotonic()<end:
            session.poll();time.sleep(.005)
        self.assertIsNone(session.future)

    def test_new_input_is_nonblocking_and_discards_stale_state_memory_and_reply(self):
        backend=Controlled(decision(reply='obsolete',state='obsolete',memories=[note('old','obsolete')]),decision(reply='current',state='current'))
        session=self.async_session(backend)
        session.line('First');self.assertTrue(backend.started.wait(1))
        started=time.monotonic();session.line('Second')
        self.assertLess(time.monotonic()-started,.1)
        self.wait_idle(session)
        self.assertEqual(self.store.get('state'),'current')
        self.assertEqual([row['text'] for row in self.store.pending_output()],['current'])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM recollections WHERE key='old'").fetchone())
        self.assertEqual(session.calls,2)
        self.assertEqual([row['status'] for row in self.store.db.execute('SELECT status FROM events ORDER BY rowid')],['superseded','done'])

    def test_input_supersedes_inflight_wake_and_coordinator_never_commits_both(self):
        backend=Controlled(decision(reply='old wake',journal=[{'kind':'story','title':'obsolete','text':'obsolete'}]),decision(reply='user response'))
        session=self.async_session(backend)
        self.assertTrue(session.tick(now=1000));self.assertTrue(backend.started.wait(1))
        session.line('New user input');self.wait_idle(session)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],0)
        self.assertEqual([row['text'] for row in self.store.pending_output()],['user response'])
        self.assertEqual(session.wakes,1)

    def test_external_new_input_rejects_completion_before_any_writes(self):
        engine=Engine(self.store,Backend())
        self.store.enqueue('Old input');turn=engine.begin();engine.dispatch(turn)
        external=Store(self.directory)
        try:external.enqueue('New input')
        finally:external.close()
        with self.assertRaises(CodexCancelled):engine.accept(turn,(decision(reply='obsolete'),{}))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_delayed_parts_survive_restart_and_history_contains_only_delivered_words(self):
        event_id=self.complete(decision(messages=[{'text':'one','delay_seconds':0},{'text':'two','delay_seconds':2},{'text':'three','delay_seconds':3}]),now=1000)
        first=self.store.pending_output(now=1000)[0];self.store.acknowledge(first['id'],now=1000)
        self.assertEqual(self.store.pending_output(now=1001),[])
        self.store.close();self.store=Store(self.directory);self.store.memory.dense=False
        second=self.store.pending_output(now=1002)[0]
        self.assertEqual(second['text'],'two')
        context=self.store.context({'person':'operator','conversation':'local','kind':'message','text':'new'})
        self.assertEqual(context['history'][0]['reply'],'one')
        self.store.acknowledge(second['id'],now=1002)
        self.assertEqual(self.store.pending_output(now=1004),[])
        self.assertEqual(self.store.pending_output(now=1005)[0]['text'],'three')
        self.store.supersede_pending('operator','local')
        self.assertEqual(self.store.pending_output(include_future=True),[])
        context=self.store.context({'person':'operator','conversation':'local','kind':'message','text':'new'})
        self.assertEqual(context['history'][0]['reply'],'one\ntwo')

    def test_wake_delivery_is_remembered_without_exposing_private_user_input(self):
        event_id=self.complete(decision(reply='My own completed step'),kind='wake',person='self')
        output=self.store.pending_output()[0];self.store.acknowledge(output['id'])
        context=self.store.context({'person':'operator','conversation':'local','kind':'message','text':'hi'})
        self.assertTrue(any(row['id']==event_id and row['reply']=='My own completed step' for row in context['history']))
        episode=self.store.db.execute('SELECT * FROM episodes WHERE id=?',(event_id,)).fetchone()
        self.assertEqual(episode['owner'],'self')

    def test_due_wake_waits_for_undelivered_message_continuation(self):
        self.complete(decision(messages=[{'text':'one','delay_seconds':0},{'text':'two','delay_seconds':2}]),now=1000)
        engine=Engine(self.store,Backend())
        first=self.store.pending_output(now=1000)[0];self.store.acknowledge(first['id'],now=1000)
        self.assertIsNone(engine.wake_if_due(now=1300))
        self.store.acknowledge(self.store.pending_output(now=1300)[0]['id'],now=1300)
        self.assertIsNotNone(engine.wake_if_due(now=1300))

    def test_initial_human_input_does_not_create_an_immediate_wake_deadline(self):
        engine=Engine(self.store,Backend(decision()))
        self.store.enqueue('First human message')
        self.assertIsNone(engine.wake_if_due(now=1000))
        self.assertEqual(self.store.get('next_wake'),'')
        engine.step()
        self.assertGreater(float(self.store.get('next_wake')),time.time()+290)

    def test_cancelled_due_wake_can_continue_after_input_without_replaying_old_generation(self):
        engine=Engine(self.store,Backend(decision(),decision()))
        first=engine.wake_if_due(now=1000);turn=engine.begin()
        engine.cancel(turn,'new input')
        self.store.enqueue('Actual new input');engine.step()
        second=engine.wake_if_due(now=1100)
        self.assertIsNotNone(second);self.assertNotEqual(first,second)
        self.assertIsNone(engine.wake_if_due(now=1101))
        engine.step()
        self.assertEqual(self.store.db.execute('SELECT status FROM events WHERE id=?',(first,)).fetchone()[0],'superseded')
        self.assertEqual(self.store.db.execute('SELECT status FROM events WHERE id=?',(second,)).fetchone()[0],'done')

    def test_failed_due_wake_is_not_automatically_retried(self):
        engine=Engine(self.store,Backend())
        first=engine.wake_if_due(now=1000);event=self.store.claim();self.store.fail(event['id'],'synthetic error')
        self.assertIsNone(engine.wake_if_due(now=1100))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.store.retry(first);self.assertEqual(self.store.status()['events'],{'pending':1})

    def test_chat_does_not_postpone_activity_or_existing_wake_deadline(self):
        self.complete(decision(activity=activity(),wake_after_minutes=5),now=1000)
        due=self.store.get('next_wake');activity_due=self.store.activities()[0]['due']
        self.complete(decision(activity=activity(next_step='New next step'),wake_after_minutes=1000),now=1100)
        self.assertEqual(self.store.get('next_wake'),due)
        self.assertEqual(self.store.activities()[0]['due'],activity_due)
        self.complete(decision(activity=activity(),wake_after_minutes=1000),kind='wake',person='self',now=1300)
        self.assertEqual(float(self.store.get('next_wake')),1600)

    def test_one_current_activity_and_versioned_progress_not_three_open_plans(self):
        self.complete(decision(activity=activity('one')),now=1000)
        self.complete(decision(activity=activity('one',next_step='Source read; compare result')),now=1050)
        self.assertEqual(len(self.store.activities()),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_versions').fetchone()[0],2)
        self.complete(decision(activity=activity('two')),now=1100)
        self.assertEqual([row['key'] for row in self.store.activities()],['two'])
        self.assertEqual(self.store.db.execute("SELECT status FROM activities WHERE key='one'").fetchone()[0],'abandoned')

    def test_completed_activity_key_survives_restart_in_context_without_becoming_active(self):
        self.complete(decision(activity=activity('finished',status='done',next_step='Complete piece; optional later revision')))
        self.store.close();self.store=Store(self.directory);self.store.memory.dense=False
        context=self.store.context({'kind':'wake','person':'self','conversation':'local','text':'own check-in'})
        self.assertEqual([(row['key'],row['status']) for row in context['activities']],[('finished','done')])
        self.assertEqual(self.store.activities(),[])
        self.complete(decision(activity=activity('finished',status='done',next_step='Revision saved')),kind='wake',person='self')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activities').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM activity_versions').fetchone()[0],2)

    def test_speech_request_is_persistent_scoped_and_outreach_quiet_keeps_private_activity(self):
        self.complete(decision(speech_preference='clean',outreach=False),person='operator')
        self.store.close();self.store=Store(self.directory);self.store.memory.dense=False
        self.assertEqual(self.store.audience({'kind':'message','person':'operator','conversation':'local'})['speech_preference'],'clean')
        self.assertEqual(self.store.audience({'kind':'message','person':'other','conversation':'local'})['speech_preference'],'contextual')
        self.complete(decision(reply='must not be sent',journal=[{'kind':'reflection','title':'Private','text':'My own thought'}]),kind='wake',person='self')
        self.assertEqual(self.store.pending_output(),[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],1)

    def test_pause_persists_and_resume_keeps_original_session_deadline(self):
        session=Session(self.store,Engine(self.store,Backend()),output=lambda _:None)
        deadline=session.deadline;session.line('/pause')
        self.assertIsNone(session.engine.wake_if_due(now=2000))
        other=Session(self.store,session.engine,output=lambda _:None)
        self.assertTrue(other.paused)
        session.line('/resume');self.assertEqual(session.deadline,deadline)
        self.assertEqual(self.store.get('initiative_paused'),'0')

    def test_v2_database_migration_preserves_delivery_and_backs_up_once(self):
        self.complete(decision(reply='already delivered'))
        self.store.acknowledge(self.store.pending_output()[0]['id'])
        self.complete(decision(reply='still queued'))
        with self.store.db:
            self.store.db.execute('CREATE TABLE old_outbox (id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE REFERENCES events(id),conversation TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,delivered REAL)')
            self.store.db.execute('INSERT INTO old_outbox SELECT id,event_id,conversation,text,status,delivered FROM outbox')
            self.store.db.execute('DROP TABLE outbox')
            self.store.db.execute('ALTER TABLE old_outbox RENAME TO outbox')
            self.store.db.execute('ALTER TABLE events DROP COLUMN surface')
            for table in ('activities','activity_versions','audience_preferences','web_runs','web_sources','model_attempts'):
                self.store.db.execute('DROP TABLE '+table)
            self.store.db.execute("DELETE FROM metadata WHERE key='runtime_version'")
        self.store.close();self.store=Store(self.directory);self.store.memory.dense=False
        self.assertEqual(self.store.get('runtime_version'),'3')
        self.assertEqual([row['text'] for row in self.store.pending_output()],['still queued'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],2)
        self.assertEqual(len(list((self.directory/'backups').glob('before-life-v3-*.sqlite3'))),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox WHERE ordinal=0 AND delay_seconds=0').fetchone()[0],2)
        self.store.close();self.store=Store(self.directory);self.store.memory.dense=False
        self.assertEqual(len(list((self.directory/'backups').glob('before-life-v3-*.sqlite3'))),1)

    def test_recovery_marks_unaccepted_native_attempt_interrupted(self):
        self.store.enqueue('input');event=self.store.claim()
        attempt=self.store.start_attempt(event)
        self.store.recover()
        self.assertEqual(self.store.db.execute('SELECT status FROM model_attempts WHERE id=?',(attempt,)).fetchone()[0],'interrupted')
        self.assertEqual(self.store.status()['events'],{'pending':1})

    def test_failed_v3_migration_rolls_back_schema_before_retry(self):
        # Existing incompatible table causes a failure AFTER ALTER/outbox copy.
        # No half-migrated surface column or outbox must survive that failure.
        with self.store.db:
            self.store.db.execute('CREATE TABLE old_outbox (id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE REFERENCES events(id),conversation TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,delivered REAL)')
            self.store.db.execute('DROP TABLE outbox')
            self.store.db.execute('ALTER TABLE old_outbox RENAME TO outbox')
            self.store.db.execute('ALTER TABLE events DROP COLUMN surface')
            for table in ('activity_versions','audience_preferences','web_runs','web_sources','model_attempts'):
                self.store.db.execute('DROP TABLE '+table)
            self.store.db.execute("DELETE FROM metadata WHERE key='runtime_version'")
        with self.assertRaises(sqlite3.OperationalError):self.store.migrate_life()
        columns=[row['name'] for row in self.store.db.execute('PRAGMA table_info(events)')]
        self.assertNotIn('surface',columns)
        self.assertNotIn('ordinal',[row['name'] for row in self.store.db.execute('PRAGMA table_info(outbox)')])
        with self.store.db:self.store.db.execute('DROP TABLE activities')
        self.store.migrate_life()
        self.assertEqual(self.store.get('runtime_version'),'3')

    def test_model_cannot_fabricate_observed_receipt_or_web_source_without_search(self):
        for value in [decision(memories=[{**note('false','Bought something'),'basis':'observed'}]),
                      decision(web_sources=[{'url':'https://example.com','title':'Example','claim':'Claim'}])]:
            self.store.enqueue('Synthetic');event=self.store.claim()
            with self.assertRaises(ValueError):self.store.complete(event,value,{})
            self.store.supersede(event['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    def test_web_intermediate_writes_nothing_and_requires_completed_native_tool(self):
        backend=Backend(decision(web_query='synthetic question'),decision(reply='answer',web_sources=[{'url':'https://example.com','title':'Example','claim':'Synthetic claim'}]))
        backend.last_web={'events':[{'id':'synthetic','action':{'type':'search','query':'synthetic'},'status':'completed'}],'calls':1}
        engine=Engine(self.store,backend)
        self.store.enqueue('Find source');engine.step()
        self.assertEqual(engine.last_calls,2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM web_sources').fetchone()[0],1)
        self.assertIn('ENABLED NATIVE WEB TURN',backend.prompts[1])
        self.assertIn('"web_query_allowed": false',backend.prompts[1])

    def test_protocol_rejects_unsafe_message_delays_sources_and_intermediate_writes(self):
        for value in [decision(messages=[{'text':'bad','delay_seconds':31}]),
                      decision(reply='duplicate',messages=[{'text':'bad','delay_seconds':0}]),
                      decision(web_sources=[{'url':'https://user:password@example.com','title':'x','claim':'x'}]),
                      decision(web_query='x',activity=activity()),decision(outreach=1)]:
            with self.assertRaises(ValueError):validate(value)


if __name__=='__main__':unittest.main()
