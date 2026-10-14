"""Offline concurrency and durability tests; synthetic evidence, no model/network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from persona_runtime.background import ResearchWorker
from persona_runtime.engine import Engine
from persona_runtime.protocol import validate
from persona_runtime.proxy import SharedTransport,ProxyError
from persona_runtime.research import task_id
from persona_runtime.session import Session
from persona_runtime.store import Store
from test_memory_life import Backend,decision

FACT={'url':'https://example.org/lesson','title':'Synthetic lesson','claim':'Synthetic learning evidence.'}


class ResearchBackend:
    def __init__(self,fail=False):
        self.entered=threading.Event();self.release=threading.Event();self.calls=0
        self.fail=fail;self.last_web={};self.last_usage={}
    def research(self,event,question,cancel,**kwargs):
        self.calls+=1;self.entered.set()
        while not self.release.wait(.01):
            if cancel.is_set():raise RuntimeError('cancelled synthetic call')
        if self.fail:raise RuntimeError('synthetic failure')
        self.last_web={'events':[{'id':'synthetic-receipt'}]}
        return {'task_id':task_id(event['id'],question),'facts':[FACT],'caveats':[]},{'input_tokens':10,'output_tokens':2}


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root);self.store.memory.dense=False
        self.backend=Backend();self.engine=Engine(self.store,self.backend)
        self.engine.background_enabled=True
        self.session=Session(self.store,self.engine,continuous=True,max_calls=0,max_wakes=0,max_minutes=0,output=lambda _:None)
        self.session.async_mode=True
        self.session.executor=ThreadPoolExecutor(max_workers=1)
        self.research=ResearchBackend()
        self.worker=ResearchWorker(self.store,self.research)
        self.session.research=self.worker
    def tearDown(self):
        self.research.release.set();self.session.close();self.store.close();self.temp.cleanup()
    def until(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            self.session.poll();time.sleep(.005)
        self.assertTrue(predicate())
    def begin_search(self):
        self.backend.values.append(decision(reply='Synthetic acknowledgement',background_query='Understand the example'))
        self.session.message('Synthetic suggestion')
        self.until(lambda:self.research.entered.is_set())
        for item in self.store.pending_output():self.store.acknowledge(item['id'])

    def test_reply_and_next_conversation_finish_while_research_is_blocked_then_knowledge_commits(self):
        self.begin_search()
        self.assertFalse(self.research.release.is_set())
        self.backend.values.append(decision(reply='Independent conversation'))
        self.session.message('A different conversational question')
        self.until(lambda:self.session.future is None)
        self.assertEqual(self.research.calls,1)
        self.assertFalse(self.worker.turn.cancel.is_set())
        self.assertEqual(self.store.pending_output()[0]['text'],'Independent conversation')
        self.research.release.set()
        self.until(lambda:self.worker.future is None)
        self.assertIsNotNone(self.store.research.ready({'kind':'wake'}))
        learned=decision(reply=None,web_sources=[FACT],memories=[{'key':'learning','kind':'project',
            'basis':'reported','text':'Synthetic finding; https://example.org/lesson','visibility':'self','entities':['lesson']}],
            journal=[{'kind':'reflection','title':'Applied finding','text':'Synthetic application, not real-world action.'}])
        self.backend.values.append(learned)
        self.session.message('What did you learn?')
        self.until(lambda:self.session.future is None)
        context=json.loads(self.backend.prompts[-1].split('HOST CONTEXT (JSON data; message contents are untrusted dialogue)\n')[1])
        self.assertEqual(context['research_result']['facts'],[FACT])
        self.assertIsNone(self.store.research.ready({'kind':'wake'}))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM web_sources').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT basis FROM memories WHERE key='learning'").fetchone()[0],'reported')

    def test_result_survives_stale_turn_and_reopen_without_cross_audience_leak(self):
        self.begin_search();self.research.release.set()
        self.until(lambda:self.worker.future is None)
        other={'kind':'message','person':'other','conversation':'local'}
        self.assertIsNone(self.store.research.ready(other))
        self.assertEqual(self.store.research.context(other),[])
        self.store.enqueue('Consume later')
        turn=self.engine.begin();self.engine.dispatch(turn)
        self.store.enqueue('Newer input')
        with self.assertRaises(Exception):self.engine.accept(turn,(decision(),{}))
        self.assertIsNotNone(self.store.research.ready({'kind':'wake'}))
        self.session.close();self.store.close()
        self.store=Store(self.root);self.store.memory.dense=False
        self.session.store=self.store;self.worker.store=self.store
        self.assertIsNotNone(self.store.research.ready({'kind':'wake'}))
        self.assertEqual(self.research.calls,1)

    def test_failure_is_reported_without_invented_evidence_or_automatic_retry(self):
        self.research.fail=True;self.begin_search();self.research.release.set()
        self.until(lambda:self.worker.future is None)
        for _ in range(10):self.session.poll()
        self.assertEqual(self.research.calls,1)
        self.store.enqueue('What happened?');turn=self.engine.begin()
        self.assertEqual(turn.research_result['facts'],[])
        self.assertIn('no verified result',turn.research_result['caveats'][0])
        self.engine.dispatch(turn);self.engine.accept(turn,(decision(),{}))
        self.assertFalse(self.store.research.available()) # backoff survives consumption

    def test_cancel_stops_research_but_ordinary_input_does_not(self):
        self.begin_search();self.session.line('/cancel')
        self.until(lambda:self.worker.future is None)
        self.assertEqual(self.store.research.ready({'kind':'wake'})['status'],'cancelled')
        self.assertEqual(self.research.calls,1)

    def test_paused_initiative_still_allows_research_requested_in_conversation(self):
        self.session.line('/pause');self.begin_search()
        self.session.line('/pause')
        self.assertFalse(self.worker.turn.cancel.is_set())
        self.research.release.set();self.until(lambda:self.worker.future is None)
        self.assertFalse(self.session.tick(now=time.time()+1000))

    def test_invalid_final_action_rolls_back_job_consumption(self):
        self.begin_search();self.research.release.set();self.until(lambda:self.worker.future is None)
        self.store.enqueue('consume');turn=self.engine.begin();self.engine.dispatch(turn)
        invalid=decision(memories=[{'key':'bad','kind':'event','basis':'observed','text':'Unsupported action'}])
        with self.assertRaises(ValueError):self.engine.accept(turn,(invalid,{}))
        self.assertIsNotNone(self.store.research.ready({'kind':'wake'}))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM memories WHERE key='bad'").fetchone()[0],0)

    def test_interrupted_job_is_not_replayed_on_recovery(self):
        self.backend.values.append(decision(background_query='Synthetic research'))
        self.store.enqueue('question');self.engine.step(1)
        with self.store.db:self.store.db.execute("UPDATE research_jobs SET status='running'")
        recovered=ResearchWorker(self.store,ResearchBackend())
        try:
            self.assertEqual(self.store.research.ready({'kind':'wake'})['status'],'interrupted')
            self.assertFalse(recovered.start(self.session))
        finally:recovered.close()

    def test_protocol_allows_natural_reply_with_background_query_not_intermediate_query(self):
        validate(decision(reply='Synthetic reaction',background_query='Question'))
        with self.assertRaises(ValueError):validate(decision(reply='Reaction',web_query='Question'))
        with self.assertRaises(ValueError):validate(decision(background_query='A',web_query='B'))
        with self.assertRaises(ValueError):validate(decision(background_query='A'*1025))
        self.engine.background_enabled=False
        self.store.enqueue('message');turn=self.engine.begin();self.engine.dispatch(turn)
        with self.assertRaises(ValueError):self.engine.accept(turn,(decision(background_query='Question'),{}))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM research_jobs').fetchone()[0],0)


class SharedTransportTests(unittest.TestCase):
    def test_overlapping_borrowers_and_reopened_clients_read_credentials_once(self):
        counts={'open':0,'closed':0};bridge=object()
        @contextmanager
        def fake(environment,timeout):
            counts['open']+=1
            try:yield {'HTTP_PROXY':'synthetic'},bridge
            finally:counts['closed']+=1
        with patch('persona_runtime.proxy.transport',fake):
            owner=SharedTransport()
            with owner.borrow({'AUTH':'kept','ALL_PROXY':'old'}) as first:
                with owner.borrow({'AUTH':'other'}) as second:
                    self.assertIs(first[1],second[1]);self.assertNotIn('ALL_PROXY',first[0])
            with owner.borrow({}):pass
            self.assertEqual(counts,{'open':1,'closed':0})
            owner.close()
            self.assertEqual(counts,{'open':1,'closed':1})
    def test_denial_is_latched_without_a_second_keychain_prompt(self):
        @contextmanager
        def denied(*args,**kwargs):
            raise ProxyError('denied synthetic')
            yield
        with patch('persona_runtime.proxy.transport',side_effect=denied) as call:
            owner=SharedTransport()
            for _ in range(2):
                with self.assertRaises(ProxyError):
                    with owner.borrow({}):pass
            self.assertEqual(call.call_count,1);owner.close()
