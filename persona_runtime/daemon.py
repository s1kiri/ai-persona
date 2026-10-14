"""One database owner, bounded background life, protected loopback control."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass,asdict
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import URLError

from .codex import CodexBackend
from .app_server import AppServerBackend
from .engine import Engine,ROOT
from .memory import Budget
from .session import Session
from .store import Store,worker_lock
from .telemetry import Ledger,safe_action
from .background import ResearchWorker
from .proxy import SharedTransport

@dataclass
class Limits:
    max_calls:int=0
    max_wakes:int=0
    max_minutes:int=0
    max_known_tokens:int=500000
    max_daily_calls:int=0
    max_daily_known_tokens:int=0
    max_hourly_calls:int=0
    max_hourly_known_tokens:int=0
    min_wake_seconds:int=60
    autonomy:bool=True
    web:bool=True
    memory_budget:int=4000
    max_web_calls:int=2
    def validate(self):
        ranges={'max_calls':(0,100),'max_wakes':(0,100),'max_minutes':(0,1440),
                'max_known_tokens':(1000,10000000),'max_daily_calls':(0,500),
                'max_daily_known_tokens':(0,10000000),'max_hourly_calls':(0,100),
                'max_hourly_known_tokens':(0,10000000),'min_wake_seconds':(60,86400),'memory_budget':(1000,12000),'max_web_calls':(1,3)}
        for key,(low,high) in ranges.items():
            if type(getattr(self,key)) is not int or not low<=getattr(self,key)<=high:raise ValueError('invalid '+key)
        for key in ('max_daily_known_tokens','max_hourly_known_tokens'):
            if 0<getattr(self,key)<1000:raise ValueError('invalid '+key)
        return self

def private_json(path,value):
    path=Path(path);temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as output:
        json.dump(value,output);output.flush();os.fsync(output.fileno())
    temporary.replace(path)

def budget_available(ledger,limits,now=None):
    now=time.time() if now is None else now
    if not any((limits.max_hourly_calls,limits.max_hourly_known_tokens,limits.max_daily_calls,limits.max_daily_known_tokens)):
        return True
    hourly,daily=ledger.totals(since=now-3600),ledger.totals(day=True)
    return all(not limit or value<limit for value,limit in (
        (hourly['total_tokens'],limits.max_hourly_known_tokens),(hourly['attempts'],limits.max_hourly_calls),
        (daily['total_tokens'],limits.max_daily_known_tokens),(daily['attempts'],limits.max_daily_calls)))

class Runtime:
    def __init__(self,directory,port=8765,limits=None,backend_factory=None):
        self.directory=Path(directory).resolve();self.port=port
        self.limits=(limits or Limits()).validate()
        self.backend_factory=backend_factory or (lambda:AppServerBackend(self.directory,max_known_tokens=self.limits.max_known_tokens))
        self.commands=queue.Queue();self.condition=threading.Condition()
        self.payload='{}';self.revision=0;self.stopping=threading.Event()
        self.ready=threading.Event();self.failure=None;self.ended=None
        self.control=secrets.token_urlsafe(32);self.cookie=secrets.token_urlsafe(32);self.csrf=secrets.token_urlsafe(32)
        self.id=str(uuid.uuid4());self.started=time.time()
    @property
    def origin(self):return 'http://127.0.0.1:'+str(self.port)
    def request(self,kind,data):
        if self.stopping.is_set():raise RuntimeError('runtime is stopping')
        answer=queue.Queue(maxsize=1);self.commands.put((kind,data,answer))
        try:success,result=answer.get(timeout=8)
        except queue.Empty:raise RuntimeError('runtime is busy or stopping')
        if not success:raise ValueError(result)
        return result
    def state(self,since=None):
        with self.condition:
            if since==self.revision and not self.stopping.is_set():self.condition.wait(timeout=15)
            return self.payload
    def publish(self,store,session,ledger):
        delivered=[dict(row) for row in store.db.execute("""SELECT o.id,o.text,o.delivered,p.seen FROM outbox o JOIN events e ON e.id=o.event_id LEFT JOIN panel_messages p ON p.id=o.id WHERE o.status='delivered' AND ((e.person='operator' AND e.conversation='local') OR e.kind='wake') ORDER BY o.delivered DESC LIMIT 100""")]
        incoming=[{'id':row['id'],'role':'user','text':row['text'],'created':row['created'],'status':row['status']} for row in store.db.execute("SELECT id,text,created,status FROM events WHERE kind='message' AND person='operator' AND conversation='local' ORDER BY created DESC LIMIT 100")]
        messages=incoming+[{'id':row['id'],'role':'garik','text':row['text'],'created':row['delivered'],'status':'read' if row['seen'] else 'delivered'} for row in delivered]
        messages.sort(key=lambda item:(item['created'],item['id']))
        calls=ledger.calls()
        usage={'session':ledger.totals(self.id),'day':ledger.totals(day=True),'all':ledger.totals()}
        feed=[]
        for row in store.db.execute("SELECT id,call_id,kind,data,created FROM observations WHERE kind!='process.started' ORDER BY created DESC LIMIT 100"):
            feed.append({**dict(row),'data':json.loads(row['data'])})
        pending=store.pending_output('operator','local',include_future=True)
        queue_info=[]
        for item in pending:
            previous=store.db.execute('SELECT delivered FROM outbox WHERE event_id=? AND ordinal=?',(item['event_id'],item['ordinal']-1)).fetchone()
            queue_info.append({'id':item['id'],'ordinal':item['ordinal'],'due':previous['delivered']+item['delay_seconds'] if previous and previous['delivered'] else None})
        next_wake=float(store.get('next_wake')) if store.get('next_wake') else None
        last_wake=float(store.get('daemon_last_wake','0'))
        effective=max(next_wake or 0,last_wake+self.limits.min_wake_seconds) if next_wake is not None else None
        mode='stopped' if self.ended else ('stopping' if self.stopping.is_set() else ('budget' if session.stopped else ('generating' if session.future else ('paused' if session.paused else 'idle'))))
        current={'event':session.turn.event['id'],'category':'search' if session.turn.web_turn else session.turn.event['kind'],'started':calls[0]['started'] if calls else None} if session.turn else None
        activities=[{k:row[k] for k in ('key','title','kind','status','next_step','updated')} for row in store.activity_context()]
        memory=calls[0]['context'].get('selected_records',[]) if calls else []
        sources=[{**dict(row),**safe_action({'url':row['url']})} for row in store.db.execute('SELECT id,url,title,source_event,created FROM web_sources ORDER BY created DESC LIMIT 12')]
        for source in sources:source['url']=safe_action({'url':source['url']}).get('url')
        value={'instance':self.id,'database_id':store.get('panel_database_id'),'pid':os.getpid(),'started':self.started,'ended':self.ended,'mode':mode,
            'test_backend':not isinstance(self.backend,(CodexBackend,AppServerBackend)),
            'revision':0,'csrf':self.csrf,'model':self.backend.model,'limits':asdict(self.limits),
            'reasoning_effort':getattr(self.backend,'reasoning_effort',None),
            'calls_used':session.calls,'wakes_used':session.wakes,'autonomy':session.autonomy_status(),
            'autonomy_until':self.started+self.limits.max_minutes*60 if self.limits.max_minutes else None,'next_wake':next_wake,'effective_wake':effective,
            'current':current,'activities':activities,'messages':messages,'pending':queue_info,
            'research':store.research.context({'kind':'wake','person':'self','conversation':'local'}),
            'feed':feed,'calls':calls,'usage':usage,'memory':memory,'sources':sources,
            'channels':[{'id':'local','title':'Локальный чат','connected':True},
                        {'id':'telegram','title':'Telegram','connected':False}],
            'capabilities':[{'id':'web','title':'Нативный поиск Codex','enabled':self.limits.web},
                            {'id':'memory','title':'Локальная память и дневник','enabled':True},
                            {'id':'voice','title':'Голос','enabled':False}],
            'usage_note':'Точный usage приходит после turn.completed. Кэш — часть input. Это расход Гарика, не всего аккаунта; денег не рассчитываем.'}
        serialized=json.dumps(value,ensure_ascii=False)
        with self.condition:
            # Compare without revision; only real changes wake readers.
            old=json.loads(self.payload);old['revision']=0
            if old!=value:
                self.revision+=1;value['revision']=self.revision
                self.payload=json.dumps(value,ensure_ascii=False);self.condition.notify_all()
    def run(self):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        store=session=server=server_thread=None
        resources_closed=False;stop_answer=None;transport_owner=None
        try:
            with worker_lock(self.directory):
                store=Store(self.directory,locked=True)
                ledger=Ledger(store)
                self.backend=self.backend_factory()
                research_backend=None
                if type(self.backend) is AppServerBackend:
                    transport_owner=SharedTransport()
                    self.backend.transport_factory=transport_owner.borrow
                    research_backend=AppServerBackend(self.directory/'research-backend',self.backend.binary,
                        self.backend.model,self.backend.timeout,self.limits.max_known_tokens,transport_owner.borrow)
                if getattr(self.backend,'supports_sessions',False):self.backend.recover_store(store)
                else:store.recover()
                ledger.sync()
                engine=Engine(store,self.backend,web_enabled=self.limits.web,max_web_calls=self.limits.max_web_calls)
                store.memory.budget=Budget(self.limits.memory_budget)
                session=Session(store,engine,max_calls=self.limits.max_calls,max_wakes=self.limits.max_wakes,
                    max_minutes=self.limits.max_minutes,autonomy=self.limits.autonomy,continuous=True,
                    output=lambda text:ledger.emit('runtime.notice',{'text':'Ошибка хода; результат не применён. Нужен явный retry.' if text.startswith('Ошибка') else text}))
                session.async_mode=True;session.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='garik-model')
                session.deliver=lambda:self.deliver(store,ledger)
                def guard():
                    ledger.sync()
                    return not self.stopping.is_set() and budget_available(ledger,self.limits)
                session.dispatch_guard=guard
                def dispatch(turn):turn.observer=ledger.open_call(turn,self.id,self.backend.model,engine.last_prompt_report)
                session.on_dispatch=dispatch
                if research_backend and self.limits.web:
                    engine.background_enabled=True
                    session.research=ResearchWorker(store,research_backend,self.limits.max_web_calls)
                    session.on_research_dispatch=lambda turn:setattr(turn,'observer',ledger.open_call(
                        turn,self.id,research_backend.model,{'research':True,'estimated':True,
                        'note':'Background research: bounded question only; conversation continues independently.'}))
                with store.db:
                    heartbeat=float(store.get('runtime_heartbeat','0'))
                    store.put('offline_gap_seconds',max(0,self.started-heartbeat) if heartbeat else '')
                    store.put('runtime_heartbeat',self.started)
                    if not store.get('panel_database_id'):store.put('panel_database_id',str(uuid.uuid4()))
                    store.db.execute('INSERT INTO daemon_sessions VALUES (?,?,?,?,?)',(self.id,self.started,None,os.getpid(),json.dumps(asdict(self.limits))))
                ledger.emit('runtime.started',{'recovered':True})
                server=ThreadingHTTPServer(('127.0.0.1',self.port),handler(self))
                server.daemon_threads=True;self.port=server.server_address[1]
                server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
                private_json(self.directory/'daemon.json',{'version':1,'pid':os.getpid(),'instance':self.id,'url':self.origin,'control':self.control,'started':self.started})
                self.publish(store,session,ledger);self.ready.set()
                try:
                    while not self.stopping.is_set():
                        ledger.sync()
                        while True:
                            try:kind,data,answer=self.commands.get_nowait()
                            except queue.Empty:break
                            try:
                                if kind=='message':
                                    result={'id':session.message(data['text'],event_id=data['id'])}
                                elif kind=='seen':
                                    with store.db:
                                        for key in data['ids']:
                                            store.db.execute("""INSERT OR IGNORE INTO panel_messages (id,event_id,text,delivered,seen) SELECT o.id,o.event_id,o.text,o.delivered,NULL FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.id=? AND o.status='delivered' AND ((e.person='operator' AND e.conversation='local') OR e.kind='wake')""",(key,))
                                            store.db.execute('UPDATE panel_messages SET seen=? WHERE id=? AND seen IS NULL',(time.time(),key))
                                    result={'ok':True}
                                elif kind=='action':
                                    action=data['action']
                                    if action=='stop':
                                        self.stopping.set();session.exit_requested=session.stopped=True
                                        session.interrupt('runtime stopped')
                                    else:session.line({'pause':'/pause','resume':'/resume','cancel':'/cancel'}[action])
                                    ledger.emit('runtime.control',{'action':action});result={'ok':True}
                                else:raise ValueError('unsupported request')
                                self.publish(store,session,ledger)
                                if kind=='action' and data['action']=='stop':
                                    stop_answer=answer
                                    break
                                answer.put((True,result))
                            except Exception:answer.put((False,'Запрос отклонён. Проверь текст, ID и доступные команды.'))
                        if self.stopping.is_set():break
                        session.poll();self.deliver(store,ledger);ledger.sync()
                        permitted=guard()
                        if not permitted:
                            session.stopped=True;session.dispatch_blocked=True
                        elif session.dispatch_blocked:
                            session.dispatch_blocked=False
                            session.stopped=bool(session.max_calls and session.calls>=session.max_calls)
                        if not session.stopped:
                            session.start_pending()
                            last=float(store.get('daemon_last_wake','0'))
                            if time.time()>=last+self.limits.min_wake_seconds:
                                previous=session.wakes;session.tick()
                                if session.wakes>previous:
                                    with store.db:store.put('daemon_last_wake',time.time())
                        if time.time()-float(store.get('runtime_heartbeat','0'))>=30:
                            with store.db:store.put('runtime_heartbeat',time.time())
                        self.publish(store,session,ledger);time.sleep(.1)
                finally:
                    session.close();ledger.sync()
                    ledger.emit('runtime.stopped',{})
                    ended=time.time()
                    with store.db:
                        store.db.execute('UPDATE daemon_sessions SET ended=? WHERE id=?',(ended,self.id))
                        store.put('runtime_heartbeat',ended)
                    self.ended=ended
                    self.publish(store,session,ledger)
                    # A stop receipt confirms the worker is closed and state committed,
                    # not merely that the command reached the queue. HTTP drains next.
                    if stop_answer:stop_answer.put((True,{'ok':True,'state':json.loads(self.payload)}))
                    server.shutdown();server.server_close()
                    store.close()
                    resources_closed=True
                    path=self.directory/'daemon.json'
                    if path.exists():
                        value=json.loads(path.read_text())
                        if value.get('instance')==self.id:path.unlink()
        except Exception as error:
            self.failure=type(error).__name__;self.ready.set()
            # Never write a raw exception or model prompt to service logs.
            print('Garik runtime stopped: '+self.failure,flush=True)
            raise RuntimeError('background runtime could not start or continue') from None
        finally:
            if not resources_closed:
                self.stopping.set()
                if session:
                    try:session.close()
                    except Exception:pass
                if server:
                    if server_thread and server_thread.is_alive():server.shutdown()
                    server.server_close()
                if store:store.close()
            if transport_owner:transport_owner.close()
    def deliver(self,store,ledger):
        for item in store.pending_output('operator','local'):
            with store.db:
                store.db.execute('INSERT OR IGNORE INTO panel_messages VALUES (?,?,?,?,NULL)',(item['id'],item['event_id'],item['text'],time.time()))
                store.acknowledge(item['id'])
            ledger.emit('chat.delivered',{'message_id':item['id'],'ordinal':item['ordinal']})

def same_secret(left,right):
    try:return hmac.compare_digest(left.encode('utf-8'),right.encode('utf-8'))
    except (AttributeError,UnicodeError):return False

def handler(runtime):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def response(self,status,value,content_type='application/json',cookie=False):
            body=value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            if cookie:self.send_header('Set-Cookie','garik_panel_'+str(runtime.port)+'='+runtime.cookie+'; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def origin_ok(self):
            return self.headers.get('Host')=='127.0.0.1:'+str(runtime.port) and self.headers.get('Origin',runtime.origin)==runtime.origin and self.headers.get('Sec-Fetch-Site','none') not in ('cross-site','same-site')
        def authenticated(self):
            auth=self.headers.get('Authorization','')
            if auth.startswith('Bearer ') and same_secret(auth[7:],runtime.control):return True
            try:
                cookie=SimpleCookie(self.headers.get('Cookie',''));value=cookie['garik_panel_'+str(runtime.port)].value
                return same_secret(value,runtime.cookie)
            except (KeyError,ValueError):return False
        def do_GET(self):
            if not self.origin_ok():return self.response(403,{'error':'origin rejected'})
            path=self.path.split('?',1)[0]
            if path in ('/','/session'):
                if path=='/session':return self.response(200,{'ok':True},cookie=True)
                return self.response(200,(Path(__file__).parent/'panel/index.html').read_text(encoding='utf-8'),'text/html; charset=utf-8',True)
            assets={'/panel.css':('panel/panel.css','text/css; charset=utf-8'),'/panel.js':('panel/panel.js','text/javascript; charset=utf-8')}
            if path in assets:
                file,mime=assets[path];return self.response(200,(Path(__file__).parent/file).read_text(encoding='utf-8'),mime)
            if path=='/avatar.png':
                body=(ROOT/'resources/character/garik-final.png').read_bytes()
                self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if not self.authenticated():return self.response(401,{'error':'local session required'})
            if path=='/api/state':
                since=None
                if '?' in self.path:
                    try:since=int(self.path.split('since=',1)[1])
                    except (ValueError,IndexError):pass
                return self.response(200,runtime.state(since))
            return self.response(404,{'error':'not found'})
        def do_POST(self):
            if not self.origin_ok():return self.response(403,{'error':'origin rejected'})
            if not self.authenticated():return self.response(401,{'error':'local session required'})
            bearer=same_secret(self.headers.get('Authorization',''),'Bearer '+runtime.control)
            if not bearer and not same_secret(self.headers.get('X-Panel-CSRF',''),runtime.csrf):return self.response(403,{'error':'csrf rejected'})
            if self.headers.get('Content-Type')!='application/json':return self.response(415,{'error':'json required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError()
                value=json.loads(self.rfile.read(length))
                path=self.path
                if path=='/api/message':
                    if set(value)!={'id','text'} or not isinstance(value['text'],str) or not 0<len(value['text'].strip())<=16000:raise ValueError()
                    uuid.UUID(value['id']);result=runtime.request('message',value)
                elif path=='/api/action':
                    if set(value)!={'action'} or value['action'] not in ('pause','resume','cancel','stop'):raise ValueError()
                    result=runtime.request('action',value)
                elif path=='/api/seen':
                    if set(value)!={'ids'} or not isinstance(value['ids'],list) or len(value['ids'])>100 or any(not isinstance(x,str) for x in value['ids']):raise ValueError()
                    result=runtime.request('seen',value)
                else:return self.response(404,{'error':'not found'})
                self.response(200,result)
            except (ValueError,TypeError,KeyError,RuntimeError):self.response(400,{'error':'request rejected'})
    return Handler

def control(directory,path='/api/state',value=None):
    descriptor=Path(directory)/'daemon.json'
    try:record=json.loads(descriptor.read_text())
    except (OSError,ValueError):return None
    # A private file never controls arbitrary destinations.
    from urllib.parse import urlsplit
    try:
        if not isinstance(record,dict):return None
        parsed=urlsplit(record.get('url',''))
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or not parsed.port or parsed.path or parsed.query or parsed.fragment:return None
        if not isinstance(record.get('control'),str) or not isinstance(record.get('instance'),str):return None
        req=Request(record['url']+path,data=json.dumps(value).encode() if value is not None else None,
                    headers={'Authorization':'Bearer '+record['control'],'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(req,timeout=9) as response:result=json.loads(response.read())
        if not isinstance(result,dict):return None
        if path=='/api/state' and (result.get('instance')!=record['instance'] or result.get('pid')!=record.get('pid')):return None
        return result
    except (OSError,URLError,ValueError,TypeError,KeyError):return None

def start(directory,limits=None,port=8765,codex=None,model=None,timeout=180):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if control(directory):return {'running':True,'already_running':True,'url':json.loads((directory/'daemon.json').read_text())['url']}
    limits=(limits or Limits()).validate()
    args=[sys.executable,'-m','persona_runtime','--data-dir',str(directory),'--timeout',str(timeout)]
    if codex:args+=['--codex',codex]
    if model:args+=['--model',model]
    args+=['daemon','run','--port',str(port)]
    for key,value in asdict(limits).items():
        if type(value) is bool:
            if not value:args+=['--no-'+key]
        else:args+=['--'+key.replace('_','-'),str(value)]
    fd=os.open(directory/'daemon.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as output:
        process=subprocess.Popen(args,cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    until=time.monotonic()+10
    while time.monotonic()<until:
        if control(directory):return {'running':True,'url':json.loads((directory/'daemon.json').read_text())['url'],'pid':process.pid}
        if process.poll() is not None:raise RuntimeError('daemon startup failed; database/port may already have an owner')
        time.sleep(.1)
    process.terminate()
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    raise RuntimeError('daemon startup not confirmed; its new process was stopped')

def stop(directory):
    if control(directory,'/api/action',{'action':'stop'}) is None:return {'running':False,'stop_confirmed':False,'note':'No responding daemon; no unrelated PID was killed.'}
    until=time.monotonic()+10
    while time.monotonic()<until:
        if not control(directory):
            try:
                with worker_lock(directory):pass
            except RuntimeError:pass
            else:return {'running':False,'stop_confirmed':True}
        time.sleep(.1)
    return {'running':True,'stop_confirmed':False,'note':'Still stopping; do not start another worker.'}
