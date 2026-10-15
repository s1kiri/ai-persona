"""Visual memory by persistent world identity, separate from render-job replay."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3


class MediaReferenceLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory/'references.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS references_by_entity(
            actor_id TEXT NOT NULL, sha256 TEXT NOT NULL, image_path TEXT NOT NULL,
            subject_ids TEXT NOT NULL, view TEXT NOT NULL, visual_notes TEXT NOT NULL,
            source TEXT NOT NULL, channel_post_id TEXT,
            PRIMARY KEY(actor_id,sha256))''')

    def close(self): self.db.close()

    def register(self, image, actor_id, view, *, source, visual_notes='', channel_post_id=None):
        # Draft generation alone is not permission to establish a reference.
        # The delivery host registers channel images only after its receipt;
        # an author-approved reference may also be registered explicitly.
        if source not in ('approved', 'channel') or (source == 'channel' and not channel_post_id):
            raise ValueError('reference needs author approval or a channel post receipt')
        subjects = image['subject_ids']
        if not actor_id or not subjects or not view.strip():
            raise ValueError('reference needs actor, stable subject identities and view')
        path = Path(image['image_path']).resolve(strict=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != image['sha256']:
            raise ValueError('reference image changed since its receipt')
        assets = self.directory/'assets'
        assets.mkdir(exist_ok=True, mode=0o700)
        stored = assets/(digest+path.suffix.lower())
        if not stored.exists():
            fd = os.open(stored, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream: stream.write(path.read_bytes())
        if hashlib.sha256(stored.read_bytes()).hexdigest() != digest:
            raise ValueError('stored visual reference is damaged')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO references_by_entity VALUES(?,?,?,?,?,?,?,?)',
                            (actor_id, digest, str(stored.resolve()), json.dumps(subjects), view, visual_notes, source, channel_post_id))
        return digest

    def select(self, actor_id, subject_ids, limit=3):
        if not 1 <= limit <= 5: raise ValueError('invalid reference limit')
        wanted, chosen, views = set(subject_ids), [], set()
        rows = self.db.execute('SELECT * FROM references_by_entity WHERE actor_id=? ORDER BY rowid DESC', (actor_id,))
        for row in rows:
            entry = dict(row); entry['subject_ids'] = json.loads(entry['subject_ids'])
            # Do not leak a co-depicted but currently unknown subject through
            # an old multi-subject photograph.
            if not set(entry['subject_ids']) <= wanted or entry['view'] in views:
                continue
            path = Path(entry['image_path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                continue
            views.add(entry['view']); chosen.append(entry)
            if len(chosen) == limit: break
        return chosen


def with_visual_references(request, entries):
    if not entries: return request
    # Reference images establish the same object/place; framing defines a new
    # photo. This is deliberately different from re-delivering the same job.
    brief = [{k: e[k] for k in ('subject_ids', 'view', 'visual_notes')} for e in entries]
    return {**request, 'visual_references': brief,
            'prompt': request['prompt']+'''
REFERENCE ROLE: the attached photographs show previous views of these SAME
world objects/places. Preserve their identity, materials, colors, signs and
stable spatial details. They are not the new photograph to return or copy.
Create a genuinely new camera shot following the requested framing and current
scene state. Do not duplicate the reference's composition merely because it
was supplied. An exterior reference does not require an exterior shot when
the new request is for the interior. Use any relevant interior/exterior views
together for continuity. No collage or pasted reference image.
Previously shown views (descriptions, not new scene actions):
'''+json.dumps(brief, ensure_ascii=False)}


def ensure_new_photo(image, reference_paths):
    for reference in reference_paths:
        path = Path(reference)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == image['sha256']:
            raise ValueError('new photo is identical to an old reference; no silent reuse')
