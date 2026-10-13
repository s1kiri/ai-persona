"""Isolated daemon/HTTP/usage checks. Synthetic backend; no credentials or model."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError

from persona_runtime.codex import CodexCancelled
from persona_runtime.daemon import Runtime,Limits,control
from persona_runtime.engine import Engine
from persona_runtime.store import Store
from persona_runtime.telemetry import Ledger
from test_memory_life import decision,Backend

class ObservableBackend:
    supports_control=True
    model='synthetic-test'
    def __init__(self):self.calls=0
    def decide(self,prompt,cancel=None,observer=None,**kwargs):
        self.calls+=1
        data=json.loads(prompt.split('HOST CONTEXT (JSON data; message contents are untrusted dialogue)\n',1)[1])
        text=data['event']['text']
        if text=='slow':
            if observer:observer({'type':'usage','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80,'cache_write_input_tokens':0}})
            cancel.wait(8)
            if cancel.is_set():raise CodexCancelled('synthetic cancellation')
        if text=='error':
            if observer:observer({'type':'usage','usage':{'input_tokens':20,'output_tokens':5}})
            raise RuntimeError('private-secret-must-not-leak')
        value=decision(reply='Synthetic current response.')
        if text=='multipart':value=decision(messages=[{'text':'one','delay_seconds':0},{'text':'two','delay_seconds':1}])
        return value,{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80,'cache_write_input_tokens':0}

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name);self.store.memory.dense=False
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_usage_spool_recovery_survives_failed_attempt_and_replay_without_double_cache(self):
        ledger=Ledger(self.store);engine=Engine(self.store,Backend())
        self.store.enqueue('Synthetic');turn=engine.begin();engine.dispatch(turn)
        observer=ledger.open_call(turn,'session','synthetic',engine.last_prompt_report)
        observer({'type':'usage','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80,'cache_write_input_tokens':0}})
        observer({'type':'reasoning','text':'private-secret'})
        self.store.fail(turn.event['id'],'Synthetic failure')
        self.store.finish_attempt(turn.attempt,'failed',{})
        self.store.close();self.store=Store(self.temp.name);self.store.memory.dense=False
        ledger=Ledger(self.store);ledger.sync()
        total=ledger.totals('session')
        self.assertEqual(total['total_tokens'],110)
        self.assertEqual(total['cached_input_tokens'],80)
        self.assertEqual(total['cache_write_input_tokens'],0)
        self.assertIsNone(ledger.calls()[0]['usage']['reasoning_output_tokens'])
        with self.store.db:self.store.put('observations_offset',0)
        ledger.ingest();self.assertEqual(ledger.totals('session')['total_tokens'],110)
        self.assertNotIn('private-secret',ledger.spool.read_text())
    def test_completed_historical_turn_aggregate_not_double_counted_with_attempts(self):
        engine=Engine(self.store,Backend(decision(),decision()))
        self.store.enqueue('One');engine.step()
        ledger=Ledger(self.store)
        self.assertEqual(ledger.totals()['input_tokens'],10)
        self.assertEqual(len(ledger.calls()),1)
        self.assertTrue(ledger.calls()[0]['legacy'])
    def test_context_report_contains_sizes_and_provenance_not_private_content(self):
        with self.store.db:self.store.put('state','private-secret internal state')
        engine=Engine(self.store,Backend());self.store.enqueue('Synthetic');turn=engine.begin()
        self.assertNotIn('private-secret',json.dumps(engine.last_prompt_report))
        self.assertIn('canon',engine.last_prompt_report['parts'])
        self.assertTrue(engine.last_prompt_report['estimated'])

    def test_partial_known_usage_and_torn_spool_survive_restart(self):
        ledger=Ledger(self.store);engine=Engine(self.store,Backend())
        self.store.enqueue('Synthetic');turn=engine.begin();engine.dispatch(turn)
        observer=ledger.open_call(turn,'session','synthetic',engine.last_prompt_report)
        observer({'type':'usage','usage':{'input_tokens':125}})
        with ledger.spool.open('ab') as output:output.write(b'{"torn":')
        self.store.close();self.store=Store(self.temp.name);self.store.memory.dense=False
        ledger=Ledger(self.store)
        self.assertEqual(ledger.totals('session')['total_tokens'],125)
        self.assertEqual(ledger.totals('session')['unknown_calls'],1)
        self.assertTrue(ledger.spool.read_bytes().endswith(b'\n'))
        observer({'type':'usage','usage':{'output_tokens':7}})
        ledger.ingest();self.assertEqual(ledger.totals('session')['total_tokens'],132)

    def test_malformed_descriptor_never_controls_other_hosts(self):
        for url in ('http://127.0.0.1:bad','http://127.0.0.1:80/path','http://127.0.0.1:80?target=evil','http://user:secret@127.0.0.1:80','https://evil.example'):
            (Path(self.temp.name)/'daemon.json').write_text(json.dumps({'url':url,'control':'synthetic','instance':'synthetic'}))
            self.assertIsNone(control(self.temp.name))

    def test_later_cli_turn_and_dispatch_without_ledger_are_imported_on_restart(self):
        Ledger(self.store)
        engine=Engine(self.store,Backend(decision()));self.store.enqueue('Later CLI');engine.step()
        ledger=Ledger(self.store)
        self.assertEqual(ledger.totals()['input_tokens'],10)
        self.store.enqueue('Interrupted dispatch');turn=engine.begin();engine.dispatch(turn)
        self.store.finish_attempt(turn.attempt,'interrupted',{})
        ledger=Ledger(self.store);ledger.sync()
        self.assertEqual(ledger.totals()['attempts'],2)
        self.assertEqual(ledger.totals()['unknown_calls'],1)
        self.assertEqual(ledger.calls()[0]['status'],'interrupted')

class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.backend=ObservableBackend()
        self.runtime=Runtime(self.directory,port=0,limits=Limits(autonomy=False,max_calls=8),backend_factory=lambda:self.backend)
        self.errors=[]
        def run():
            try:self.runtime.run()
            except Exception as error:self.errors.append(error)
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()
        self.addCleanup(self.runtime.stopping.set)
        self.assertTrue(self.runtime.ready.wait(15),'startup did not complete');self.assertIsNone(self.runtime.failure)
        self.opener=build_opener(ProxyHandler({}))
    def tearDown(self):
        self.runtime.stopping.set();self.thread.join(10)
        self.assertFalse(self.thread.is_alive())
        self.temp.cleanup()
    def wait(self,condition):
        until=time.monotonic()+6
        while time.monotonic()<until:
            state=control(self.directory)
            if condition(state):return state
            time.sleep(.05)
        self.fail('condition not reached')
    def message(self,text,key=None):
        return control(self.directory,'/api/message',{'id':key or str(uuid.uuid4()),'text':text})
    def test_ui_disconnected_mailbox_delivery_continues_with_pause_and_no_extra_call(self):
        self.message('multipart')
        result=self.wait(lambda s:len([m for m in s['messages'] if m['role']=='garik'])==2)
        outgoing=[m for m in result['messages'] if m['role']=='garik']
        self.assertGreaterEqual(outgoing[1]['created']-outgoing[0]['created'],1)
        self.assertEqual(self.backend.calls,1)
        self.assertTrue(all(m['status']=='delivered' for m in outgoing))
    def test_refresh_and_duplicate_message_id_do_not_duplicate_calls(self):
        key=str(uuid.uuid4());self.message('hello',key)
        self.wait(lambda s:any(m['role']=='garik' for m in s['messages']))
        for _ in range(4):control(self.directory)
        self.message('hello',key);time.sleep(.2)
        self.assertEqual(self.backend.calls,1)
        self.assertEqual(len([m for m in control(self.directory)['messages'] if m['role']=='user']),1)
    def test_user_input_cancels_stale_reply_and_known_usage_is_retained(self):
        self.message('slow')
        self.wait(lambda s:s['usage']['session']['known_calls']==1)
        self.message('new')
        result=self.wait(lambda s:len(s['calls'])==2 and s['mode']=='idle')
        self.assertEqual(result['usage']['session']['total_tokens'],220)
        self.assertEqual(len([m for m in result['messages'] if m['role']=='garik']),1)
        self.assertEqual(result['calls'][1]['status'],'cancelled')
    def test_error_masking_and_pause_resume_stop(self):
        self.message('error')
        result=self.wait(lambda s:s['calls'] and s['calls'][0]['status']=='failed')
        self.assertNotIn('private-secret',json.dumps(result))
        self.assertEqual(result['usage']['session']['total_tokens'],25)
        control(self.directory,'/api/action',{'action':'pause'})
        self.assertEqual(control(self.directory)['autonomy'],'на паузе')
        control(self.directory,'/api/action',{'action':'resume'})
        self.assertNotEqual(control(self.directory)['autonomy'],'на паузе')
        control(self.directory,'/api/action',{'action':'stop'})
        self.thread.join(5);self.assertFalse(self.thread.is_alive())
    def test_second_owner_rejected_before_model_or_database_changes(self):
        other=Runtime(self.directory,port=0,limits=Limits(autonomy=False),backend_factory=lambda:self.backend)
        with self.assertRaises(RuntimeError):other.run()
        self.assertEqual(self.backend.calls,0)

    def test_daily_attempt_budget_preserves_new_message_without_dispatch(self):
        self.runtime.limits.max_daily_calls=1
        self.message('hello');self.wait(lambda s:s['mode']=='budget')
        self.message('later');time.sleep(.2)
        state=control(self.directory)
        self.assertEqual(self.backend.calls,1)
        self.assertEqual(state['usage']['day']['attempts'],1)
        self.assertTrue(any(m['text']=='later' and m['status']=='pending' for m in state['messages']))

    def test_stop_during_generation_retains_receipt_and_starts_no_followup(self):
        self.message('slow');self.wait(lambda s:s['usage']['session']['known_calls']==1)
        control(self.directory,'/api/action',{'action':'stop'})
        self.thread.join(5);self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.backend.calls,1)
        store=Store(self.directory)
        try:
            ledger=Ledger(store);ledger.sync()
            self.assertEqual(ledger.totals()['total_tokens'],110)
            self.assertEqual(ledger.calls()[0]['status'],'cancelled')
        finally:store.close()
    def test_loopback_origin_auth_csrf_and_static_path_are_enforced(self):
        origin=self.runtime.origin
        def get(path,headers=None):
            return self.opener.open(Request(origin+path,headers=headers or {}),timeout=3)
        with self.assertRaises(HTTPError) as error:get('/api/state')
        self.assertEqual(error.exception.code,401)
        with self.assertRaises(HTTPError) as error:get('/',{'Origin':'https://evil.example'})
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:get('/',{'Host':'evil.example'})
        self.assertEqual(error.exception.code,403)
        response=get('/');cookie=response.headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly',response.headers['Set-Cookie'])
        req=Request(origin+'/api/action',data=b'{"action":"stop"}',headers={'Cookie':cookie,'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:self.opener.open(req)
        self.assertEqual(error.exception.code,403)
        req.add_header('X-Panel-CSRF',self.runtime.csrf);req.add_header('Origin','https://evil.example')
        with self.assertRaises(HTTPError) as error:self.opener.open(req)
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError):get('/../daemon.json',{'Cookie':cookie})
        self.assertEqual(self.runtime.port, self.runtime.port)

if __name__=='__main__':unittest.main()
