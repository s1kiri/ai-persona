"""Actor-owned generated photographs, separate from new render jobs and delivery."""
import hashlib
import json
import os
from pathlib import Path
import re


class Gallery:
    def __init__(self, store):
        self.store = store
        self.assets = store.directory / 'gallery' / 'assets'
        self.assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.lock, store.db:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS gallery_images(
                    actor_id TEXT NOT NULL,id TEXT NOT NULL,sha256 TEXT NOT NULL,
                    image_path TEXT NOT NULL,subject_ids TEXT NOT NULL,source_ids TEXT NOT NULL,
                    view TEXT NOT NULL,caption TEXT NOT NULL,minute INTEGER NOT NULL,
                    PRIMARY KEY(actor_id,id));
                CREATE TABLE IF NOT EXISTS gallery_deliveries(
                    actor_id TEXT NOT NULL,image_id TEXT NOT NULL,post_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                    PRIMARY KEY(actor_id,image_id,chat_id,message_id));
                CREATE TABLE IF NOT EXISTS gallery_generations(
                    actor_id TEXT NOT NULL,post_id TEXT NOT NULL,image_id TEXT NOT NULL,
                    PRIMARY KEY(actor_id,post_id));
            ''')

    def register(self, image, actor, view, sources=(), caption='', minute=None, new_job=None):
        path = Path(image['image_path']).resolve(strict=True)
        if not path.is_relative_to(self.store.directory):
            raise ValueError('gallery import needs an asset belonging to this world')
        raw = path.read_bytes()
        if len(raw) > 10_000_000 or not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'):
            raise ValueError('gallery needs a bounded PNG, JPEG or WebP image')
        digest = hashlib.sha256(raw).hexdigest()
        if digest != image['sha256']:
            raise ValueError('gallery asset differs from its receipt')
        if not image['subject_ids'] or not view.strip():
            raise ValueError('gallery photo needs subjects and a view')
        suffix = '.png' if raw.startswith(b'\x89PNG') else '.jpg' if raw.startswith(b'\xff\xd8') else '.webp'
        target = self.assets / (digest + suffix)
        if not target.exists():
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('gallery copy is damaged')
        identity = 'photo-' + digest
        with self.store.lock, self.store.db:
            if new_job:
                prior=self.store.db.execute('SELECT image_id FROM gallery_generations WHERE actor_id=? AND post_id=?',(actor,new_job)).fetchone()
                exists=self.store.db.execute('SELECT 1 FROM gallery_images WHERE actor_id=? AND id=?',(actor,identity)).fetchone()
                if prior and prior[0]!=identity or exists and (not prior or prior[0]!=identity):
                    raise ValueError('new photograph already exists in gallery; explicit existing selection required')
            self.store.db.execute('INSERT OR IGNORE INTO gallery_images VALUES(?,?,?,?,?,?,?,?,?)',
                (actor,identity,digest,str(target.resolve()),json.dumps(image['subject_ids']),json.dumps(list(sources)),
                 view[:700],caption[:700],self.store.get('minute') if minute is None else minute))
            if new_job:
                self.store.db.execute('INSERT OR IGNORE INTO gallery_generations VALUES(?,?,?)',(actor,new_job,identity))
        return identity

    def get(self, actor, identity, known_subjects):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM gallery_images WHERE actor_id=? AND id=?',(actor,identity)).fetchone()
            if not row:
                raise ValueError('gallery photo does not belong to this actor')
            item = dict(row)
            item['subject_ids'] = json.loads(item['subject_ids'])
            item['source_ids'] = json.loads(item['source_ids'])
            if not set(item['subject_ids']) <= set(known_subjects):
                raise ValueError('gallery photo contains an unknown subject')
            path = Path(item['image_path']).resolve(strict=True)
            if not path.is_relative_to(self.assets.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                raise ValueError('gallery photo is missing or damaged')
            return item

    def catalog(self, actor, known_subjects, query='', limit=24):
        terms = re.findall(r'\w{4,}',query.casefold())[:12]
        with self.store.lock:
            rows = [dict(r) for r in self.store.db.execute('SELECT * FROM gallery_images WHERE actor_id=? ORDER BY minute DESC,rowid DESC',(actor,))]
            result=[]
            for item in rows:
                subjects=json.loads(item['subject_ids'])
                if not set(subjects)<=set(known_subjects) or not Path(item['image_path']).is_file():continue
                shared=self.store.db.execute('SELECT post_id,chat_id,message_id FROM gallery_deliveries WHERE actor_id=? AND image_id=? ORDER BY rowid DESC LIMIT 4',(actor,item['id'])).fetchall()
                result.append({'id':item['id'],'subject_ids':subjects,'view':item['view'],'caption':item['caption'],
                               'minute':item['minute'],'shared':[dict(r) for r in shared]})
        def relevance(item):
            text=(item['view']+' '+item['caption']+' '+' '.join(item['subject_ids'])).casefold()
            return sum(term in text for term in terms)
        return sorted(result,key=relevance,reverse=True)[:limit]

    def delivered(self, actor, identity, post_id, receipt):
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO gallery_deliveries VALUES(?,?,?,?,?)',
                (actor,identity,post_id,receipt['chat_id'],receipt['message_id']))

    def backfill(self):
        """Import only this world's already delivered image receipts, never test folders."""
        with self.store.lock:
            rows=self.store.db.execute("SELECT id,actor_id,minute,value FROM world_events WHERE stage='post'").fetchall()
        for row in rows:
            value=json.loads(row['value'])
            if not value.get('photo') or value.get('delivery',{}).get('status')!='sent':continue
            image=value['photo']
            with self.store.lock:
                job=self.store.db.execute('SELECT photo FROM channel_jobs WHERE post_id=?',(row['id'],)).fetchone()
            choice=json.loads(job[0]).get('choice',{}) if job and job[0] else {}
            identity=self.register(image,row['actor_id'],choice.get('framing') or 'Previously shared photograph',
                                   choice.get('source_ids',value.get('source_ids',[])),value['text'],row['minute'])
            receipt=value['delivery']['receipts'][-1]
            self.delivered(row['actor_id'],identity,row['id'],receipt)
