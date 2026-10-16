"""Central host for two independent characters; preparation is offline by default."""
import argparse
from contextlib import ExitStack
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import time

from .character_profile import CharacterProfile
from .channel_life import DEFAULT_CONTROLS, MOSCOW, START_DESIRE, controls_checked, AwakeGuard, live_interpreter
from .daemon import same_secret
from .local_http import local_request, local_navigation
from .profile_host import bind_connection
from .shared_world import SharedWorldStore, SharedTimeline
from .shared_scheduler import SharedScheduler, SharedChannelLife
from .telegram import BotAPI, TelegramConnection, TelegramError, private_json
from .telegram_setup import Setup, LocalServer, handler as setup_handler
from .world import WorldConflict, WorldSession
from .world_lab import Laboratory

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).parent / 'shared_panel'


def load_config(path):
    path = Path(path).resolve(); value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {'version','data_dir','port','actors','shared_places'} or
            value['version'] != 1 or not isinstance(value['actors'], list) or len(value['actors']) != 2 or
            not isinstance(value['shared_places'], list) or any(not isinstance(s,str) for s in value['shared_places'])):
        raise ValueError('shared config needs version 1 and exactly two actors')
    def resolve(text):
        if not isinstance(text,str) or not text:
            raise ValueError('empty configuration path')
        return (path.parent / text).resolve()
    directory = resolve(value['data_dir'])
    if ROOT in directory.parents and ROOT/'.local' not in directory.parents:
        raise ValueError('repository runtime data must stay under .local/')
    actors = []
    for item in value['actors']:
        if not isinstance(item,dict) or set(item) != {'profile','telegram_dir','setup_port','initial_place'}:
            raise ValueError('invalid actor host configuration')
        from .profile_host import effective_profile
        profile_path = resolve(item['profile']); profile = effective_profile(directory,CharacterProfile.load(profile_path))
        telegram = resolve(item['telegram_dir'])
        if telegram == directory or telegram in directory.parents or directory in telegram.parents:
            raise ValueError('use separate world and Telegram directories')
        if ROOT in telegram.parents and ROOT/'.local' not in telegram.parents:
            raise ValueError('repository Telegram data must stay under .local/')
        if item['initial_place'] is not None and not isinstance(item['initial_place'],str):
            raise ValueError('initial place must be an entity id or null')
        actors.append({**item,'profile':profile,'profile_path':profile_path,'telegram_dir':telegram})
    ports = [value['port'], *(a['setup_port'] for a in actors)]
    if any(type(n) is not int or not 0<=n<=65535 for n in ports) or len([n for n in ports if n]) != len({n for n in ports if n}):
        raise ValueError('shared console and actor panels need distinct ports')
    if len({a['profile'].actor_id for a in actors}) != 2 or len({a['telegram_dir'] for a in actors}) != 2:
        raise ValueError('profiles and Telegram directories must be distinct')
    return {**value,'data_dir':directory,'actors':actors}


def identity(config):
    return {'version':1, 'profiles':{a['profile'].actor_id:a['profile'].digest for a in config['actors']},
            'shared_places':config['shared_places']}


def initialize_world(store, config, *, now=None, imported=False):
    if not store.get('paused') or store.db.execute("SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked','waiting','held')").fetchone():
        raise WorldConflict('initialize shared life only after an idle, paused shutdown')
    for item in config['actors']:
        profile = item['profile']
        installed = (store.get('character_packages') or {}).get(profile.actor_id)
        if installed is not None and installed != profile.digest:
            raise WorldConflict('imported profile differs; author migration required')
        if installed is None:
            profile.install(store)
    actors = [a['profile'].actor_id for a in config['actors']]
    if {a['id'] for a in store.state()['actors']} != set(actors):
        raise WorldConflict('unexpected actors in shared world')
    moment = time.time() if now is None else now
    with store.lock,store.db:
        if not imported:
            clock = datetime.fromtimestamp(moment, MOSCOW)
            store.put('minute',clock.hour*60+clock.minute)
        for eid in [*actors,*config['shared_places']]:
            row = store.db.execute('SELECT value FROM world_entities WHERE id=?',(eid,)).fetchone()
            if not row:
                raise ValueError('unknown shared location or actor: '+eid)
            entity = json.loads(row[0])
            if eid not in actors and entity['kind']!='place':
                raise ValueError('only a place can be shared as a meeting location')
            entity['known_to'] = list(dict.fromkeys([*entity['known_to'],*actors]))
            store.db.execute('UPDATE world_entities SET value=? WHERE id=?',(json.dumps(entity,ensure_ascii=False),eid))
        for item in config['actors']:
            actor = item['profile'].actor_id; place = item['initial_place'] if not imported else None
            if place:
                row = store.db.execute('SELECT value FROM world_entities WHERE id=?',(place,)).fetchone()
                if not row or json.loads(row[0])['kind']!='place' or actor not in json.loads(row[0])['known_to']:
                    raise ValueError('initial place must be known to its actor')
            store.db.execute('INSERT INTO peer_positions VALUES(?,?,?)',(actor,place,'author-start' if not imported else 'import-position-unknown'))
        store.put('shared_epoch',{'minute':store.get('minute'),'wall':moment})
        store.put('shared_host',identity(config))
        store.put('shared_activation',{'at':moment,'imported':imported,'basis':'explicit_shared_host_initialization'})
        store.put('shared_public_floor',store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM world_events').fetchone()[0])
        store.put('settings',{**store.get('settings'),'post_cooldown_minutes':0})
        store.put('revision',store.get('revision')+1)
        store.snapshot()


def open_shared_world(config, initialize=False):
    directory = config['data_dir']
    if not (directory/'world.sqlite3').exists() and not initialize:
        raise WorldConflict('new shared world needs explicit --init; import an existing life with shared_migrate instead')
    store = SharedWorldStore(directory)
    try:
        if store.get('shared_host'):
            if store.get('shared_host') != identity(config):
                raise WorldConflict('shared profiles changed; use an explicit migration')
        elif initialize and not store.db.execute('SELECT 1 FROM world_cycles').fetchone() and not store.state()['actors']:
            initialize_world(store,config)
        else:
            raise WorldConflict('existing life requires explicit shared migration, never --init over history')
        return store
    except Exception:
        store.close();raise


def attach_channel(store, item, connection, now):
    actor = item['profile'].actor_id; binding = connection.get('binding')
    if not binding:
        raise TelegramError('Configure both channels in their local panels before --live.')
    bind_connection(connection,actor,store.directory)
    with store.lock,store.db:
        if not store.actor_get(actor,'channel_epoch'):
            store.actor_put(actor,'channel_epoch',store.get('shared_epoch'))
            store.actor_put(actor,'channel_start',{'desire':START_DESIRE,'basis':'author_requested_starting_desire'})
            store.actor_put(actor,'life_controls',{**DEFAULT_CONTROLS, **(store.actor_get(actor,'life_controls') or {})})
        status = store.actor_get(actor,'life_status') or {'next_wake':now,'last_cycle':None,'error':None}
        # Host startup stays paused. It never clears an unresolved error.
        status['status'] = 'error' if status.get('error') else 'paused'
        store.actor_put(actor,'life_status',status)
        store.actor_put(actor,'channel_life',{'enabled':True,'run':store.directory.name})
        store.actor_put(actor,'channel_connection',{'channel_name':binding['channel'].get('title','Свой канал'),
            'available':True,'explanation':'Свой канал уже подключён. Готовые тексты публикуются дословно по твоему выбору. '
            'Комментарии и фотографии доступны отдельно; квитанции подтверждают отправку. Это свободный канал, мат допустим по контексту. '
            'Общение с другим героем само по себе не публикуется. Можно писать о полученных словах и общих событиях в своём канале, '
            'отвечать в обсуждении другого канала, предложить встречу или оставить мысль при себе. Графика и квоты постов нет.'})


def verify_connections(connections):
    if len({c.get('bot')['id'] for c in connections.values()}) != 2:
        raise TelegramError('Каждому персонажу нужен отдельный бот.')
    if len({c.get('binding')['channel']['id'] for c in connections.values()}) != 2:
        raise TelegramError('Персонажам нужны отдельные каналы.')
    for actor, sender in connections.items():
        for peer, target in connections.items():
            if peer==actor:continue
            rights = sender.api.call('getChatMember',{'chat_id':target.get('binding')['discussion']['id'],'user_id':sender.get('bot')['id']})
            if rights.get('status') not in ('creator','administrator','member','restricted') or rights.get('can_send_messages') is False:
                raise TelegramError('Добавьте бота '+actor+' в обсуждение другого героя с правом писать.')


class SharedPanel:
    def __init__(self,store,config,connections,lives=None,scheduler=None):
        self.store,self.config,self.connections = store,config,connections
        self.lives,self.scheduler = lives or {},scheduler
        self.cookie,self.csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        self.urls = {}

    def state(self):
        from .profile_editor import editor_state
        from .world_interventions import catalog
        actors = []
        for item in self.config['actors']:
            p=item['profile'];actor=p.actor_id;life=self.lives.get(actor)
            actors.append({'id':actor,'name':p.name,'model':getattr(life.lab.session.backend,'model',None) if life else p.runtime.get('model'),
                'reasoning_effort':getattr(life.lab.session.backend,'reasoning_effort',None) if life else p.runtime.get('reasoning_effort'),
                'configured':bool(self.connections[actor].get('binding')),'panel_url':self.urls.get(actor),
                'life':life.state() if life else None,'peer_context':self.store.peer_context(actor),
                'controls': life.get('life_controls') if life else {**DEFAULT_CONTROLS, **(self.store.actor_get(actor,'life_controls') or {})},
                'profile':editor_state(p,paused=self.store.get('paused'),busy=any(l.lab.busy for l in self.lives.values())),
                'memories':len(self.store.memories(actor))})
        return {'csrf':self.csrf,'pid':os.getpid(),'live':bool(self.scheduler),'paused':self.store.get('paused'),
                'error':self.store.get('shared_error'),
                'world_events':catalog(self.store),
                'minute':self.store.get('minute'),'revision':self.store.get('revision'),'actors':actors,
                'settings':self.store.get('settings'), 'settings_revision':self.store.get('settings_revision'),
                'cycles':[{k:c[k] for k in ('id','actor_id','status','phase','revision')}
                          for c in self.store.monitor()['cycles'] if c['status'] not in ('complete','abandoned')],
                'peer_jobs':[{**dict(r),'value':json.loads(r['value'])} for r in self.store.db.execute('SELECT id,sender,recipient,status,error,value FROM peer_jobs ORDER BY rowid DESC LIMIT 20')]}


def panel_handler(panel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,value,mime='application/json',cookie=False):
            body=(json.dumps(value,ensure_ascii=False) if mime=='application/json' else value).encode()
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            if cookie:self.send_header('Set-Cookie',f'shared_{self.server.server_port}={panel.cookie}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def auth(self):
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:return False
            key=f'shared_{self.server.server_port}'
            return key in cookie and same_secret(cookie[key].value,panel.cookie)
        def do_GET(self):
            if not local_request(self) and not local_navigation(self):return self.reply(403,{'error':'localhost only'})
            assets={'/':('index.html','text/html; charset=utf-8'),'/shared.js':('shared.js','text/javascript; charset=utf-8'),'/shared.css':('shared.css','text/css; charset=utf-8')}
            if self.path == '/character-settings.js':return self.reply(200,(ASSETS.parent/'character_settings.js').read_text(),'text/javascript; charset=utf-8')
            if self.path in assets:
                file,mime=assets[self.path];return self.reply(200,(ASSETS/file).read_text(),mime,self.path=='/')
            if not self.auth():return self.reply(401,{'error':'Сначала откройте пульт.'})
            if self.path=='/api/state':return self.reply(200,panel.state())
            return self.reply(404,{'error':'Не найдено.'})
        def do_POST(self):
            origin=f'http://127.0.0.1:{self.server.server_port}'
            if not local_request(self) or self.headers.get('Origin')!=origin or not self.auth() or not same_secret(self.headers.get('X-Shared-CSRF',''),panel.csrf):
                return self.reply(403,{'error':'Откройте локальный пульт заново.'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if self.headers.get('Content-Type')!='application/json' or not 0<size<=(128000 if self.path=='/api/character-profile' else 8000):raise ValueError('invalid request')
                value=json.loads(self.rfile.read(size))
                if self.path=='/api/action' and set(value)=={'actor_id','running'} and value['actor_id'] in panel.lives:
                    panel.lives[value['actor_id']].action(value['running'])
                elif self.path=='/api/settings' and set(value)=={'settings','revision'}:
                    panel.store.set_settings(value['settings'],value['revision'])
                elif self.path=='/api/world-events' and set(value)=={'request_id','text'}:
                    from .world_interventions import enqueue
                    enqueue(panel.store,value['request_id'],value['text'])
                elif self.path=='/api/world-events/cancel' and set(value)=={'id'}:
                    from .world_interventions import cancel
                    cancel(panel.store,value['id'])
                elif self.path=='/api/character-settings' and set(value)=={'actor_id','controls'} and value['actor_id'] in panel.connections:
                    actor=value['actor_id']
                    if actor in panel.lives:
                        panel.lives[actor].configure(value['controls'])
                    else:
                        controls=controls_checked({**DEFAULT_CONTROLS, **(panel.store.actor_get(actor,'life_controls') or {}), **value['controls']})
                        with panel.store.lock,panel.store.db:panel.store.actor_put(actor,'life_controls',controls)
                elif self.path=='/api/character-profile' and set(value)=={'actor_id','profile'}:
                    from .profile_editor import edit_profile
                    item=next((a for a in panel.config['actors'] if a['profile'].actor_id==value['actor_id']),None)
                    if item is None:raise WorldConflict('unknown character')
                    item['profile']=edit_profile(panel.store,item['profile'],value['profile'],panel.connections.values(),
                                                  lives=panel.lives.values(),scheduler=panel.scheduler)
                elif self.path=='/api/peer-confirm-absent' and set(value)=={'actor_id','job_id','confirmation'} and value['confirmation']=='owner_checked_absent' and panel.scheduler and value['actor_id'] in panel.lives:
                    panel.scheduler.confirm_peer_absent(value['actor_id'],value['job_id'])
                else:raise ValueError('Персонажи запускаются только в режиме --live; проверьте оба подключения.')
                return self.reply(200,{'ok':True})
            except (ValueError,WorldConflict,TelegramError,KeyError,TypeError) as error:
                return self.reply(409,{'error':str(error)})
    return Handler


def shared_awake_guard(store,hours):
    return AwakeGuard(SimpleNamespace(store=store,get=store.get,put=store.put),hours)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'shared-life.example.json')
    parser.add_argument('--init',action='store_true',help='explicit NEW world, never reset existing history')
    parser.add_argument('--live',action='store_true',help='enable model and Telegram life after configuration')
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--background',action='store_true')
    parser.add_argument('--awake-hours',type=int,default=0,help='0 holds sleep prevention while live host runs; otherwise 1–12 hours')
    parser.add_argument('--write-config',type=Path,help='write a NEW private config with absolute paths, without starting a host')
    args=parser.parse_args(argv)
    interpreter=live_interpreter(ROOT,sys.executable,sys.platform)
    if interpreter:os.execv(interpreter,[interpreter,'-m','persona_runtime.shared_host',*(sys.argv[1:] if argv is None else argv)])
    if not 0<=args.awake_hours<=12:parser.error('awake duration must be 0–12 hours')
    config=load_config(args.config)
    if args.write_config:
        output=args.write_config.expanduser().resolve()
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        serialized={**config,'data_dir':str(config['data_dir']),'actors':[
            {k:(str(a['profile_path']) if k=='profile' else str(a[k]) if k=='telegram_dir' else a[k])
             for k in ('profile','telegram_dir','setup_port','initial_place')} for a in config['actors']]}
        fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(serialized,stream,ensure_ascii=False,indent=2)
        print(json.dumps({'config':str(output),'started':False},ensure_ascii=False));return
    if args.start and not args.live:parser.error('--start requires --live')
    if args.background:
        directory=config['data_dir'];directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        arguments=list(sys.argv[1:] if argv is None else argv);arguments.remove('--background')
        fd=os.open(directory/'shared.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as log:
            child=subprocess.Popen([sys.executable,'-m','persona_runtime.shared_host',*arguments],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('shared host stopped; see private shared.log')
            try:
                info=json.loads((directory/'shared.json').read_text())
                if info['pid']==child.pid:print(json.dumps(info,ensure_ascii=False),flush=True);return
            except (OSError,ValueError,KeyError):pass
            time.sleep(.1)
        child.terminate();child.wait(timeout=10);raise RuntimeError('shared host readiness timeout')
    with ExitStack() as resources:
        store=open_shared_world(config,args.init);resources.callback(store.close)
        store.pause(True)
        from .proxy import SharedTransport
        owner=SharedTransport();resources.callback(owner.close)
        connections={};lives={};scheduler=None
        for item in config['actors']:
            p=item['profile'];connection=TelegramConnection(item['telegram_dir'],api_factory=lambda token:BotAPI(token,transport_factory=owner.borrow));resources.callback(connection.close)
            if connection.get('host_profile') and connection.get('host_profile')!={'actor_id':p.actor_id,'world':str(store.directory)}:
                raise WorldConflict('Telegram connection requires explicit shared migration')
            from .profile_host import claim_setup
            claim_setup(connection,p.actor_id);connections[p.actor_id]=connection
        if args.live:
            if not all(c.get('binding') for c in connections.values()):raise TelegramError('Настройте обоих ботов в режиме подготовки.')
            for c in connections.values():c.bind(c.get('binding')['channel']['id'])
            verify_connections(connections)
            from .proxy import SharedTransport
            from .world_backend import WorldBackend
            from .world_image_backend import CodexImageBackend
            actor_ids={item['profile'].actor_id for item in config['actors']}
            portraits={subject:path for item in config['actors'] for subject,path in item['profile'].references.items() if subject in actor_ids}
            for item in config['actors']:
                p=item['profile'];actor=p.actor_id;connection=connections[actor]
                attach_channel(store,item,connection,time.time())
                backend=WorldBackend(store.directory/actor/'transport',transport_factory=owner.borrow,**p.runtime);resources.callback(backend.close)
                research=WorldBackend(store.directory/actor/'research-transport',research_host=True,transport_factory=owner.borrow,max_known_tokens=50000,**p.runtime);resources.callback(research.close)
                renderer=CodexImageBackend(store.directory/actor/'image-transport',transport_factory=owner.borrow,**p.runtime);resources.callback(renderer.close)
                session=WorldSession(store,backend,{actor:p.persona},p.speech_contract,autonomous_posts=True,personal_life=True,
                    free_intentions=True,research_enabled=True,research_backend=research,lossless_intentions=True,active_world=True,
                    direct_channel=True,continuous=True,external_channel=True,timeline=SharedTimeline(store),lively_world=True)
                lab=Laboratory(store,session);lab.actor_id,lab.actor_name=actor,p.name
                lab.profile=p
                lives[actor]=SharedChannelLife(lab,connection,p.persona,renderer,social_enabled=True,actor_id=actor,references={**p.references,**portraits},recall_enabled=True)
            scheduler=SharedScheduler(store,lives);resources.callback(scheduler.stopping.set)
            for life in lives.values():life.lab.session.cancel=scheduler.stopping;life.cancel=scheduler.stopping
        panel=SharedPanel(store,config,connections,lives,scheduler)
        servers=[];threads=[]
        for item in config['actors']:
            p=item['profile'];setup=Setup(connections[p.actor_id],life=lives.get(p.actor_id),profile=p)
            server=LocalServer(('127.0.0.1',item['setup_port']),setup_handler(setup));servers.append(server)
            panel.urls[p.actor_id]='http://127.0.0.1:'+str(server.server_port)
        central=LocalServer(('127.0.0.1',config['port']),panel_handler(panel));servers.append(central)
        stopping=threading.Event()
        awake=shared_awake_guard(store,args.awake_hours) if args.live else None
        if awake:resources.callback(awake.close)
        def stop(*unused):
            stopping.set();store.pause(True)
            if scheduler:scheduler.stopping.set()
        try:
            signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
            for actor,life in lives.items():life.lab.world_url='http://127.0.0.1:'+str(central.server_port)
            for connection in connections.values():connection.start()
            for server in servers:
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.2},daemon=True);thread.start();threads.append(thread)
            if scheduler:
                if args.start:
                    for life in lives.values():life.action(True)
                worker=threading.Thread(target=scheduler.loop,daemon=True,name='shared-life');worker.start();threads.append(worker)
            info={'pid':os.getpid(),'url':'http://127.0.0.1:'+str(central.server_port),'panels':panel.urls,'live':args.live,'data':str(store.directory)}
            private_json(store.directory/'shared.json',info);print(json.dumps(info,ensure_ascii=False),flush=True)
            if awake:awake.tick()
            while not stopping.wait(1):
                if awake:awake.tick()
        finally:
            stop()
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(40)


if __name__=='__main__':main()
