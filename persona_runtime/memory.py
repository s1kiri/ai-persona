"""Versioned memories, scoped episodes, a small graph and bounded hybrid retrieval."""
from array import array
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .embedding import MODEL_DIR, get_encoder

SCHEMA = '''
CREATE TABLE IF NOT EXISTS recollections (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
 basis TEXT NOT NULL, text TEXT NOT NULL, source_event TEXT NOT NULL REFERENCES events(id),
 created REAL NOT NULL, valid_to REAL, supersedes TEXT REFERENCES recollections(id),
 entities TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS recollections_current ON recollections(owner,key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS recollections_owner ON recollections(owner,created);
CREATE TABLE IF NOT EXISTS episodes (
 id TEXT PRIMARY KEY REFERENCES events(id), owner TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_owner ON episodes(owner,created);
CREATE TABLE IF NOT EXISTS relations (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL, subject TEXT NOT NULL,
 predicate TEXT NOT NULL, object TEXT NOT NULL, basis TEXT NOT NULL, text TEXT NOT NULL,
 source_event TEXT NOT NULL REFERENCES events(id), created REAL NOT NULL, valid_to REAL,
 supersedes TEXT REFERENCES relations(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS relations_current ON relations(owner,key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS relations_owner ON relations(owner,created);
CREATE TABLE IF NOT EXISTS journal (
 id TEXT PRIMARY KEY, source_event TEXT NOT NULL REFERENCES events(id), ordinal INTEGER NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(source_event,ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(id UNINDEXED, owner UNINDEXED, text);
CREATE TABLE IF NOT EXISTS recall_vectors (
 id TEXT PRIMARY KEY, model TEXT NOT NULL, digest TEXT NOT NULL, vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS retrieval_log (
 event_id TEXT PRIMARY KEY REFERENCES events(id), report TEXT NOT NULL
);
'''
STOP = set('а и в во на с со к у о об от до по из за не да но ну же это как что кто какой какая какие мне меня мы ты вы он она оно они я для the a an is are was to of and or it i you about'.split())


class Budget:
    """cl100k proxy when installed; UTF-8 bytes are a conservative fallback, never a char/4 guess."""
    def __init__(self, limit=4000):
        self.limit = limit
        self.encoding = None
        try:
            import os
            os.environ['TIKTOKEN_CACHE_DIR'] = str(MODEL_DIR / 'tokenizer-cache')
            # Loading only after explicit setup has cached it avoids hidden network requests.
            if any((MODEL_DIR / 'tokenizer-cache').glob('*')):
                import tiktoken
                self.encoding = tiktoken.get_encoding('cl100k_base')
        except (ImportError, OSError):
            pass
        self.name = 'cl100k_base_estimate' if self.encoding else 'utf8_bytes_upper_bound'

    def cost(self, value):
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return len(self.encoding.encode(text, disallowed_special=())) if self.encoding else len(text.encode('utf-8'))


def entity(value):
    return ' '.join(value.casefold().replace('ё', 'е').split())


def episode_chunks(text):
    if len(text) <= 1000:
        return [(None,text)]
    return [(offset,text[offset:offset+900]) for offset in range(0,len(text),750)]


class Memory:
    def __init__(self, store, encoder=None, budget=4000, dense=True):
        self.store, self.db = store, store.db
        self.budget = Budget(budget)
        self.mode = 'hybrid'
        self.encoder = encoder
        self.dense = dense
        self.encoder_checked = encoder is not None
        self.dense_error = None
        self.stemmers = None
        try:
            import snowballstemmer
            self.stemmers = {lang: snowballstemmer.stemmer(lang) for lang in ('russian', 'english')}
        except ImportError:
            pass

    def normalized(self, text):
        tokens = re.findall(r'[^\W_]+', text.casefold().replace('ё', 'е'), re.UNICODE)
        if self.stemmers:
            tokens = [self.stemmers['russian' if re.search('[а-я]', t) else 'english'].stemWord(t)
                      for t in tokens]
        return ' '.join(tokens)

    def index(self, item_id, owner, text):
        self.db.execute('DELETE FROM recall_fts WHERE id=?', (item_id,))
        self.db.execute('INSERT INTO recall_fts(id,owner,text) VALUES (?,?,?)',
                        (item_id, owner, self.normalized(text)))

    def record(self, event, note, now):
        owner = 'self' if event['kind'] == 'wake' or note.get('visibility') == 'self' else 'person:' + event['person']
        previous = self.db.execute('SELECT id FROM recollections WHERE owner=? AND key=? AND valid_to IS NULL',
                                   (owner, note['key'])).fetchone()
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, event['id'] + ':note:' + owner + ':' + note['key']))
        if self.db.execute('SELECT 1 FROM recollections WHERE id=?', (item_id,)).fetchone():
            return
        if previous:
            self.db.execute('UPDATE recollections SET valid_to=? WHERE id=?', (now, previous['id']))
            self.db.execute('DELETE FROM recall_fts WHERE id=?', ('note:' + previous['id'],))
        entities = [entity(v) for v in note.get('entities', [])]
        self.db.execute('INSERT INTO recollections VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (item_id, owner, note['key'], note['kind'], note['basis'], note['text'],
                         event['id'], now, None, previous['id'] if previous else None, json.dumps(entities)))
        self.index('note:' + item_id, owner, note['text'])

    def relation(self, event, value, now):
        owner = 'self' if event['kind'] == 'wake' or value['visibility'] == 'self' else 'person:' + event['person']
        previous = self.db.execute('SELECT id FROM relations WHERE owner=? AND key=? AND valid_to IS NULL',
                                   (owner, value['key'])).fetchone()
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, event['id'] + ':edge:' + owner + ':' + value['key']))
        if self.db.execute('SELECT 1 FROM relations WHERE id=?', (item_id,)).fetchone():
            return
        if previous:
            self.db.execute('UPDATE relations SET valid_to=? WHERE id=?', (now, previous['id']))
            self.db.execute('DELETE FROM recall_fts WHERE id=?', ('edge:' + previous['id'],))
        self.db.execute('INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (item_id, owner, value['key'], entity(value['subject']), entity(value['predicate']),
                         entity(value['object']), value['basis'], value['text'], event['id'], now,
                         None, previous['id'] if previous else None))
        self.index('edge:' + item_id, owner, value['text'])

    def episode(self, event, reply=None):
        if event['kind'] != 'message':
            return
        owner = 'person:' + event['person']
        text = 'Audience message: ' + event['text']
        if reply:
            text += '\nConfirmed delivered Garik reply: ' + reply
        self.db.execute('INSERT OR REPLACE INTO episodes VALUES (?,?,?,?)',
                        (event['id'], owner, text, event['created']))
        prefix = 'episode:' + event['id']
        self.db.execute('DELETE FROM recall_fts WHERE id=? OR substr(id,1,?)=?',
                        (prefix,len(prefix)+1,prefix+':'))
        for offset,chunk in episode_chunks(text):
            self.index(prefix + (':' + str(offset) if offset is not None else ''),owner,chunk)

    def journal(self, event, entries, now):
        for ordinal, entry in enumerate(entries):
            entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, event['id'] + ':journal:' + str(ordinal)))
            self.db.execute('INSERT INTO journal VALUES (?,?,?,?,?,?,?)',
                            (entry_id, event['id'], ordinal, entry['kind'], entry['title'], entry['text'], now))
            self.index('journal:' + entry_id, 'self', entry['title'] + '\n' + entry['text'])
            # Receipt is host-authored; an invented scene in the entry is not a physical observation.
            self.record(event, {'key': 'journal:' + entry_id, 'kind': 'event', 'basis': 'observed',
                               'visibility': 'self', 'entities': ['journal'],
                               'text': 'Host confirmed a saved personal journal entry: ' + entry['title'] +
                               ' (kind=' + entry['kind'] + ', id=' + entry_id + ').'}, now)

    def owners(self, event):
        return ('self',) if event['kind'] == 'wake' else ('self', 'person:' + event['person'])

    def documents(self, event):
        owners = self.owners(event)
        marks = ','.join('?' for _ in owners)
        result = {}
        queries = (
            ('note', 'recollections', 'valid_to IS NULL'),
            ('edge', 'relations', 'valid_to IS NULL'),
            ('episode', 'episodes', '1=1'),
        )
        for prefix, table, condition in queries:
            for row in self.db.execute('SELECT * FROM ' + table + ' WHERE owner IN (' + marks + ') AND ' + condition, owners):
                value = dict(row)
                value['record_type'] = prefix
                if prefix == 'note':
                    value['entities'] = json.loads(value['entities'])
                elif prefix == 'edge':
                    value['entities'] = [value['subject'], value['object']]
                else:
                    value.update(entities=[], basis='reported', source_event=value['id'])
                    value['historical_dialogue'] = True
                    for offset,chunk in episode_chunks(value['text']):
                        result[prefix + ':' + value['id'] + (':' + str(offset) if offset is not None else '')] = {**value,'text':chunk}
                    continue
                result[prefix + ':' + value['id']] = value
        # Journal content is explicitly authored for Garik's own life, never audience transcripts.
        for row in self.db.execute('SELECT * FROM journal'):
            value = dict(row)
            value.update(record_type='journal', owner='self', entities=['journal'], basis='fiction' if value['kind'] == 'story' else 'reported')
            value['text'] = value['title'] + '\n' + value['text']
            result['journal:' + value['id']] = value
        return result

    def ensure_encoder(self):
        if self.dense and not self.encoder_checked:
            self.encoder_checked = True
            try:
                self.encoder = get_encoder()
            except (ImportError, RuntimeError, OSError) as exc:
                self.dense_error = str(exc)
        return self.encoder

    def vectors(self, docs):
        encoder = self.ensure_encoder()
        if encoder is None or not docs:
            return {}
        result, missing = {}, []
        for key, value in docs.items():
            digest = hashlib.sha256(value['text'].encode()).hexdigest()
            row = self.db.execute('SELECT * FROM recall_vectors WHERE id=?', (key,)).fetchone()
            if row and row['model'] == encoder.model_id and row['digest'] == digest:
                result[key] = array('f', row['vector'])
            else:
                missing.append((key, value['text'], digest))
        # This cache is derived data, never part of the character decision transaction.
        if missing:
            encoded = encoder.encode([v[1] for v in missing])
            with self.db:
                for (key, text, digest), vector in zip(missing, encoded):
                    vector = array('f', vector)
                    self.db.execute('INSERT OR REPLACE INTO recall_vectors VALUES (?,?,?,?)',
                                    (key, encoder.model_id, digest, vector.tobytes()))
                    result[key] = vector
        return result

    def search(self, event, query, mode='hybrid', limit=12):
        if mode not in ('recent', 'bm25', 'dense', 'hybrid'):
            raise ValueError('unknown retrieval mode')
        started = time.monotonic()
        docs = self.documents(event)
        terms = [t for t in self.normalized(query).split() if t not in STOP and len(t) > 1][:40]
        lexical = []
        if terms and mode in ('bm25', 'hybrid'):
            expression = ' OR '.join('"' + term + '"' for term in dict.fromkeys(terms))
            # Search only authorized owners, then intersect active IDs. Old versions stay archived.
            owners = self.owners(event)
            marks = ','.join('?' for _ in owners)
            rows = self.db.execute('SELECT id,bm25(recall_fts) AS score FROM recall_fts WHERE recall_fts MATCH ? AND owner IN (' + marks + ') ORDER BY score LIMIT 200', (expression,) + owners)
            lexical = [r['id'] for r in rows if r['id'] in docs][:40]
        dense = []
        if terms and mode in ('dense', 'hybrid'):
            vectors = self.vectors(docs)
            if vectors:
                q = self.encoder.encode([query], query=True)[0]
                # Exact cosine is deliberately adequate for the first local corpus; no ANN server.
                dense = sorted(((sum(a*b for a,b in zip(q, vector)), key) for key, vector in vectors.items()), reverse=True)
                dense = [key for score, key in dense[:40] if score >= 0.72]
        if mode == 'recent':
            ranked = sorted(docs, key=lambda key: (docs[key]['created'], key), reverse=True)
        else:
            scores = {}
            for ranking in (lexical, dense):
                for rank, key in enumerate(ranking):
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank + 1)
            ranked = sorted(scores, key=lambda key: (scores[key], docs[key]['created'], key), reverse=True)
        selected = ranked[:limit]
        # One bounded graph hop; entity IDs are scoped by document ownership during expansion.
        seeds = {(docs[k]['owner'], e) for k in selected[:4] for e in docs[k]['entities']}
        neighbours = [k for k,v in docs.items() if k not in selected and v['record_type'] in ('note','edge')
                      and any((v['owner'], e) in seeds for e in v['entities'])]
        if mode == 'hybrid':
            selected += sorted(neighbours, key=lambda k: docs[k]['created'], reverse=True)[:4]
        report = {'mode': mode, 'dense_available': self.encoder is not None,
                  'dense_error': self.dense_error, 'authorized_documents': len(docs),
                  'lexical_hits': len(lexical), 'dense_hits': len(dense), 'graph_added': max(0,len(selected)-min(limit,len(ranked))),
                  'elapsed_ms': round((time.monotonic()-started)*1000, 2)}
        return [(k, docs[k]) for k in selected], report

    @staticmethod
    def public_record(key, value):
        fields = ('text','basis','source_event','created','kind','key','subject','predicate','object','historical_dialogue')
        return {'id': key, 'type': value['record_type'], 'visibility': 'self' if value['owner'] == 'self' else 'person',
                **{k:value[k] for k in fields if k in value}}

    def context(self, event, query=None, mode=None, extra=None):
        mode = mode or self.mode
        rows = list(self.db.execute('''SELECT e.id,e.text,o.text AS reply FROM events e
            LEFT JOIN outbox o ON o.event_id=e.id AND o.status='delivered'
            WHERE e.conversation=? AND e.person=? AND e.kind='message' AND e.status='done'
            ORDER BY e.created DESC,e.rowid DESC LIMIT 6''', (event['conversation'],event['person'])))
        history = [dict(row) for row in reversed(rows)]
        # The nearest two user messages resolve references like "what colour was it?".
        search_query = query or '\n'.join([r['text'] for r in history[-2:]] + [event['text']])
        matches, report = self.search(event, search_query, mode=mode)
        current = self.documents(event)
        personal = sorted(((k,v) for k,v in current.items() if v['record_type']=='note'
                           and v['owner']=='person:'+event['person'] and v['kind']=='person'),
                          key=lambda item:item[1]['created'], reverse=True)[:3]
        # Fixed core is small; large history entries cannot evict all relevant memories.
        context = {'state': self.store.get('state')[:1800], 'goals': self.store.get('goals')[:1800],
                   'memories': [], 'history': [], 'journal_receipts': [],
                   'retrieval': {'mode':mode,'counter':self.budget.name,'budget':self.budget.limit}}
        while self.budget.cost(context) > self.budget.limit and (context['state'] or context['goals']):
            context['state'] = context['state'][:len(context['state'])//2]
            context['goals'] = context['goals'][:len(context['goals'])//2]
        candidates = personal + matches + (extra or [])
        seen = set()
        content_seen = set()
        source_counts = {}
        for key,value in candidates:
            if key in seen:
                continue
            seen.add(key)
            record = self.public_record(key,value)
            fingerprint = self.normalized(record['text'])
            source = record['source_event']
            if fingerprint in content_seen or source_counts.get(source,0) >= 2:
                continue
            # Every selected item remains source-linked. Oversized episodes get a labelled excerpt.
            if len(record['text']) > 1800:
                record['text'] = record['text'][:1200] + '\n[excerpt; full source retained]\n' + record['text'][-400:]
            context['memories'].append(record)
            if self.budget.cost(context) > self.budget.limit - 700:
                context['memories'].pop()
            else:
                content_seen.add(fingerprint)
                source_counts[source] = source_counts.get(source,0) + 1
        for entry in reversed(history):
            entry = {k:(v[:1000] + ' [excerpt]' if isinstance(v,str) and len(v)>1000 else v) for k,v in entry.items()}
            context['history'].insert(0,entry)
            if self.budget.cost(context) > self.budget.limit - 200:
                context['history'].pop(0)
        for row in self.db.execute('SELECT id,title,kind,created FROM journal ORDER BY created DESC LIMIT 2'):
            context['journal_receipts'].append(dict(row))
            if self.budget.cost(context) > self.budget.limit - 100:
                context['journal_receipts'].pop()
        context['retrieval']['used'] = self.budget.cost(context) + 20
        if self.budget.cost(context) > self.budget.limit:
            raise ValueError('memory context budget exceeded')
        report.update(context['retrieval'], selected_ids=[r['id'] for r in context['memories']])
        return context, report

    def migrate(self):
        self.db.executescript(SCHEMA)
        if self.store.get('memory_version') == '2':
            return
        with self.db:
            # Replay archived decisions so updates retain their lineage and invalidation time.
            for row in self.db.execute('SELECT e.*,t.decision,t.completed AS turn_time FROM events e JOIN turns t ON t.event_id=e.id ORDER BY t.completed,e.rowid').fetchall():
                event = dict(row)
                decision = json.loads(row['decision'])
                for note in decision.get('memories',[]):
                    self.record(event,note,row['turn_time'])
                reply = self.db.execute("SELECT text FROM outbox WHERE event_id=? AND status='delivered'", (event['id'],)).fetchone()
                self.episode(event, reply['text'] if reply else None)
            self.store.put('memory_version','2')

    def export_journal(self):
        directory = self.store.directory / 'journal'
        entries = self.db.execute('SELECT * FROM journal ORDER BY created,id').fetchall()
        if not entries:
            return
        directory.mkdir(mode=0o700,exist_ok=True)
        for row in entries:
            path = directory / (row['id'] + '.md')
            body = '# ' + row['title'] + '\n\nKind: ' + row['kind'] + '\nSource event: ' + row['source_event'] + '\n\n' + row['text'] + '\n'
            if path.exists() and path.read_text(encoding='utf-8') == body:
                continue
            temporary = path.with_suffix('.tmp')
            temporary.write_text(body,encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(path)
