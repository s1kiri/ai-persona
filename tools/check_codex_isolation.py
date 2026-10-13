#!/usr/bin/env python3
"""Negative installed-Codex probes: loopback synthetic responses, zero inference.

No credentials are inspected. Each fixture serves at most three responses; the
native-budget probe must stop after two. Even a broken adapter cannot run an
unbounded successful synthetic tool loop. macOS Seatbelt is required.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from persona_runtime import app_server
from persona_runtime.codex import CodexError
from tools.check_codex_session import ANSWER, declarations


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):self.send_error(404)
    def do_POST(self):
        server=self.server
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        server.receipts.append({'tools':declarations(payload)})
        for item in payload.get('input',[]):
            if item.get('type','').endswith('call_output'):
                output=str(item.get('output','')).lower()
                server.categories.update(word for word in ('read-only','rejected','failed','not a function','undefined','not found','unknown') if word in output)
        count=len(server.receipts)
        if count>3:self.send_error(400);return
        if server.scenario=='stall':
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            server.release.wait(6)
            return
        attack=count==1 or server.scenario=='loop'
        if attack:
            if server.scenario=='shell':
                code='text(await tools.exec_command({cmd:'+json.dumps('/usr/bin/touch '+str(server.canary))+'}));'
            else:
                code='text(await tools.apply_patch('+json.dumps('*** Begin Patch\n*** Add File: '+str(server.canary)+'\n+synthetic\n*** End Patch')+'));'
            item={'type':'custom_tool_call','id':'tool'+str(count),'call_id':'call'+str(count),
                  'name':'exec','namespace':'functions','status':'completed','input':code}
            events=[{'type':'response.output_item.added','output_index':0,'item':{**item,'input':''}},
                {'type':'response.custom_tool_call_input.delta','output_index':0,'item_id':item['id'],'delta':code}]
        else:
            item={'type':'message','id':'answer','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':json.dumps(ANSWER),'annotations':[]}]}
            events=[{'type':'response.output_item.added','output_index':0,'item':{**item,'content':[]}},
                {'type':'response.output_text.delta','output_index':0,'item_id':'answer','content_index':0,'delta':json.dumps(ANSWER)}]
        response={'id':'r'+str(count),'object':'response','status':'completed','output':[item],
            'usage':{'input_tokens':100,'output_tokens':2,'total_tokens':102,
                'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
            *events,{'type':'response.output_item.done','output_index':0,'item':item},
            {'type':'response.completed','response':response}]
        data=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def check(scenario):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    server.scenario=scenario;server.receipts=[];server.categories=set();server.release=threading.Event()
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    real=app_server.StdioClient
    clients=[]
    @contextmanager
    def transport(environment,**kwargs):
        yield {k:v for k,v in environment.items() if not k.lower().endswith('_proxy')},None
    def client(command,environment,directory):
        command=[x.replace('model_provider="openai"','model_provider="garik_mock"') for x in command]
        flags={'model_providers.garik_mock.name':'Synthetic test',
            'model_providers.garik_mock.base_url':'http://127.0.0.1:%d/v1'%server.server_port,
            'model_providers.garik_mock.requires_openai_auth':False,
            'model_providers.garik_mock.supports_standalone_web_search':True,
            'model_providers.garik_mock.wire_api':'responses',
            'model_providers.garik_mock.request_max_retries':0,
            'model_providers.garik_mock.stream_max_retries':0,
            # This inherited server must be disabled before any thread starts.
            'mcp_servers.synthetic_forbidden.command':'/usr/bin/touch',
            'mcp_servers.synthetic_forbidden.args':[str(server.mcp_canary)]}
        if scenario=='loop':flags.update({'features.rollout_budget.limit_tokens':4,
            'features.rollout_budget.prefill_token_weight':0.0})
        for key,value in flags.items():command+=['-c',key+'='+json.dumps(value)]
        host='/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host'
        profile=('(version 1)(allow default)(deny network-outbound)'
            '(allow network-outbound (remote ip "localhost:%d") (remote unix-socket))'
            '(deny process-exec)(allow process-exec (literal %s) (literal %s))') % (
                server.server_port,json.dumps(command[0]),json.dumps(host))
        value=real(['/usr/bin/sandbox-exec','-p',profile,*command],environment,directory)
        clients.append(value)
        return value
    try:
        with tempfile.TemporaryDirectory(prefix='garik-isolation-') as scratch, \
                patch.object(app_server,'StdioClient',client),patch.object(app_server,'transport',transport):
            server.canary=Path(scratch)/'forbidden';server.mcp_canary=Path(scratch)/'mcp-started'
            backend=app_server.AppServerBackend(Path(scratch)/'data',timeout=1 if scenario=='stall' else 8)
            prompt='Synthetic isolation check'+app_server.CONTEXT_MARKER+json.dumps({
                'event':{'id':scenario,'kind':'message','person':'test','conversation':'test','register':'auto'},'history':[]})
            started=time.monotonic();failed=False;failure=None
            try:backend.decide(prompt,attempt=1)
            except CodexError as error:failed=True;failure=str(error)
            finally:backend.close();server.release.set()
            elapsed=time.monotonic()-started
            assert not server.canary.exists(), 'Read-only boundary failed'
            assert not server.mcp_canary.exists(), 'Inherited MCP server started'
            assert clients and all(c.process.poll() is not None and not c.reader.is_alive() for c in clients)
            assert all(not any(name.startswith(('collaboration.','mcp__','synthetic_forbidden')) or name in ('exec_command','write_stdin','web__run') for name in row['tools']) for row in server.receipts)
            if scenario=='loop':
                assert failed and len(server.receipts)==2, 'Native budget did not stop the internal loop'
            elif scenario=='stall':
                assert failed and len(server.receipts)==1 and elapsed<8, 'Wall deadline/cleanup failed'
            else:
                assert len(server.receipts)==2, (scenario,len(server.receipts),failure)
                if scenario=='write':assert 'read-only' in server.categories and 'rejected' in server.categories
                else:assert server.categories & {'not a function','undefined','not found','unknown'}
            return {'scenario':scenario,'synthetic_requests':len(server.receipts),
                'no_file_written':True,'mcp_not_started':True,'process_reaped':True,
                'elapsed_seconds':round(elapsed,2),'categories':sorted(server.categories)}
    finally:
        server.release.set();server.shutdown();server.server_close();worker.join(1)


if __name__=='__main__':
    print(json.dumps({'real_model_turns':0,'checks':[check(name) for name in ('write','shell','loop','stall')]},indent=2))
