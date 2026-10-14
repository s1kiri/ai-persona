"""Durable bounded research, independent of cancellable audience turns.

Only the coordinator touches SQLite. The research worker returns evidence; it
cannot speak, update character state or mutate memories directly.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import time

from .engine import Turn
from .research import task_id,decode
from .codex import CodexCancelled


class ResearchJobs:
    def __init__(self,store):
        self.store=store;self.db=store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS research_jobs (
            id TEXT PRIMARY KEY,source_event TEXT NOT NULL REFERENCES events(id),
            owner TEXT NOT NULL,question TEXT NOT NULL,status TEXT NOT NULL,
            result TEXT,receipt TEXT,attempt INTEGER,created REAL NOT NULL,
            completed REAL,consumed_by TEXT REFERENCES events(id))''')
        self.db.commit()

    def owner(self,event):
        return self.store.scope({**event,'person':'operator','conversation':'local'} if event['kind']=='wake' else event)

    def available(self):
        return time.time()>=float(self.store.get('research_retry_after','0')) and not self.db.execute("SELECT 1 FROM research_jobs WHERE consumed_by IS NULL LIMIT 1").fetchone()

    def enqueue(self,event,question,now):
        if not self.available():raise ValueError('research is already pending; do not duplicate it')
        self.db.execute('INSERT INTO research_jobs VALUES (?,?,?,?,?,NULL,NULL,NULL,?,NULL,NULL)',
            (task_id(event['id'],question),event['id'],self.owner(event),question,'queued',now))

    def ready(self,event):
        row=self.db.execute("SELECT * FROM research_jobs WHERE owner=? AND status IN ('completed','failed','interrupted','cancelled') AND consumed_by IS NULL ORDER BY created LIMIT 1",(self.owner(event),)).fetchone()
        return dict(row) if row else None

    def context(self,event):
        return [dict(row) for row in self.db.execute('SELECT id,question,status FROM research_jobs WHERE owner=? AND consumed_by IS NULL LIMIT 1',(self.owner(event),))]

    def consume(self,job_id,event):
        changed=self.db.execute('UPDATE research_jobs SET consumed_by=? WHERE id=? AND owner=? AND consumed_by IS NULL',
            (event['id'],job_id,self.owner(event))).rowcount
        if changed!=1:raise ValueError('research evidence already consumed or belongs to another audience')


class ResearchWorker:
    def __init__(self,store,backend,max_web_calls=2):
        self.store,self.backend,self.max_web_calls=store,backend,max_web_calls
        self.jobs=store.research
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='garik-research')
        self.future=self.turn=self.job=None
        # A lost response might have incurred usage: never replay it on restart.
        with store.db:
            store.db.execute("UPDATE research_jobs SET status='interrupted',completed=? WHERE status='running'",(time.time(),))
            if self.jobs.ready({'kind':'wake','person':'self','conversation':'local'}):
                store.put('next_wake',min(float(store.get('next_wake',str(time.time()))),time.time()))

    def start(self,session):
        if self.future or session.stopped or session.exit_requested:return False
        if session.dispatch_guard and not session.dispatch_guard():return False
        if session.remaining_calls()<1:return False
        row=self.store.db.execute("SELECT * FROM research_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return False
        self.job=dict(row)
        event=dict(self.store.db.execute('SELECT * FROM events WHERE id=?',(row['source_event'],)).fetchone())
        if session.paused and event['kind']=='wake':return False
        self.turn=Turn(event,1,web_turn=True,web_request=row['question'])
        with self.store.db:
            self.turn.attempt=self.store.db.execute("INSERT INTO model_attempts (source_event,started,status) VALUES (?,?,'started')",(event['id'],time.time())).lastrowid
            self.store.db.execute("UPDATE research_jobs SET status='running',attempt=? WHERE id=?",(self.turn.attempt,row['id']))
        session.calls+=1
        if session.on_research_dispatch:session.on_research_dispatch(self.turn)
        turn=self.turn
        self.future=self.executor.submit(self.backend.research,event,row['question'],cancel=turn.cancel,
            attempt=turn.attempt,observer=turn.observer,max_web_calls=self.max_web_calls)
        return True

    def poll(self):
        if self.future is None or not self.future.done():return False
        turn,job=self.turn,self.job
        usage={};result=None;status='completed'
        try:
            result,usage=self.future.result()
            decode(json.dumps(result))
            if result['task_id']!=job['id']:raise ValueError('research task mismatch')
            if not self.backend.last_web.get('events'):raise ValueError('no search receipt')
            if turn.cancel.is_set():raise CodexCancelled('research cancelled')
        except Exception as exc:
            status='cancelled' if turn.cancel.is_set() or isinstance(exc,CodexCancelled) else 'failed'
            result=None;usage=getattr(self.backend,'last_usage',{})
        receipt={'usage':usage,'web':getattr(self.backend,'last_web',{}),'diagnostics':getattr(self.backend,'last_diagnostics',{})}
        now=time.time()
        with self.store.db:
            failures=0 if status=='completed' else int(self.store.get('research_failures','0'))+1
            self.store.put('research_failures',failures)
            self.store.put('research_retry_after',now+min(3600,60*2**min(failures,6)) if failures else 0)
            self.store.db.execute('UPDATE model_attempts SET status=?,result=? WHERE id=?',(status,json.dumps(receipt),turn.attempt))
            self.store.db.execute('UPDATE research_jobs SET status=?,result=?,receipt=?,completed=? WHERE id=?',
                (status,json.dumps(result),json.dumps(receipt),now,job['id']))
            # Only this local audience is reachable by autonomous check-ins.
            if job['owner']==self.store.scope({'person':'operator','conversation':'local'}):
                old=float(self.store.get('next_wake',str(now)))
                self.store.put('next_wake',min(old,now))
        if status=='completed' and getattr(self.backend,'supports_sessions',False):
            self.backend.acknowledge(turn.attempt)
        self.future=self.turn=self.job=None
        return True

    def cancel(self,wakes_only=False):
        if self.turn and (not wakes_only or self.turn.event['kind']=='wake'):self.turn.cancel.set()
        with self.store.db:
            self.store.db.execute("UPDATE research_jobs SET status='cancelled',completed=? WHERE status='queued' AND (?=0 OR source_event IN (SELECT id FROM events WHERE kind='wake'))",(time.time(),int(wakes_only)))

    def close(self):
        # Queued work is durable; only an in-flight call is interrupted on shutdown.
        if self.turn:self.turn.cancel.set()
        self.executor.shutdown(wait=True,cancel_futures=True)
        self.poll()
        if getattr(self.backend,'supports_sessions',False):self.backend.close()
