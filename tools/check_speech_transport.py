#!/usr/bin/env python3
"""Inspect actual installed-CLI wire prompts using loopback synthetic responses.

No real model calls, credentials, headers or proxy configuration are recorded.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from persona_runtime import app_server
from persona_runtime.app_server import save_private
from tools.check_codex_session import ANSWER, declarations
from tools.evaluate_speech import run


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):self.send_error(404)
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.receipts.append(payload)
        structured=payload.get('text',{}).get('format',{}).get('type')=='json_schema'
        text=json.dumps(ANSWER) if structured else 'Синтетический ответ для проверки транспорта.'
        answer={'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':text,'annotations':[]}]}
        response={'id':'response_'+str(len(self.server.receipts)),'object':'response','status':'completed',
            'output':[answer],'usage':{'input_tokens':100,'output_tokens':2,'total_tokens':102,
                'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
            {'type':'response.output_item.added','output_index':0,'item':{**answer,'content':[]}},
            {'type':'response.output_text.delta','item_id':answer['id'],'output_index':0,'content_index':0,'delta':text},
            {'type':'response.output_item.done','output_index':0,'item':answer},
            {'type':'response.completed','response':response}]
        data=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def audit(directory):
    directory=Path(directory).resolve()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.receipts=[]
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    real_client=app_server.StdioClient
    @contextmanager
    def isolated_transport(environment,**kwargs):
        yield {k:v for k,v in environment.items() if not k.lower().endswith('_proxy')},None
    def local_client(command,environment,workspace):
        command=[x.replace('model_provider="openai"','model_provider="garik_mock"') for x in command]
        extra={'model_providers.garik_mock.name':'Synthetic speech prompt audit',
            'model_providers.garik_mock.base_url':'http://127.0.0.1:%d/v1'%server.server_port,
            'model_providers.garik_mock.requires_openai_auth':False,
            'model_providers.garik_mock.wire_api':'responses',
            'model_providers.garik_mock.request_max_retries':0,
            'model_providers.garik_mock.stream_max_retries':0}
        for key,value in extra.items():command+=['-c',key+'='+json.dumps(value)]
        host='/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host'
        profile=('(version 1)(allow default)(deny network-outbound)'
            '(allow network-outbound (remote ip "localhost:%d") (remote unix-socket))'
            '(deny process-exec)(allow process-exec (literal %s) (literal %s))')%(
                server.server_port,json.dumps(command[0]),json.dumps(host))
        return real_client(['/usr/bin/sandbox-exec','-p',profile,*command],environment,workspace)
    try:
        with patch.object(app_server,'StdioClient',local_client):
            run(directory,8,30,isolated_transport,scene_limit=1)
        save_private(directory/'synthetic-wire.json',server.receipts)
        persona=(directory/'persona-unchanged.md').read_text()
        summary=[]
        for payload in server.receipts:
            structured=payload.get('text',{}).get('format',{}).get('type')=='json_schema'
            instructions=payload.get('instructions','') or '\n'.join(
                c.get('text','') for item in payload.get('input',[]) if item.get('role')=='developer'
                for c in item.get('content',[]) if isinstance(c,dict))
            assert persona.strip() in instructions
            assert ('RUNTIME CONTRACT' in instructions)==structured
            assert ('<skills_instructions>' in instructions)==structured
            tools=declarations(payload)
            assert not any(n.startswith(('collaboration.','mcp__codex_apps')) or n in ('exec_command','write_stdin','web__run') for n in tools)
            assert payload.get('reasoning',{}).get('effort')=='medium'
            summary.append({'structured':structured,'instructions_chars':len(instructions),
                'roles':[i.get('role',i.get('type')) for i in payload.get('input',[])],
                'tools':tools,'model':payload.get('model')})
        assert len(summary)==8
        save_private(directory/'audit.json',{'real_model_calls':0,'synthetic_requests':8,'requests':summary})
        print(json.dumps({'real_model_calls':0,'synthetic_requests':8,'requests':summary},ensure_ascii=False,indent=2))
    finally:
        server.shutdown();server.server_close();thread.join(1)


if __name__=='__main__':audit(sys.argv[1])
