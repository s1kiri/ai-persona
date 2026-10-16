"""Actor-scoped historical recall for life and publication, with local embeddings."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from .embedding import MODEL_REVISION
from .memory import STOP


def encode_local(texts, query):
    # The live host may use Apple's Python without ONNX packages. Keep native
    # dependencies in the already configured venv and out of the daemon.
    root = Path(__file__).resolve().parent.parent
    python = root/'.venv/bin/python'
    result = subprocess.run([str(python), '-m', 'persona_runtime.channel_recall', '--encode'],
                            input=json.dumps({'texts': texts, 'query': query}), text=True,
                            capture_output=True, timeout=45, cwd=root)
    if result.returncode:
        raise RuntimeError('local embedding worker unavailable')
    return json.loads(result.stdout)


def terms(text):
    return [w for w in re.findall(r'[\w]+', text.casefold().replace('ё', 'е')) if len(w)>2 and w not in STOP]


class ChannelRecall:
    def __init__(self, store, encoder=encode_local):
        self.store, self.encoder = store, encoder
        self.report = {}
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS channel_recall_vectors(
                actor_id TEXT NOT NULL,event_id TEXT NOT NULL,digest TEXT NOT NULL,
                revision TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(actor_id,event_id))''')

    def documents(self, actor, publications_only=False, *, at_minute=None):
        docs = []
        with self.store.lock:
            rows = self.store.db.execute('SELECT id,actor_id,stage,minute,value FROM world_events ORDER BY rowid').fetchall()
        for row in rows:
            if at_minute is not None and row['minute']>at_minute:continue
            value = json.loads(row['value']); stage = row['stage']; text = ''
            if stage == 'post' and row['actor_id']==actor:
                if value.get('retracted') or value.get('delivery',{}).get('status')!='sent':continue
                if publications_only and value.get('reply_to'):continue
                text = value['text']
            elif publications_only:continue
            elif stage in ('world','outcome') and actor in value.get('visible_to',[row['actor_id']]):
                text = value['observation']
            elif row['actor_id']==actor:
                if stage=='experience':text=value['note']
                elif stage=='research':text=json.dumps(value.get('facts',[]),ensure_ascii=False)
                elif stage=='audience':text='\n'.join(c['speaker']+': '+c['text'] for c in value['comments'])
            if text:
                docs.append({'id':row['id'],'minute':row['minute'],'stage':stage,'text':text,
                             'has_photo':bool(value.get('photo'))})
        return docs

    def recent(self, actor, *, at_minute=None):
        cutoff = (self.store.get('minute') if at_minute is None else at_minute)-1440
        return [{**d,'text':d['text'][:220]} for d in self.documents(actor,True,at_minute=at_minute) if d['minute']>=cutoff][-24:]

    def search(self, actor, query, *, publications_only=False, limit=6, at_minute=None):
        docs = self.documents(actor,publications_only,at_minute=at_minute)
        if not docs or not query.strip():return []
        qterms = set(terms(query)[:60]); tokenized = [Counter(terms(d['text'])) for d in docs]
        avg = max(1,sum(sum(t.values()) for t in tokenized)/len(docs))
        lexical = []
        for index, counts in enumerate(tokenized):
            score = 0
            for word in qterms & counts.keys():
                df = sum(word in c for c in tokenized)
                tf = counts[word]
                score += math.log(1+(len(docs)-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*sum(counts.values())/avg))
            if score:lexical.append((score,index))
        lexical.sort(reverse=True)
        dense = []; error = None
        try:
            vectors = {}; missing = []
            with self.store.lock:
                cached = {r['event_id']:dict(r) for r in self.store.db.execute('SELECT * FROM channel_recall_vectors WHERE actor_id=?',(actor,))}
            for index,d in enumerate(docs):
                digest = hashlib.sha256(d['text'].encode()).hexdigest(); row = cached.get(d['id'])
                if row and row['digest']==digest and row['revision']==MODEL_REVISION:
                    vectors[index] = json.loads(row['vector'])
                else:missing.append((index,d,digest))
            encoded = self.encoder([d['text'] for _,d,_ in missing],query)
            if len(encoded['vectors'])!=len(missing):raise ValueError('embedding count mismatch')
            with self.store.lock, self.store.db:
                for (index,d,digest),vector in zip(missing,encoded['vectors']):
                    vectors[index] = vector
                    self.store.db.execute('INSERT OR REPLACE INTO channel_recall_vectors VALUES(?,?,?,?,?)',
                        (actor,d['id'],digest,MODEL_REVISION,json.dumps(vector)))
            qvector = encoded['query']
            for index,vector in vectors.items():
                if len(vector)!=len(qvector):raise ValueError('embedding dimension mismatch')
                score = sum(a*b for a,b in zip(vector,qvector))
                if score>=.78:dense.append((score,index))
            dense.sort(reverse=True)
        except (OSError,subprocess.SubprocessError,ValueError,RuntimeError,KeyError):
            error = 'local_encoder_unavailable'
        scores = {}
        for ranking in (lexical,dense):
            for rank,(_,index) in enumerate(ranking[:40]):scores[index]=scores.get(index,0)+1/(61+rank)
        ranked = sorted(scores,key=lambda i:(scores[i],docs[i]['minute']),reverse=True)[:limit]
        dense_scores = {i:s for s,i in dense}
        self.report = {'mode':'hybrid' if error is None else 'lexical_fallback','error':error,
                       'documents':len(docs),'lexical_hits':len(lexical),'semantic_hits':len(dense)}
        return [{**docs[i],'text':docs[i]['text'][:900], 'semantic_similarity':dense_scores.get(i)} for i in ranked]


if __name__ == '__main__':
    if sys.argv[1:] != ['--encode']:raise SystemExit(2)
    from .embedding import get_encoder
    request = json.load(sys.stdin); encoder = get_encoder()
    print(json.dumps({'vectors':encoder.encode(request['texts']),
                      'query':encoder.encode([request['query']],query=True)[0],'model':encoder.model_id}))
