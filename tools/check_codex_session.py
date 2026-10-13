#!/usr/bin/env python3
"""Installed-CLI integration check; synthetic loopback Responses only, no inference.

Uses a temporary private database, no proxy config/Keychain, no raw requests,
authorization headers or model transcripts in output. Requires macOS Seatbelt.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from persona_runtime import app_server
from persona_runtime.codex import CodexError
from persona_runtime.engine import Engine
from persona_runtime.store import Store

ANSWER = {'reply':'Synthetic answer','state':'Synthetic state','goals':'Synthetic goals',
    'wake_after_minutes':60,'memories':[],'relations':[],'journal':[],'memory_query':None,
    'web_query':None,'web_sources':[],'messages':[],'activity':None,'speech_preference':None,'outreach':None}


def declarations(payload):
    names = []
    def visit(tool, namespace=''):
        name = tool.get('name', tool.get('type', ''))
        if tool.get('type') == 'namespace':
            for nested in tool.get('tools', []): visit(nested, name)
        else:
            names.append((namespace+'.' if namespace else '')+name)
            names.extend(re.findall(r'### `([^`]+)`', tool.get('description','')))
    for tool in payload.get('tools', []): visit(tool)
    for item in payload.get('input', []):
        if item.get('type') == 'additional_tools':
            for tool in item.get('tools', []): visit(tool)
    return sorted(set(names))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self): self.send_error(404)
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.receipts.append({'model':payload.get('model'), 'tools':declarations(payload),
            'input_items':len(payload.get('input', [])), 'effort':payload.get('reasoning',{}).get('effort')})
        answer = {'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':json.dumps(ANSWER),'annotations':[]}]}
        response = {'id':'response_'+str(len(self.server.receipts)), 'object':'response','status':'completed',
            'output':[answer],'usage':{'input_tokens':100,'output_tokens':2,'total_tokens':102,
                'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
        events = [{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
            {'type':'response.output_item.added','output_index':0,'item':{**answer,'content':[]}},
            {'type':'response.output_text.delta','item_id':answer['id'],'output_index':0,'content_index':0,'delta':json.dumps(ANSWER)},
            {'type':'response.output_item.done','output_index':0,'item':answer},
            {'type':'response.completed','response':response}]
        data = ''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def run():
    real_client = app_server.StdioClient
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.receipts = []
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    @contextmanager
    def isolated_transport(environment, **kwargs):
        yield {k:v for k,v in environment.items() if not k.lower().endswith('_proxy')}, None
    def local_client(command, environment, directory):
        command = [x.replace('model_provider="openai"','model_provider="garik_mock"') for x in command]
        extra = {'model_providers.garik_mock.name':'Synthetic local test',
            'model_providers.garik_mock.base_url':'http://127.0.0.1:%d/v1'%server.server_port,
            'model_providers.garik_mock.requires_openai_auth':False,
            'model_providers.garik_mock.supports_standalone_web_search':True,
            'model_providers.garik_mock.wire_api':'responses'}
        for k,v in extra.items():command += ['-c',k+'='+json.dumps(v)]
        host='/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host'
        profile = ('(version 1)(allow default)(deny network-outbound)'
            '(allow network-outbound (remote ip "localhost:%d") (remote unix-socket))'
            '(deny process-exec)(allow process-exec (literal %s) (literal %s))') % (
                server.server_port,json.dumps(command[0]),json.dumps(host))
        return real_client(['/usr/bin/sandbox-exec','-p',profile,*command],environment,directory)
    backend = store = None
    try:
        with tempfile.TemporaryDirectory(prefix='garik-session-check-') as scratch, \
                patch.object(app_server,'transport',isolated_transport), \
                patch.object(app_server,'StdioClient',local_client):
            store = Store(Path(scratch)/'data');store.memory.dense=False
            backend = app_server.AppServerBackend(store.directory,timeout=25)
            engine = Engine(store,backend)
            for message in ('Synthetic first message','Synthetic second message'):
                store.enqueue(message);engine.step(max_calls=1)
            thread_id=next(iter(backend.state['scopes'].values()))['thread']
            backend.close()
            backend = app_server.AppServerBackend(store.directory,timeout=25)
            engine = Engine(store,backend)
            store.enqueue('Synthetic after restart');engine.step(max_calls=1)
            assert next(iter(backend.state['scopes'].values()))['thread'] == thread_id
            event=store.enqueue('Synthetic search stage');event=store.claim()
            # Inspect the actual enabled tool catalog. A fabricated final answer
            # without a search receipt MUST be rejected by the adapter.
            try: backend.research(event,'synthetic query',attempt=store.start_attempt(event))
            except CodexError as error:
                assert str(error)=='Requested native search did not complete', str(error)
            else: raise AssertionError('Unperformed search accepted')
            assert len(server.receipts)==4
            for record in server.receipts:
                assert record['model']=='gpt-6.1-sol'
                assert record['effort']=='medium'
                assert not any(name.startswith(('collaboration.','mcp__codex_apps')) or name in ('exec_command','write_stdin') for name in record['tools'])
            assert all('web__run' not in record['tools'] for record in server.receipts[:3])
            assert 'web__run' in server.receipts[3]['tools']
            assert server.receipts[1]['input_items'] > server.receipts[0]['input_items']
            print(json.dumps({'real_model_turns':0,'synthetic_requests':len(server.receipts),
                'same_thread_after_restart':True,'search_gate':True,
                'unperformed_search_rejected':True,'requests':server.receipts},indent=2))
            backend.close();backend=None
            store.close();store=None
    finally:
        if backend:backend.close()
        if store:store.close()
        server.shutdown();server.server_close();thread.join(1)


if __name__=='__main__':run()
