"""SQLite is canonical; source character files are immutable seed data."""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .protocol import bounded_text, validate


class StaleTurn(RuntimeError):
    pass


class Store:
    def __init__(self, directory, locked=False):
        self.locked=locked
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'runtime.sqlite3'
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, person TEXT NOT NULL,
                display_name TEXT NOT NULL, conversation TEXT NOT NULL,
                text TEXT NOT NULL, register TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, completed REAL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
                conversation TEXT NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', delivered REAL
            );
            CREATE TABLE IF NOT EXISTS memories (
                scope TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, basis TEXT NOT NULL,
                text TEXT NOT NULL, source_event TEXT NOT NULL REFERENCES events(id),
                updated REAL NOT NULL, PRIMARY KEY(scope, key)
            );
            CREATE TABLE IF NOT EXISTS turns (
                event_id TEXT PRIMARY KEY REFERENCES events(id), decision TEXT NOT NULL,
                usage TEXT NOT NULL, completed REAL NOT NULL
            );
        ''')

        from .memory import Memory
        self.memory = Memory(self)
        if self.get('memory_version') != '2':
            with (nullcontext() if self.locked else worker_lock(self.directory)):
                if self.db.execute('SELECT 1 FROM turns LIMIT 1').fetchone():
                    self.backup(self.directory / 'backups' / ('before-memory-v2-' + str(uuid.uuid4()) + '.sqlite3'))
                self.memory.migrate()
        self.migrate_life()
        self.sync_journal()

    def migrate_life(self):
        if self.get('runtime_version') == '3':
            return
        with (nullcontext() if self.locked else worker_lock(self.directory)):
            if self.db.execute('SELECT 1 FROM turns LIMIT 1').fetchone():
                self.backup(self.directory/'backups'/('before-life-v3-'+str(uuid.uuid4())+'.sqlite3'))
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute("ALTER TABLE events ADD COLUMN surface TEXT NOT NULL DEFAULT 'unknown'")
                self.db.execute("""CREATE TABLE outbox_v3 (
                    id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events(id),conversation TEXT NOT NULL,
                    text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',delivered REAL,
                    ordinal INTEGER NOT NULL DEFAULT 0,delay_seconds INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
                    UNIQUE(event_id,ordinal))""")
                self.db.execute('INSERT INTO outbox_v3 SELECT o.id,o.event_id,o.conversation,o.text,o.status,o.delivered,0,0,e.created FROM outbox o JOIN events e ON e.id=o.event_id')
                self.db.execute('DROP TABLE outbox')
                self.db.execute('ALTER TABLE outbox_v3 RENAME TO outbox')
                self.db.execute("""CREATE TABLE activities (
                    key TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,
                    next_step TEXT NOT NULL,reason TEXT NOT NULL,due REAL NOT NULL,
                    source_event TEXT NOT NULL REFERENCES events(id),created REAL NOT NULL,updated REAL NOT NULL)""")
                self.db.execute('CREATE TABLE activity_versions (id INTEGER PRIMARY KEY,activity_key TEXT NOT NULL,source_event TEXT NOT NULL REFERENCES events(id),value TEXT NOT NULL,created REAL NOT NULL)')
                self.db.execute("CREATE TABLE audience_preferences (scope TEXT PRIMARY KEY,speech_preference TEXT NOT NULL DEFAULT 'contextual',outreach INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL)")
                self.db.execute('CREATE TABLE web_runs (id INTEGER PRIMARY KEY,source_event TEXT NOT NULL REFERENCES events(id),receipt TEXT NOT NULL,created REAL NOT NULL)')
                self.db.execute('CREATE TABLE web_sources (id TEXT PRIMARY KEY,source_event TEXT NOT NULL REFERENCES events(id),owner TEXT NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,claim TEXT NOT NULL,created REAL NOT NULL)')
                self.db.execute('CREATE TABLE model_attempts (id INTEGER PRIMARY KEY,source_event TEXT NOT NULL REFERENCES events(id),started REAL NOT NULL,status TEXT NOT NULL,result TEXT)')
                self.put('runtime_version','3')

    def audience(self,event):
        target={**event,'person':'operator','conversation':'local'} if event['kind']=='wake' else event
        row=self.db.execute('SELECT * FROM audience_preferences WHERE scope=?',(self.scope(target),)).fetchone()
        return {'surface':event.get('surface','unknown'),'speech_preference':row['speech_preference'] if row else 'contextual',
                'outreach_allowed':bool(row['outreach']) if row else True}

    def update_preferences(self,event,decision,now):
        speech=decision.get('speech_preference');outreach=decision.get('outreach')
        if speech is None and outreach is None:
            return
        if event['kind']!='message':
            raise ValueError('audience preferences need an actual audience message')
        old=self.audience(event)
        self.db.execute('INSERT OR REPLACE INTO audience_preferences VALUES (?,?,?,?)',
                        (self.scope(event),speech or old['speech_preference'],int(old['outreach_allowed'] if outreach is None else outreach),now))

    def activities(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM activities WHERE status IN ('active','waiting') ORDER BY updated DESC LIMIT 3")]

    def activity_context(self):
        # Keep stable keys of completed work available for a later continuation.
        # Completed work is history, not another open project or a duty to resume it.
        return [dict(row) for row in self.db.execute("SELECT * FROM activities ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'waiting' THEN 0 ELSE 1 END,updated DESC LIMIT 3")]

    def record_activity(self,event,value,now):
        if value is None:
            return
        previous=self.db.execute('SELECT * FROM activities WHERE key=?',(value['key'],)).fetchone()
        due=(previous['due'] if previous and previous['status'] in ('active','waiting') and event['kind']=='message' else now+value['due_after_minutes']*60)
        created=previous['created'] if previous else now
        replaced=self.db.execute("SELECT * FROM activities WHERE key<>? AND status IN ('active','waiting')",(value['key'],)).fetchall()
        for old in replaced:
            transition={**dict(old),'status':'abandoned','host_transition':'replaced by a newly selected activity','new_activity_key':value['key']}
            self.db.execute('INSERT INTO activity_versions (activity_key,source_event,value,created) VALUES (?,?,?,?)',(old['key'],event['id'],json.dumps(transition,ensure_ascii=False),now))
        self.db.execute("UPDATE activities SET status='abandoned',updated=?,source_event=? WHERE key<>? AND status IN ('active','waiting')",(now,event['id'],value['key']))
        self.db.execute('INSERT OR REPLACE INTO activities VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (value['key'],value['title'],value['kind'],value['status'],value['next_step'],value['reason'],due,event['id'],created,now))
        self.db.execute('INSERT INTO activity_versions (activity_key,source_event,value,created) VALUES (?,?,?,?)',(value['key'],event['id'],json.dumps(value,ensure_ascii=False),now))
        if value['status'] in ('active','waiting'):
            existing=self.get('next_wake')
            if not existing or float(existing)>due:
                self.put('next_wake',due)

    def is_stale(self,event):
        row=self.db.execute('SELECT rowid FROM events WHERE id=?',(event['id'],)).fetchone()
        if not row:
            return True
        if event['kind']=='wake':
            return bool(self.db.execute("SELECT 1 FROM events WHERE kind='message' AND status='pending' LIMIT 1").fetchone())
        return bool(self.db.execute("SELECT 1 FROM events WHERE rowid>? AND person=? AND conversation=? AND kind='message' AND status='pending' LIMIT 1",(row['rowid'],event['person'],event['conversation'])).fetchone())

    def supersede(self,event_id,reason='new audience input'):
        with self.db:
            self.db.execute("UPDATE events SET status='superseded',error=?,completed=? WHERE id=? AND status IN ('processing','pending')",(reason,time.time(),event_id))

    def supersede_pending(self,person,conversation,include_wakes=True):
        with self.db:
            self.db.execute("UPDATE events SET status='superseded',error='new audience input' WHERE status='pending' AND ((person=? AND conversation=?) OR (? AND kind='wake'))",(person,conversation,include_wakes))
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE status='queued' AND event_id IN (SELECT id FROM events WHERE (person=? AND conversation=?) OR (? AND kind='wake'))",(person,conversation,include_wakes))

    def start_attempt(self,event):
        with self.db:
            cursor=self.db.execute("INSERT INTO model_attempts (source_event,started,status) VALUES (?,?,'started')",(event['id'],time.time()))
            return cursor.lastrowid

    def finish_attempt(self,attempt_id,status,receipt=None):
        with self.db:
            self.db.execute('UPDATE model_attempts SET status=?,result=? WHERE id=?',(status,json.dumps(receipt or {},ensure_ascii=False),attempt_id))

    def close(self):
        self.db.close()

    def get(self, key, default=''):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', (key, str(value)))

    def seed(self, character, state, goals):
        with self.db:
            if self.get('character') and self.get('character') != character:
                raise ValueError('this data directory belongs to another character')
            self.put('character', character)
            if not self.get('state'):
                self.put('state', state)
                self.put('goals', goals)

    def enqueue(self, text, person='operator', display_name='Кирилл',
                conversation='local', register='non-obscene', event_id=None,
                kind='message', now=None, surface='unknown'):
        for value, field, limit in ((text, 'message', 16000), (person, 'person', 160),
                                    (display_name, 'display_name', 160),
                                    (conversation, 'conversation', 160)):
            bounded_text(value, field, limit)
        if surface not in ('private','formal','unknown'):
            raise ValueError('invalid audience surface')
        if register not in ('auto', 'non-obscene', 'free') or kind not in ('message', 'wake'):
            raise ValueError('invalid event kind or register')
        event_id = event_id or str(uuid.uuid4())
        bounded_text(event_id, 'event_id', 160)
        payload = (kind, person, display_name, conversation, text, register, surface)
        with self.db:
            old = self.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if old:
                if tuple(old[k] for k in ('kind', 'person', 'display_name', 'conversation', 'text', 'register', 'surface')) != payload:
                    raise ValueError('event ID already exists with different contents')
                return event_id
            self.db.execute('''INSERT INTO events
                (id, kind, person, display_name, conversation, text, register, surface, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (event_id,) + payload + (time.time() if now is None else now,))
        return event_id

    def recover(self):
        """Caller MUST hold the process lock. An interrupted call is not a completed action."""
        with self.db:
            self.db.execute("UPDATE model_attempts SET status='interrupted',result=? WHERE status='started'",(json.dumps({'reason':'worker stopped before a result was accepted'}),))
            self.db.execute("UPDATE events SET status='pending', error='interrupted before commit' WHERE status='processing' AND attempts<3")
            self.db.execute("UPDATE events SET status='failed', error='interrupted; attempt limit reached' WHERE status='processing'")

    def claim(self):
        with self.db:
            row = self.db.execute("SELECT * FROM events WHERE status='pending' ORDER BY CASE kind WHEN 'message' THEN 0 ELSE 1 END, created, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE events SET status='processing', attempts=attempts+1, error=NULL WHERE id=?", (row['id'],))
        return dict(row)

    def fail(self, event_id, error):
        with self.db:
            self.db.execute("UPDATE events SET status='failed', error=? WHERE id=? AND status='processing'", (str(error)[:1000], event_id))

    def retry(self, event_id):
        with self.db:
            changed = self.db.execute("UPDATE events SET status='pending', error=NULL WHERE id=? AND status='failed' AND attempts<3", (event_id,)).rowcount
            if not changed:
                raise ValueError('retry requires a failed event with fewer than three attempts')

    def context(self, event):
        context, _ = self.memory.context(event)
        return context

    @staticmethod
    def scope(event):
        # JSON tuple avoids collisions in user-supplied identifiers.
        return json.dumps([event['conversation'], event['person']], ensure_ascii=False)

    def complete(self, event, decision, usage, now=None, retrieval=None, web_receipts=None):
        validate(decision)
        if decision.get('memory_query') or decision.get('web_query'):
            raise ValueError('intermediate recall decisions cannot be committed')
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute('SELECT status FROM events WHERE id=?', (event['id'],)).fetchone()
            if not row or row['status'] != 'processing':
                raise ValueError('event is not processing')
            if self.is_stale(event):
                raise StaleTurn('new audience input superseded this generation')
            if decision.get('web_sources') and not any(r.get('events') for r in (web_receipts or [])):
                raise ValueError('web sources require a completed native search receipt')
            for memory in decision['memories']:
                if memory['basis'] == 'observed':
                    raise ValueError('observed receipts are host-authored; source statements are reported')
                self.db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)',
                                (self.scope(event), memory['key'], memory['kind'], memory['basis'],
                                 memory['text'], event['id'], now))
                self.memory.record(event, memory, now)
            for relation in decision.get('relations', []):
                if relation['basis'] == 'observed':
                    raise ValueError('observed relations require host authorship')
                self.memory.relation(event, relation, now)
            self.memory.episode(event)
            self.memory.journal(event, decision.get('journal', []), now)
            self.update_preferences(event, decision, now)
            pieces = decision.get('messages', [])
            if decision['reply'] is not None:
                pieces = [{'text':decision['reply'],'delay_seconds':0}]
            # A quiet request blocks unsolicited delivery, not private local activity.
            if event['kind'] == 'wake' and not self.audience(event)['outreach_allowed']:
                pieces = []
            for ordinal,piece in enumerate(pieces):
                self.db.execute('INSERT INTO outbox (id,event_id,conversation,text,ordinal,delay_seconds,created) VALUES (?,?,?,?,?,?,?)',
                                (str(uuid.uuid4()),event['id'],event['conversation'],piece['text'],ordinal,
                                 piece['delay_seconds'] if ordinal else 0,now))
            self.put('state', decision['state'])
            self.put('goals', decision['goals'])
            # Conversation cannot postpone an existing autonomous deadline indefinitely.
            if event['kind'] == 'wake' or not self.get('next_wake'):
                self.put('next_wake',now+decision['wake_after_minutes']*60)
            self.record_activity(event,decision.get('activity'),now)
            for receipt in web_receipts or []:
                self.db.execute('INSERT INTO web_runs (source_event,receipt,created) VALUES (?,?,?)',
                                (event['id'],json.dumps(receipt,ensure_ascii=False),now))
            for ordinal,source in enumerate(decision.get('web_sources', [])):
                self.db.execute('INSERT INTO web_sources VALUES (?,?,?,?,?,?,?)',
                                (str(uuid.uuid5(uuid.NAMESPACE_URL,event['id']+':source:'+str(ordinal))),event['id'],
                                 'self' if event['kind']=='wake' else 'person:'+event['person'],source['url'],source['title'],source['claim'],now))
                self.memory.record(event,{'key':'web:'+source['url'],'kind':'event','basis':'reported',
                    'visibility':'self' if event['kind']=='wake' else 'person','entities':['web'],
                    'text':'Source cited after host-confirmed native search: '+source['url']+' — '+source['title']+'; source claim: '+source['claim']},now)
            self.db.execute('INSERT INTO turns VALUES (?,?,?,?)',
                            (event['id'], json.dumps(decision, ensure_ascii=False), json.dumps(usage), now))
            if retrieval is not None:
                self.db.execute('INSERT OR REPLACE INTO retrieval_log VALUES (?,?)',
                                (event['id'], json.dumps(retrieval)))
            self.db.execute("UPDATE events SET status='done', completed=?, error=NULL WHERE id=?", (now, event['id']))

        self.sync_journal()

    def sync_journal(self):
        try:
            self.memory.export_journal()
        except OSError as exc:
            # SQLite remains canonical. Export retries on the next launch without rerunning the model.
            with self.db:
                self.put('journal_export_error', str(exc)[:500])
        else:
            if self.get('journal_export_error'):
                with self.db:
                    self.put('journal_export_error', '')

    def pending_output(self, person=None, conversation=None, now=None, include_future=False):
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT o.*,e.person,e.kind FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.status='queued' ORDER BY o.created,o.ordinal,o.rowid").fetchall()
        result=[]
        for row in rows:
            if person is not None and not ((row['person']==person and row['conversation']==conversation) or
                    (person=='operator' and conversation=='local' and row['kind']=='wake')):
                continue
            previous=self.db.execute('SELECT status,delivered FROM outbox WHERE event_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT 1',(row['event_id'],row['ordinal'])).fetchone()
            if not include_future and previous and (previous['status']!='delivered' or previous['delivered']+row['delay_seconds']>now):
                continue
            result.append(dict(row))
        return result

    def acknowledge(self, output_id, now=None):
        now=time.time() if now is None else now
        with self.db:
            output=self.db.execute('SELECT * FROM outbox WHERE id=?',(output_id,)).fetchone()
            if not output or output_id not in {v['id'] for v in self.pending_output(now=now)}:
                raise ValueError('no due queued output with that ID')
            self.db.execute("UPDATE outbox SET status='delivered',delivered=? WHERE id=?",(now,output_id))
            event=self.db.execute('SELECT * FROM events WHERE id=?',(output['event_id'],)).fetchone()
            delivered=self.db.execute("SELECT text FROM outbox WHERE event_id=? AND status='delivered' ORDER BY ordinal",(event['id'],)).fetchall()
            self.memory.episode(dict(event),'\n'.join(row['text'] for row in delivered))

    def status(self):
        return {'events': {r['status']: r['n'] for r in self.db.execute('SELECT status,COUNT(*) AS n FROM events GROUP BY status')},
                'queued_replies': len(self.pending_output(include_future=True)),
                'active_activities':self.activities(),
                'initiative_paused':self.get('initiative_paused')=='1',
                'memories': self.db.execute('SELECT COUNT(*) FROM recollections WHERE valid_to IS NULL').fetchone()[0],
                'journal_entries': self.db.execute('SELECT COUNT(*) FROM journal').fetchone()[0],
                'journal_export_error': self.get('journal_export_error') or None,
                'next_wake': self.get('next_wake'),
                'failed': [dict(r) for r in self.db.execute("SELECT id,attempts,error FROM events WHERE status='failed'")]}

    def backup(self, path):
        destination = Path(path).expanduser().resolve()
        if destination == self.path or destination.exists():
            raise ValueError('backup destination must be a new file')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(str(destination)) as target:
            self.db.backup(target)
        os.chmod(destination, 0o600)


@contextmanager
def worker_lock(directory):
    """One worker per database, also across terminal windows; works on macOS/Linux."""
    import fcntl
    path = Path(directory) / 'worker.lock'
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another character worker is already running')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
