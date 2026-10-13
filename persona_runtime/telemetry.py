"""Observable receipts and exact post-hoc usage. Never store reasoning/transcripts."""
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode

FIELDS=('input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens')

def usage_values(value):
    return {key:value[key] if type(value.get(key)) is int and value[key]>=0 else None for key in FIELDS}

def safe_action(action):
    if not isinstance(action,dict):action={}
    result={}
    for key in ('type','query','pattern'):
        if isinstance(action.get(key),str):result[key]=action[key][:400]
    if isinstance(action.get('queries'),list):result['queries']=[str(x)[:400] for x in action['queries'][:4]]
    url=action.get('url')
    if isinstance(url,str):
        try:
            parsed=urlsplit(url)
            if parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password:
                params=[(k,'[hidden]' if any(x in k.lower() for x in ('token','key','secret','password','auth','session','code')) else v) for k,v in parse_qsl(parsed.query)]
                result['url']=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(params),''))[:2000]
        except ValueError:pass
    return result

SCHEMA="""
CREATE TABLE IF NOT EXISTS call_ledger (
 id TEXT PRIMARY KEY,attempt_id INTEGER UNIQUE,source_event TEXT NOT NULL,
 session_id TEXT,category TEXT NOT NULL,model TEXT,started REAL NOT NULL,
 finished REAL,status TEXT NOT NULL,usage TEXT NOT NULL,context TEXT NOT NULL,
 legacy INTEGER NOT NULL DEFAULT 0,call_count INTEGER
);
CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY,call_id TEXT,kind TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daemon_sessions (id TEXT PRIMARY KEY,started REAL NOT NULL,ended REAL,pid INTEGER NOT NULL,limits TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS panel_messages (id TEXT PRIMARY KEY REFERENCES outbox(id),event_id TEXT NOT NULL,text TEXT NOT NULL,delivered REAL NOT NULL,seen REAL);
"""

class Ledger:
    def __init__(self,store):
        self.store,self.db=store,store.db
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='call_ledger'").fetchone() and self.db.execute('SELECT 1 FROM turns LIMIT 1').fetchone():
            store.backup(store.directory/'backups'/('before-panel-'+str(uuid.uuid4())+'.sqlite3'))
        self.db.executescript(SCHEMA)
        self.spool=store.directory/'observations.jsonl'
        if not self.spool.exists():
            descriptor=os.open(self.spool,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
        # Only at startup, before any producer exists: discard a torn final write.
        with self.spool.open('r+b') as source:
            source.seek(0,2);end=source.tell()
            if end:
                source.seek(end-1)
                if source.read(1)!=b'\n':
                    position=end
                    while position:
                        begin=max(0,position-4096);source.seek(begin)
                        block=source.read(position-begin);newline=block.rfind(b'\n')
                        if newline>=0:position=begin+newline+1;break
                        position=begin
                    source.truncate(position);source.flush();os.fsync(source.fileno())
        self.import_legacy()
        self.ingest()

    def import_legacy(self):
        with self.db:
            # Completed old turns have an aggregate, possibly covering several calls.
            # Do not also sum individual attempts of those historical turns.
            completed=set()
            for row in self.db.execute('SELECT t.*,e.kind FROM turns t JOIN events e ON e.id=t.event_id').fetchall():
                completed.add(row['event_id']);value=json.loads(row['usage'])
                if self.db.execute('SELECT 1 FROM call_ledger WHERE source_event=?',(row['event_id'],)).fetchone():continue
                self.db.execute('INSERT OR IGNORE INTO call_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('legacy:'+row['event_id'],None,row['event_id'],None,'legacy_'+row['kind'],None,row['completed'],row['completed'],'completed',json.dumps(usage_values(value)),'{}',1,value.get('model_calls') if type(value.get('model_calls')) is int and value['model_calls']>0 else None))
            for row in self.db.execute('SELECT a.*,e.kind FROM model_attempts a JOIN events e ON e.id=a.source_event').fetchall():
                if row['source_event'] in completed:continue
                receipt=json.loads(row['result'] or '{}')
                self.db.execute('INSERT OR IGNORE INTO call_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('attempt:'+str(row['id']),row['id'],row['source_event'],None,'legacy_'+row['kind'],None,row['started'],None,row['status'],json.dumps(usage_values(receipt.get('usage',{}))),'{}',1,1))
            self.store.put('ledger_legacy_imported','1')

    def open_call(self,turn,session_id,model,context):
        call_id='attempt:'+str(turn.attempt)
        category='search' if turn.web_turn else ('recall' if turn.recall_used else turn.event['kind'])
        with self.db:
            self.db.execute('INSERT INTO call_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (call_id,turn.attempt,turn.event['id'],session_id,category,model,time.time(),None,'running',json.dumps(usage_values({})),json.dumps(context),0,1))
        self.emit('call.started',{'category':category,'query':turn.web_request if turn.web_turn else None},call_id)
        def observe(event):
            # Producer does no SQLite work. Critical receipts fsync BEFORE returning.
            kind=event.get('type');data={}
            if kind=='usage':data={'usage':usage_values(event.get('usage',{}))}
            elif kind=='process.started':data={'pid':event['pid']}
            elif kind=='cli.diagnostic':
                data={k:v for k,v in event.items() if k in ('event','categories','after_completion','retry_attempt','retry_limit')}
            elif kind=='cli.result':
                data={k:v for k,v in event.items() if k in ('turn_completed','turn_failed','exit_code','answer_present','answer_valid','retry_recovered','startup_ms','first_delta_ms','elapsed_ms','persistent_session','recovered_without_inference')}
            elif kind=='session.input':
                data={k:v for k,v in event.items() if k in ('startup_ms','sent_chars','new_thread')}
            elif kind=='web.tool':data={'id':str(event.get('id',''))[:160],'stage':event.get('stage'),'failed':bool(event.get('failed')),'action':safe_action(event.get('action',{}))}
            else:return
            value={'id':str(uuid.uuid4()),'call_id':call_id,'kind':kind,'data':data,'created':time.time()}
            payload=(json.dumps(value,ensure_ascii=False)+'\n').encode()
            fd=os.open(self.spool,os.O_WRONLY|os.O_APPEND)
            try:
                offset=0
                while offset<len(payload):offset+=os.write(fd,payload[offset:])
                os.fsync(fd)
            finally:os.close(fd)
        return observe

    def emit(self,kind,data,call_id=None):
        with self.db:self.db.execute('INSERT INTO observations VALUES (?,?,?,?,?)',(str(uuid.uuid4()),call_id,kind,json.dumps(data,ensure_ascii=False),time.time()))

    def ingest(self):
        offset=int(self.store.get('observations_offset','0'))
        with self.spool.open('rb') as source:
            source.seek(offset)
            while True:
                start=source.tell();line=source.readline()
                if not line or not line.endswith(b'\n'):break
                try:record=json.loads(line)
                except ValueError:
                    with self.db:self.store.put('observations_offset',source.tell())
                    continue
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)',(record['id'],record['call_id'],record['kind'],json.dumps(record['data']),record['created']))
                    if record['kind']=='usage':self.merge_usage(record['call_id'],record['data']['usage'])
                    if record['kind']=='cli.result' and record['data'].get('recovered_without_inference'):
                        self.db.execute('UPDATE call_ledger SET call_count=0 WHERE id=?',(record['call_id'],))
                    self.store.put('observations_offset',source.tell())

    def merge_usage(self,call_id,value):
        row=self.db.execute('SELECT usage FROM call_ledger WHERE id=?',(call_id,)).fetchone()
        if not row:return
        old=json.loads(row['usage'])
        for key,number in usage_values(value).items():
            if number is not None:old[key]=number
        encoded=json.dumps(old)
        if encoded!=row['usage']:self.db.execute('UPDATE call_ledger SET usage=? WHERE id=?',(encoded,call_id))

    def sync(self):
        self.ingest()
        with self.db:
            for row in self.db.execute("SELECT l.id,a.status,a.result FROM call_ledger l JOIN model_attempts a ON a.id=l.attempt_id").fetchall():
                receipt=json.loads(row['result'] or '{}')
                self.merge_usage(row['id'],receipt.get('usage',{}))
                status='running' if row['status']=='started' else row['status']
                old=self.db.execute('SELECT status FROM call_ledger WHERE id=?',(row['id'],)).fetchone()['status']
                if old!=status:
                    self.db.execute('UPDATE call_ledger SET status=?,finished=CASE WHEN ? THEN COALESCE(finished,?) ELSE finished END WHERE id=?',(status,status!='running',time.time(),row['id']))
                    self.db.execute('INSERT INTO observations VALUES (?,?,?,?,?)',(str(uuid.uuid4()),row['id'],'call.finished',json.dumps({'status':status}),time.time()))

    def totals(self,session_id=None,day=False):
        query='SELECT * FROM call_ledger';args=[]
        if session_id:query+=' WHERE session_id=?';args=[session_id]
        elif day:query+=' WHERE started>=?';args=[int(time.time()//86400)*86400]
        rows=list(self.db.execute(query,args));result={'attempts':sum(row['call_count'] if row['call_count'] is not None else 1 for row in rows),'unknown_attempt_groups':sum(row['call_count'] is None for row in rows),'known_calls':0,'known_token_calls':0,'unknown_calls':0,'total_tokens':0,**{k:0 for k in FIELDS},'coverage':{k:0 for k in FIELDS}}
        for row in rows:
            if row['call_count']==0:continue
            values=json.loads(row['usage'])
            known=values.get('input_tokens') is not None and values.get('output_tokens') is not None
            result['known_calls' if known else 'unknown_calls']+=row['call_count'] or 1
            if values.get('input_tokens') is not None or values.get('output_tokens') is not None:
                result['known_token_calls']+=row['call_count'] or 1
                result['total_tokens']+=(values.get('input_tokens') or 0)+(values.get('output_tokens') or 0)
            for key in FIELDS:
                if values.get(key) is not None:result[key]+=values[key];result['coverage'][key]+=row['call_count'] or 1
        return result

    def calls(self):
        result=[]
        for row in self.db.execute('SELECT * FROM call_ledger ORDER BY started DESC LIMIT 60'):
            item=dict(row);item['usage']=json.loads(item['usage']);item['context']=json.loads(item['context']);result.append(item)
        return result
