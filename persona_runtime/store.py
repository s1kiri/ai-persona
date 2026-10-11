"""SQLite is canonical; source character files are immutable seed data."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .protocol import bounded_text, validate


class Store:
    def __init__(self, directory):
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
                kind='message', now=None):
        for value, field, limit in ((text, 'message', 16000), (person, 'person', 160),
                                    (display_name, 'display_name', 160),
                                    (conversation, 'conversation', 160)):
            bounded_text(value, field, limit)
        if register not in ('non-obscene', 'free') or kind not in ('message', 'wake'):
            raise ValueError('invalid event kind or register')
        event_id = event_id or str(uuid.uuid4())
        bounded_text(event_id, 'event_id', 160)
        payload = (kind, person, display_name, conversation, text, register)
        with self.db:
            old = self.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if old:
                if tuple(old[k] for k in ('kind', 'person', 'display_name', 'conversation', 'text', 'register')) != payload:
                    raise ValueError('event ID already exists with different contents')
                return event_id
            self.db.execute('''INSERT INTO events
                (id, kind, person, display_name, conversation, text, register, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (event_id,) + payload + (time.time() if now is None else now,))
        return event_id

    def recover(self):
        """Caller MUST hold the process lock. An interrupted call is not a completed action."""
        with self.db:
            self.db.execute("UPDATE events SET status='pending', error='interrupted before commit' WHERE status='processing' AND attempts<3")
            self.db.execute("UPDATE events SET status='failed', error='interrupted; attempt limit reached' WHERE status='processing'")

    def claim(self):
        with self.db:
            row = self.db.execute("SELECT * FROM events WHERE status='pending' ORDER BY created, rowid LIMIT 1").fetchone()
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
        scope = self.scope(event)
        memories = [dict(r) for r in self.db.execute('''SELECT key,kind,basis,text,source_event FROM memories
            WHERE scope=? ORDER BY updated DESC, key LIMIT 20''', (scope,))]
        rows = list(self.db.execute('''SELECT e.id,e.text,o.text AS reply FROM events e
            LEFT JOIN outbox o ON o.event_id=e.id AND o.status='delivered'
            WHERE e.conversation=? AND e.person=? AND e.kind='message' AND e.status='done'
            ORDER BY e.created DESC,e.rowid DESC LIMIT 6''', (event['conversation'], event['person'])))
        return {'state': self.get('state'), 'goals': self.get('goals'),
                'memories': memories, 'history': [dict(r) for r in reversed(rows)]}

    @staticmethod
    def scope(event):
        # JSON tuple avoids collisions in user-supplied identifiers.
        return json.dumps([event['conversation'], event['person']], ensure_ascii=False)

    def complete(self, event, decision, usage, now=None):
        validate(decision)
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute('SELECT status FROM events WHERE id=?', (event['id'],)).fetchone()
            if not row or row['status'] != 'processing':
                raise ValueError('event is not processing')
            for memory in decision['memories']:
                self.db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)',
                                (self.scope(event), memory['key'], memory['kind'], memory['basis'],
                                 memory['text'], event['id'], now))
            if decision['reply'] is not None:
                self.db.execute('INSERT INTO outbox (id,event_id,conversation,text) VALUES (?,?,?,?)',
                                (str(uuid.uuid4()), event['id'], event['conversation'], decision['reply']))
            self.put('state', decision['state'])
            self.put('goals', decision['goals'])
            self.put('next_wake', now + decision['wake_after_minutes'] * 60)
            self.db.execute('INSERT INTO turns VALUES (?,?,?,?)',
                            (event['id'], json.dumps(decision, ensure_ascii=False), json.dumps(usage), now))
            self.db.execute("UPDATE events SET status='done', completed=?, error=NULL WHERE id=?", (now, event['id']))

    def pending_output(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox WHERE status='queued' ORDER BY rowid")]

    def acknowledge(self, output_id):
        with self.db:
            changed = self.db.execute("UPDATE outbox SET status='delivered', delivered=? WHERE id=? AND status='queued'", (time.time(), output_id)).rowcount
            if not changed:
                raise ValueError('no queued output with that ID')

    def status(self):
        return {'events': {r['status']: r['n'] for r in self.db.execute('SELECT status,COUNT(*) AS n FROM events GROUP BY status')},
                'queued_replies': len(self.pending_output()),
                'memories': self.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0],
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
