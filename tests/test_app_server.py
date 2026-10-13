"""Persistent-session coordination and recovery, without model/network calls."""
from collections import deque
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from persona_runtime.app_server import AppServerBackend, CONTEXT_MARKER, cumulative_delta, scope_key, split_prompt
from persona_runtime.codex import CodexCancelled, CodexError
from persona_runtime.engine import Engine
from persona_runtime.store import Store
from test_memory_life import decision


def prompt(event='one', history=None, person='operator', kind='message', web=None):
    return 'RUNTIME CONTRACT\nconstant contract\nPERSONA\ncomplete unchanged persona'+CONTEXT_MARKER+json.dumps({
        'event':{'id':event,'kind':kind,'person':person,'conversation':'local','register':'auto','text':'hello'},
        'history':history or [],'state':'current','requested_recall':None,'requested_web_search':web})


class FakeClient:
    def __init__(self):
        self.calls, self.events = [], deque()
        self.total = 0
        self.turn = 0
        self.history = []
        self.invalid = None
        self.closed = False
        self.answers = deque()
        self.web_receipt = False
    def rpc(self, method, params, timeout=30):
        self.calls.append((method,copy.deepcopy(params)))
        if method=='thread/start':return {'thread':{'id':'thread-'+str(len(self.calls))}}
        if method=='thread/unsubscribe':
            self.events.append({'method':'thread/closed','params':{'threadId':params['threadId']}})
        if method=='thread/read':return {'thread':{'turns':self.history}}
        if method=='turn/start':
            self.turn+=1;tid='turn-'+str(self.turn);thread=params['threadId']
            common={'threadId':thread,'turnId':tid}
            def event(method, **values):return {'method':method,'params':{**common,**values}}
            self.total+=100
            usage=event('thread/tokenUsage/updated',tokenUsage={'total':{'inputTokens':self.total,'outputTokens':self.turn*2,'cachedInputTokens':max(0,self.total-100)}})
            self.events.extend([usage,copy.deepcopy(usage)])
            if self.invalid:self.events.append(event('item/started',item={'id':'bad','type':self.invalid}))
            self.events.append(event('item/agentMessage/delta',delta='ignored, never logged'))
            value=decision(reply='hello')
            if 'facts' in params['outputSchema']['properties']:
                from persona_runtime.research import task_id
                data=json.loads(params['input'][0]['text'].split('\nHOST UPDATE (JSON data)\n')[1])
                value={'task_id':data['research_task'],'facts':[{'url':'https://example.org/fact','title':'Source','claim':'Synthetic fact'}],'caveats':[]}
                if self.web_receipt:self.events.append(event('item/completed',item={'id':'search','type':'webSearch','action':{'type':'search','query':data['question']}}))
            elif self.answers:value=self.answers.popleft()
            item={'type':'agentMessage','id':'answer','text':json.dumps(value)}
            self.events.append(event('item/completed',item=item))
            completed={'id':tid,'status':'completed','items':[item]}
            self.history.append(completed)
            self.events.append(event('turn/completed',turn=completed))
            return {'turn':{'id':tid}}
        return {}
    def receive(self, deadline):
        if not self.events:raise CodexError('missing synthetic event')
        return self.events.popleft()
    def close(self):self.closed=True


class Backend(AppServerBackend):
    def __init__(self, directory, client=None):
        super().__init__(directory)
        self.fake=client or FakeClient()
        self.starts=0
    def _start(self):
        if self.client:return 0
        self.starts+=1;self.client=self.fake
        self.workspace=self.directory/'workspace';self.workspace.mkdir(parents=True,exist_ok=True)
        self.overrides={}
        return 25
    def close(self):
        if self.client:self.client.close()
        self.client=None;self.loaded.clear()


class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.backend=Backend(self.directory)
    def tearDown(self):self.backend.close();self.temp.cleanup()
    def test_reuses_process_and_thread_and_appends_changed_delivery_only(self):
        receipt={'id':'old','text':'prior input','reply':'delivered'}
        a,u=self.backend.decide(prompt(history=[receipt]),attempt=1)
        self.backend.acknowledge(1)
        b,v=self.backend.decide(prompt('two',[receipt]),attempt=2)
        self.assertEqual(self.backend.starts,1)
        calls=self.backend.fake.calls
        self.assertEqual(sum(m=='thread/start' for m,_ in calls),1)
        self.assertEqual(sum(m=='thread/resume' for m,_ in calls),0)
        sent=[p for m,p in calls if m=='turn/start']
        self.assertNotIn('complete unchanged persona',sent[1]['input'][0]['text'])
        self.assertEqual(json.loads(sent[1]['input'][0]['text'].split('\nHOST UPDATE (JSON data)\n')[1])['history'],[])
        self.assertEqual(u,{'input_tokens':100,'output_tokens':2,'cached_input_tokens':0})
        self.assertEqual(v,{'input_tokens':100,'output_tokens':2,'cached_input_tokens':100})
        self.assertNotIn('cache_write_input_tokens',v)
        self.assertEqual(self.backend.path.stat().st_mode & 0o777,0o600)
    def test_resume_after_restart_uses_same_thread_and_baseline(self):
        _,u=self.backend.decide(prompt(),attempt=1);self.backend.acknowledge(1)
        original=next(iter(self.backend.state['scopes'].values()))['thread']
        fake=FakeClient();fake.total=100;fake.turn=1
        self.backend.close();self.backend=Backend(self.directory,fake)
        _,v=self.backend.decide(prompt('two'),attempt=2)
        self.assertEqual(fake.calls[0][0],'thread/resume')
        self.assertEqual(fake.calls[0][1]['threadId'],original)
        self.assertEqual(v['input_tokens'],100)
    def test_completed_receipt_replay_spends_no_model_call_or_duplicate_usage(self):
        first=self.backend.decide(prompt(),attempt=1)
        self.backend.close();fake=FakeClient();self.backend=Backend(self.directory,fake)
        second=self.backend.decide(prompt(),attempt=2)
        self.assertEqual(second,(first[0],{}))
        self.assertEqual(fake.calls,[])
        self.backend.acknowledge(2);self.assertIsNone(self.backend.state['pending'])
    def test_unknown_acceptance_and_intermediate_stage_never_blind_retry(self):
        self.backend.decide(prompt(),attempt=1)
        pending=self.backend.state['pending'];pending.update(status='submitting',turn=None)
        before=len(self.backend.fake.calls)
        with self.assertRaisesRegex(CodexError,'uncertain'):self.backend.decide(prompt(),attempt=2)
        self.assertEqual(len(self.backend.fake.calls),before)
        pending.update(status='completed',key=['one',None,'search something'])
        with self.assertRaisesRegex(CodexError,'multi-stage'):self.backend.decide(prompt(),attempt=3)
        self.assertEqual(len(self.backend.fake.calls),before)
    def test_reconciles_accepted_completion_before_retry(self):
        result=self.backend.decide(prompt(),attempt=1)
        self.backend.state['pending']['status']='accepted'
        self.backend.state['pending'].pop('decision')
        replay=self.backend.decide(prompt(),attempt=2)
        self.assertEqual(replay,(result[0],{}))
        self.assertEqual(self.backend.fake.calls[-1][0],'thread/read')
    def test_native_web_requires_receipt_and_other_tools_rejected(self):
        self.backend.fake.invalid='commandExecution'
        with self.assertRaisesRegex(CodexError,'Unexpected tool'):self.backend.decide(prompt(),attempt=1)
        self.assertTrue(self.backend.fake.closed)
        self.assertIsNone(self.backend.client)
    def test_web_start_without_action_is_valid_but_not_a_completed_receipt(self):
        from persona_runtime.telemetry import safe_action
        self.assertEqual(safe_action(None),{})
        self.assertEqual(safe_action('untrusted malformed value'),{})
    def test_research_rejects_credential_urls_and_oversized_handoffs(self):
        from persona_runtime.research import decode
        value={'task_id':'task','facts':[{'url':'https://user:secret@example.org','title':'t','claim':'c'}],'caveats':[]}
        with self.assertRaises(ValueError):decode(json.dumps(value))
        value['facts']=[];value['caveats']=['x'*801]
        with self.assertRaises(ValueError):decode(json.dumps(value))

    def test_ordinary_thread_cannot_enable_native_search(self):
        with self.assertRaisesRegex(CodexError,'separate research'):
            self.backend.decide(prompt(),attempt=1,web_search=True)
        self.assertEqual(self.backend.starts,0)
    def test_research_is_separate_and_search_without_receipt_fails(self):
        self.backend.decide(prompt(),attempt=1);self.backend.acknowledge(1)
        with self.assertRaisesRegex(CodexError,'search did not complete'):
            self.backend.research(json.loads(prompt().split(CONTEXT_MARKER)[1])['event'],'query',attempt=2)
        methods=[m for m,_ in self.backend.fake.calls]
        self.assertEqual(methods.count('thread/start'),2)
        self.assertNotIn('thread/unsubscribe',methods)
        self.assertNotIn('thread/resume',methods)
    def test_no_cross_audience_wake_or_register_thread_reuse(self):
        for n,kwargs in enumerate(({}, {'person':'someone'}, {'person':'self','kind':'wake'}),1):
            self.backend.decide(prompt(str(n),**kwargs),attempt=n);self.backend.acknowledge(n)
        self.assertEqual(len(self.backend.state['scopes']),3)
    def test_cancel_before_send_does_not_start_server(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(CodexCancelled):self.backend.decide(prompt(),attempt=1,cancel=cancel)
        self.assertEqual(self.backend.starts,0)
    def test_counter_reset_and_missing_fields_are_unknown_not_negative_or_zero(self):
        self.assertEqual(cumulative_delta({'inputTokens':10},{'inputTokens':20}),{})
        self.assertEqual(cumulative_delta({'outputTokens':4},{'outputTokens':1}),{'output_tokens':3})
    def test_store_recovery_blocks_automatic_inference(self):
        store=Store(self.directory/'db')
        try:
            store.enqueue('test');event=store.claim();store.start_attempt(event)
            self.backend.recover_store(store)
            self.assertIsNone(store.claim())
            self.assertEqual(store.db.execute('SELECT status FROM events').fetchone()[0],'failed')
        finally:store.close()
    def test_engine_keeps_full_persona_in_constant_and_acknowledges(self):
        store=Store(self.directory/'db');store.memory.dense=False
        try:
            store.enqueue('Synthetic input')
            engine=Engine(store,self.backend)
            engine.step(max_calls=1)
            start=next(p for m,p in self.backend.fake.calls if m=='thread/start')
            self.assertIn('3427',start['baseInstructions'])
            self.assertIsNone(self.backend.state['pending'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM turns').fetchone()[0],1)
        finally:store.close()

    def research_engine(self):
        store=Store(self.directory/'db');store.memory.dense=False
        self.addCleanup(store.close)
        store.enqueue('Private dialogue must never leak to the researcher')
        self.backend.fake.answers.extend([decision(reply=None,web_query='specific bounded question'),decision(reply='fact answer')])
        self.backend.fake.web_receipt=True
        return store,Engine(store,self.backend)

    def test_research_three_turn_flow_keeps_main_thread_and_minimal_context(self):
        store,engine=self.research_engine()
        engine.step(max_calls=3)
        calls=self.backend.fake.calls
        starts=[p for m,p in calls if m=='thread/start']
        turns=[p for m,p in calls if m=='turn/start']
        self.assertEqual(len(starts),2)
        self.assertIn('3427',starts[0]['baseInstructions'])
        self.assertNotIn('3427',starts[1]['baseInstructions'])
        self.assertEqual(turns[0]['threadId'],turns[2]['threadId'])
        self.assertNotEqual(turns[0]['threadId'],turns[1]['threadId'])
        self.assertNotIn('Private dialogue',turns[1]['input'][0]['text'])
        self.assertIn('specific bounded question',turns[1]['input'][0]['text'])
        self.assertIn('research_result',turns[2]['input'][0]['text'])
        self.assertNotIn('thread/unsubscribe',[m for m,_ in calls])
        self.assertEqual(store.db.execute('SELECT count(*) FROM turns').fetchone()[0],1)
        self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.assertEqual(engine.last_calls,3)

    def test_research_needs_budget_for_return_to_character(self):
        store,engine=self.research_engine()
        with self.assertRaisesRegex(ValueError,'two remaining'):engine.step(max_calls=2)
        self.assertEqual(len([1 for m,_ in self.backend.fake.calls if m=='turn/start']),1)
        self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_research_result_stale_after_new_input_cannot_commit(self):
        store,engine=self.research_engine()
        turn=engine.begin(3);engine.dispatch(turn);engine.accept(turn,engine.generate(turn))
        engine.dispatch(turn);result=engine.generate(turn)
        store.enqueue('New intent cancels the search')
        with self.assertRaises(CodexCancelled):engine.accept(turn,result)
        self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_crash_after_research_completion_reuses_receipt_once(self):
        store,engine=self.research_engine()
        turn=engine.begin(3);engine.dispatch(turn);engine.accept(turn,engine.generate(turn))
        engine.dispatch(turn);engine.generate(turn)  # completed, not accepted by SQLite
        self.backend.close();self.backend.recover_store(store)
        store.retry(turn.event['id'])
        before=len([1 for m,_ in self.backend.fake.calls if m=='turn/start'])
        engine.step(3)
        after=len([1 for m,_ in self.backend.fake.calls if m=='turn/start'])
        self.assertEqual(after-before,1)  # only character continuation
        self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        usage=json.loads(store.db.execute('SELECT usage FROM turns').fetchone()[0])
        self.assertGreaterEqual(usage['input_tokens'],300)
        self.assertIsNone(self.backend.state['pending'])

    def test_crash_between_sqlite_stage_accept_and_private_ack(self):
        store,engine=self.research_engine()
        turn=engine.begin(3);engine.dispatch(turn)
        result=engine.generate(turn)
        from unittest.mock import patch
        with patch.object(self.backend,'acknowledge',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):engine.accept(turn,result)
        self.backend.recover_store(store)
        self.assertIsNone(self.backend.state['pending'])
        store.retry(turn.event['id'])
        engine.step(3)
        self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_bridge_lives_with_app_server_and_scrubs_child_environment(self):
        import socket,time
        from urllib.parse import urlsplit
        from unittest.mock import patch
        from persona_runtime import proxy
        from persona_runtime.app_server import DISABLED
        from test_proxy import server,Vault,config,SECRET
        from fake_app_server import make_cli
        clients=[]
        class Wire(FakeClient):
            def __init__(self,command,environment,directory):
                super().__init__();clients.append(self)
                assert command[0]=='/usr/bin/sandbox-exec'
                assert environment['NO_PROXY']=='' and SECRET not in str(environment)
                address=urlsplit(environment['HTTPS_PROXY'])
                self.sock=socket.create_connection((address.hostname,address.port),timeout=2)
                self.sock.sendall(b'CONNECT destination.invalid:443 HTTP/1.1\r\n\r\n')
                assert b'200' in self.sock.recv(4096)
            def send(self,value):pass
            def rpc(self,method,params,timeout=30):
                if method=='config/read':return {'config':{'features':{key:False for key in DISABLED}}}
                return super().rpc(method,params,timeout)
            def close(self):self.sock.close();super().close()
        path=self.directory/'proxy.json'
        with server() as socks:
            value=config(socks.server_address[1]);proxy.save(value,path);Vault().put(value['account'],SECRET)
            with patch('persona_runtime.app_server.transport',side_effect=lambda env,**kw:proxy.transport(env,path,timeout=.1,keychain_factory=Vault)),patch('persona_runtime.app_server.StdioClient',Wire):
                backend=AppServerBackend(self.directory/'wire',binary=make_cli(self.directory,decision()))
                try:
                    backend.decide(prompt(),attempt=1);backend.acknowledge(1)
                    bridge=backend._proxy_bridge
                    time.sleep(.25)
                    backend.decide(prompt('two'),attempt=2)
                    clients[0].sock.sendall(b'alive')
                    self.assertEqual(clients[0].sock.recv(5),b'alive')
                    self.assertEqual(len(clients),1)
                    self.assertEqual(bridge.requests,1)
                finally:backend.close()
                self.assertFalse(bridge.active)
                self.assertTrue(clients[0].closed)


if __name__=='__main__':unittest.main()
