"""An opt-in fictional world, isolated from the running character database.

One shared timeline, actor-scoped perceptions and notes. No model is called by
opening the store, moving a slider or preparing a cycle. Each model stage is
recorded before dispatch and never retried automatically after interruption.
"""
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
import uuid

from .world_protocol import BEATS, DOMAINS, identifier, validate_result

DEFAULT_SETTINGS = {'events_per_hour': 2.0, 'surprise': 45, 'continuity': 70,
                    'quiet': 30, 'max_threads': 4}


def settings_checked(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError('invalid world controls')
    for key, lower, upper in [('events_per_hour', 0, 6), ('surprise', 0, 100),
                              ('continuity', 0, 100), ('quiet', 0, 100), ('max_threads', 1, 8)]:
        number = value[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError('invalid world control: ' + key)
        if key != 'events_per_hour' and type(number) is not int:
            raise ValueError('integer control required: ' + key)
    return dict(value)


def director(state, actor_id, elapsed, draw):
    """Pace new incidents; returning causes are independent of novelty frequency."""
    settings = state['settings']
    history = state['history']
    recent = [e for e in history if e['stage'] == 'world' and e['value']['beat'] != 'quiet'][-8:]
    due = [t for t in state['threads'] if t['status'] != 'closed' and
           t['due_minute'] is not None and t['due_minute'] <= state['minute']]
    probability = -math.expm1(-settings['events_per_hour'] * elapsed / 60)
    novel = draw < probability
    allowed = list(BEATS) if novel else ['consequence', 'return', 'progress', 'closure', 'quiet']
    causes = [e['id'] for e in history]
    if not causes:
        allowed = [beat for beat in allowed if beat not in ('consequence', 'return', 'closure')]
    domains = list(DOMAINS)
    last = [e['value']['domain'] for e in recent[-3:]]
    if len(last) == 3 and len(set(last)) == 1 and not any(t['domain'] == last[0] for t in due):
        domains.remove(last[0])
    weights = {beat: 1.0 for beat in allowed}
    for event in recent[-4:]:
        beat = event['value']['beat']
        if beat in weights:
            weights[beat] *= .45
    for beat in ('consequence', 'return', 'progress', 'closure'):
        if beat in weights:
            weights[beat] *= 1 + settings['continuity'] / 25 + bool(due) * 2
    weights['quiet'] *= 1 + settings['quiet'] / 15
    quiet_streak = 0
    for e in reversed([e for e in history if e['stage'] == 'world']):
        if e['value']['beat'] != 'quiet':
            break
        quiet_streak += 1
    # An available arrival should eventually change the circumstances. This
    # never manufactures an arrival when frequency is zero or the gate is shut.
    if novel and settings['quiet'] < 100 and quiet_streak >= 2 + settings['quiet'] // 20:
        allowed.remove('quiet')
        weights.pop('quiet')
    return {'actor_id': actor_id, 'minute': state['minute'], 'settings': settings,
            'new_event_allowed': novel, 'new_event_probability': round(probability, 4),
            'allowed_beats': allowed, 'allowed_domains': domains, 'beat_weights': weights,
            'recent_patterns': [e['value']['pattern'] for e in recent[-6:]],
            'due_threads': [t['id'] for t in due], 'cause_ids': causes,
            'surprise_ceiling': settings['surprise'],
            'open_slots': max(0, settings['max_threads'] - sum(t['status'] != 'closed' for t in state['threads']))}


class WorldConflict(RuntimeError):
    pass


class WorldStore:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        # Cannot accidentally attach the laboratory to the production Store.
        if any((self.directory / name).exists() for name in ('runtime.sqlite3', 'daemon.json')):
            raise ValueError('world laboratory requires a separate directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = os.open(self.directory / 'world.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.owner)
            raise WorldConflict('world laboratory already has an owner') from None
        self.path = self.directory / 'world.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world_actors(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world_entities(id TEXT PRIMARY KEY,value TEXT NOT NULL,source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world_threads(id TEXT PRIMARY KEY,value TEXT NOT NULL,source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world_cycles(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES world_actors(id),
                status TEXT NOT NULL,phase TEXT NOT NULL,revision INTEGER NOT NULL,value TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS world_events(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL REFERENCES world_cycles(id),
                actor_id TEXT NOT NULL,stage TEXT NOT NULL,minute INTEGER NOT NULL,value TEXT NOT NULL,
                UNIQUE(cycle_id,stage));
            CREATE TABLE IF NOT EXISTS world_calls(id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL REFERENCES world_cycles(id),
                stage TEXT NOT NULL,status TEXT NOT NULL,usage TEXT NOT NULL,created REAL NOT NULL,error TEXT);
            CREATE TABLE IF NOT EXISTS world_notes(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES world_actors(id),
                cycle_id TEXT NOT NULL REFERENCES world_cycles(id),minute INTEGER NOT NULL,text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS world_snapshots(revision INTEGER PRIMARY KEY,value TEXT NOT NULL);
        ''')
        with self.lock, self.db:
            if self.get('version') is None:
                self.put('version', 1)
                self.put('revision', 0)
                self.put('minute', 0)
                self.put('settings', DEFAULT_SETTINGS)
                self.put('settings_revision', 0)
                self.put('paused', True)
                self.put('basis', {'garik': 'Гарик, Гар, Гарыч; на Орте Гарр. 3427 земных лет, 2791 год на Земле. Долгоживущий, не неуязвимый и не всеведущий. Орта сухая и каменистая. Живёт в крупном городе России, город не выбран. Снимает квартиру над круглосуточной шаурмичной. Ест человеческую еду, мягкий известняк и мел, гранит слишком твёрдый. Спит раз в семнадцать дней по восемнадцать часов. Происхождению и возрасту обычно не верят. Его любимый пёс встречался ему однажды; имени и судьбы не знает.'})
                self.register_actor('garik', 'Гарик')
                for item in [
                    {'id': 'home', 'kind': 'place', 'name': 'Съёмная квартира',
                     'description': 'Квартира над круглосуточной шаурмичной в крупном городе России. Город не выбран.',
                     'condition': 'Обычная квартира.', 'known_to': ['garik'], 'world_note': ''},
                    {'id': 'shawarma', 'kind': 'place', 'name': 'Шаурмичная внизу',
                     'description': 'Обычная круглосуточная шаурмичная под квартирой.',
                     'condition': 'Работает.', 'known_to': ['garik'], 'world_note': ''}]:
                    self.db.execute('INSERT INTO world_entities VALUES(?,?,?)', (item['id'], json.dumps(item, ensure_ascii=False), 'author-basis'))
                self.snapshot()
            elif self.get('version') != 1:
                raise ValueError('unknown world database version')
            # Ambiguous in-flight calls never replay merely because the UI reopened.
            self.db.execute("UPDATE world_calls SET status='interrupted',error='process stopped; no automatic retry' WHERE status='started'")
            self.db.execute("UPDATE world_cycles SET status='blocked' WHERE status='running'")

    def close(self):
        with self.lock:
            self.db.close()
            os.close(self.owner)

    def get(self, key):
        row = self.db.execute('SELECT value FROM world_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO world_meta VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def register_actor(self, actor_id, name):
        """Host operation only. A second persona is not a model-created NPC."""
        identifier(actor_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('invalid actor name')
        actor = self.db.execute('SELECT name FROM world_actors WHERE id=?', (actor_id,)).fetchone()
        if actor and actor[0] != name:
            raise WorldConflict('actor identity already exists with another name')
        if not actor and self.db.execute('SELECT 1 FROM world_entities WHERE id=?', (actor_id,)).fetchone():
            raise WorldConflict('actor identity collides with a world entity')
        self.db.execute('INSERT OR IGNORE INTO world_actors VALUES(?,?)', (actor_id, name))
        entity = {'id': actor_id, 'kind': 'person', 'name': name,
                  'description': 'Самостоятельный участник мира; его личность и решения задаются отдельно.',
                  'condition': 'В мире.', 'known_to': [actor_id], 'world_note': ''}
        self.db.execute('INSERT OR IGNORE INTO world_entities VALUES(?,?,?)',
                        (actor_id, json.dumps(entity, ensure_ascii=False), 'actor-registry'))

    def _rows(self, table, limit=None):
        query = 'SELECT * FROM ' + table
        if table == 'world_events':
            query += ' ORDER BY rowid DESC LIMIT ' + str(limit or 32)
        return [dict(row) for row in self.db.execute(query)]

    def state(self):
        with self.lock:
            entities = [json.loads(r['value']) for r in self._rows('world_entities')]
            threads = [json.loads(r['value']) for r in self._rows('world_threads')]
            history = [{**r, 'value': json.loads(r['value'])} for r in reversed(self._rows('world_events'))]
            return {'revision': self.get('revision'), 'minute': self.get('minute'),
                    'basis': self.get('basis'),
                    'settings': self.get('settings'), 'settings_revision': self.get('settings_revision'),
                    'paused': self.get('paused'), 'actors': self._rows('world_actors'),
                    'entities': entities, 'threads': threads, 'history': history}

    def actor_view(self, actor_id, state=None):
        state = copy.deepcopy(state or self.state())
        state['entities'] = [{k: v for k, v in e.items() if k != 'world_note'}
                             for e in state['entities'] if actor_id in e['known_to']]
        state['threads'] = [t for t in state['threads'] if actor_id in t['visible_to']]
        history = []
        for e in state['history']:
            visible = e['value'].get('visible_to', [e['actor_id']])
            if actor_id not in visible:
                continue
            if e['stage'] in ('world', 'outcome'):
                history.append({'id': e['id'], 'minute': e['minute'], 'stage': e['stage'],
                                'observation': e['value']['observation']})
            elif e['actor_id'] == actor_id and e['stage'] == 'choice':
                history.append({'id': e['id'], 'minute': e['minute'], 'stage': 'choice', 'value': e['value']})
        state['history'] = history[-16:]
        state['notes'] = [dict(r) for r in self.db.execute(
            'SELECT minute,text FROM world_notes WHERE actor_id=? ORDER BY rowid DESC LIMIT 8', (actor_id,))]
        state['basis'] = {actor_id: state.get('basis', {}).get(actor_id, '')}
        visible_ids = {e['id'] for e in state['entities']}
        state['actors'] = [a for a in state['actors'] if a['id'] in visible_ids]
        for key in ('settings', 'settings_revision', 'paused', 'revision'):
            state.pop(key, None)
        return state

    def snapshot(self):
        state = self.state()
        self.db.execute('INSERT OR REPLACE INTO world_snapshots VALUES(?,?)',
                        (state['revision'], json.dumps(state, ensure_ascii=False)))

    def set_settings(self, settings, expected):
        settings = settings_checked(settings)
        if type(expected) is not int:
            raise ValueError('invalid controls revision')
        with self.lock, self.db:
            if expected != self.get('settings_revision'):
                raise WorldConflict('controls changed in another window')
            self.put('settings', settings)
            self.put('settings_revision', expected + 1)
        return settings

    def pause(self, paused):
        if type(paused) is not bool:
            raise ValueError('invalid pause')
        with self.lock, self.db:
            self.put('paused', paused)

    def cycle(self, cycle_id):
        row = self.db.execute('SELECT * FROM world_cycles WHERE id=?', (cycle_id,)).fetchone()
        if not row:
            raise ValueError('unknown cycle')
        return {**dict(row), 'value': json.loads(row['value'])}

    def prepare(self, actor_id='garik', minutes=30, draw=None):
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError('invalid virtual interval')
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked')").fetchone():
                raise WorldConflict('finish or explicitly abandon the current cycle first')
            if not self.db.execute('SELECT 1 FROM world_actors WHERE id=?', (actor_id,)).fetchone():
                raise ValueError('unknown actor')
            self.put('minute', self.get('minute') + minutes)
            self.put('revision', self.get('revision') + 1)
            state = self.state()
            brief = director(state, actor_id, minutes, random.random() if draw is None else draw)
            cycle_id = 'cycle-' + uuid.uuid4().hex
            value = {'brief': brief, 'inputs': {}, 'results': {}}
            self.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                            (cycle_id, actor_id, 'ready', 'world', state['revision'], json.dumps(value, ensure_ascii=False), time.time()))
            self.snapshot()
        return self.cycle(cycle_id)

    def claim(self, cycle_id, limit):
        with self.lock, self.db:
            cycle = self.cycle(cycle_id)
            if self.get('paused'):
                raise WorldConflict('laboratory is paused')
            if cycle['status'] != 'ready' or cycle['revision'] != self.get('revision'):
                raise WorldConflict('cycle is not ready or its world changed')
            if self.db.execute('SELECT COUNT(*) FROM world_calls').fetchone()[0] >= limit:
                raise WorldConflict('explicit laboratory call limit reached')
            call_id = 'call-' + uuid.uuid4().hex
            self.db.execute('INSERT INTO world_calls VALUES(?,?,?,?,?,?,?)',
                            (call_id, cycle_id, cycle['phase'], 'started', '{}', time.time(), None))
            self.db.execute("UPDATE world_cycles SET status='running' WHERE id=?", (cycle_id,))
        return call_id, cycle

    def save_input(self, cycle_id, payload):
        with self.lock, self.db:
            cycle = self.cycle(cycle_id)
            cycle['value']['inputs'][cycle['phase']] = payload
            self.db.execute('UPDATE world_cycles SET value=? WHERE id=?',
                            (json.dumps(cycle['value'], ensure_ascii=False), cycle_id))

    def complete(self, call_id, value, usage):
        with self.lock, self.db:
            call = self.db.execute('SELECT * FROM world_calls WHERE id=?', (call_id,)).fetchone()
            if not call:
                raise ValueError('unknown call')
            if call['status'] == 'completed':
                return False
            cycle = self.cycle(call['cycle_id'])
            if call['status'] != 'started' or cycle['status'] != 'running' or cycle['revision'] != self.get('revision'):
                raise WorldConflict('stale or interrupted result')
            stage = cycle['phase']
            state = self.state()
            brief = cycle['value']['brief']
            if stage == 'speech':
                if not isinstance(value, str) or not value.strip() or len(value) > 12000:
                    raise ValueError('invalid spoken text')
            else:
                validate_result(stage, value, state, brief)
            event_id = cycle['id'] + '-' + stage
            if stage in ('world', 'outcome'):
                for field, table in [('entities', 'world_entities'), ('threads', 'world_threads')]:
                    for entry in value[field]:
                        self.db.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(?,?,?)',
                                        (entry['id'], json.dumps(entry, ensure_ascii=False), event_id))
                if stage == 'outcome':
                    self.put('minute', state['minute'] + value['elapsed_minutes'])
            if stage == 'choice' and value['note'].strip():
                self.db.execute('INSERT INTO world_notes VALUES(?,?,?,?,?)',
                                (event_id, cycle['actor_id'], cycle['id'], state['minute'], value['note']))
            stored = {'text': value, 'draft': True} if stage == 'speech' else value
            self.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                            (event_id, cycle['id'], cycle['actor_id'], stage, self.get('minute'), json.dumps(stored, ensure_ascii=False)))
            cycle['value']['results'][stage] = value
            next_phase = {'world': 'choice', 'choice': 'outcome', 'outcome': 'done', 'speech': 'done'}[stage]
            status = 'complete' if next_phase == 'done' else 'ready'
            revision = state['revision'] + 1
            self.put('revision', revision)
            self.db.execute('UPDATE world_cycles SET status=?,phase=?,revision=?,value=? WHERE id=?',
                            (status, next_phase, revision, json.dumps(cycle['value'], ensure_ascii=False), cycle['id']))
            self.db.execute("UPDATE world_calls SET status='completed',usage=? WHERE id=?", (json.dumps(usage), call_id))
            self.snapshot()
        return True

    def request_speech(self, cycle_id, surface='private', message='Как у тебя дела?'):
        """An explicit review request, not an autonomous decision to publish."""
        if surface not in ('private', 'channel_draft', 'formal') or not isinstance(message, str) or not 0 < len(message.strip()) <= 2000:
            raise ValueError('invalid speech request')
        with self.lock, self.db:
            cycle = self.cycle(cycle_id)
            if cycle['status'] != 'complete' or 'speech' in cycle['value']['results']:
                raise WorldConflict('speech is only requested once after a completed cycle')
            if self.db.execute("SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked')").fetchone():
                raise WorldConflict('another cycle is active')
            cycle['value']['speech_request'] = {'surface': surface, 'message': message}
            self.db.execute("UPDATE world_cycles SET status='ready',phase='speech',revision=?,value=? WHERE id=?",
                            (self.get('revision'), json.dumps(cycle['value'], ensure_ascii=False), cycle_id))

    def fail(self, call_id, usage=None, rejected=None, error_type='Error'):
        with self.lock, self.db:
            row = self.db.execute('SELECT cycle_id FROM world_calls WHERE id=?', (call_id,)).fetchone()
            self.db.execute("UPDATE world_calls SET status='failed',error='stage failed; review required, no automatic retry',usage=? WHERE id=? AND status='started'",
                            (json.dumps(usage or {}), call_id))
            if row:
                cycle = self.cycle(row[0])
                if rejected is not None:
                    cycle['value']['rejected'] = {'stage': cycle['phase'], 'answer': rejected,
                                                  'error_type': error_type}
                self.db.execute("UPDATE world_cycles SET status='blocked',value=? WHERE id=?",
                                (json.dumps(cycle['value'], ensure_ascii=False), row[0]))

    def abandon(self, cycle_id):
        with self.lock, self.db:
            cycle = self.cycle(cycle_id)
            if cycle['status'] == 'running':
                raise WorldConflict('pause and wait for the running call first')
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id=? AND status IN ('ready','blocked')", (cycle_id,))

    def monitor(self):
        with self.lock:
            state = self.state()
            cycles = [{**dict(r), 'value': json.loads(r['value'])} for r in self.db.execute(
                'SELECT * FROM world_cycles ORDER BY rowid DESC LIMIT 12')]
            calls = [{**dict(r), 'usage': json.loads(r['usage'])} for r in self.db.execute(
                'SELECT * FROM world_calls ORDER BY rowid DESC LIMIT 60')]
            notes = [dict(r) for r in self.db.execute('SELECT * FROM world_notes ORDER BY rowid DESC LIMIT 24')]
            state.update(cycles=cycles, calls=calls, notes=notes,
                         call_count=self.db.execute('SELECT COUNT(*) FROM world_calls').fetchone()[0])
            return state


class WorldSession:
    """One stage per explicit click. No autonomous loop or external actions yet."""
    def __init__(self, store, backend, actor_prompts, speech_contract, max_calls=24):
        if type(max_calls) is not int or not 1 <= max_calls <= 200:
            raise ValueError('invalid world laboratory call limit')
        self.store, self.backend = store, backend
        self.actor_prompts = dict(actor_prompts)
        self.speech_contract, self.max_calls = speech_contract, max_calls

    def step(self, cycle_id):
        from .world_prompts import WORLD, CHOICE, OUTCOME, SPEECH_CONTEXT
        if self.store.cycle(cycle_id)['actor_id'] not in self.actor_prompts:
            raise WorldConflict('actor has no configured personality')
        call_id, cycle = self.store.claim(cycle_id, self.max_calls)
        answer = None
        try:
            actor = cycle['actor_id']
            stage = cycle['phase']
            if actor not in self.actor_prompts:
                raise ValueError('actor has no configured personality')
            state = self.store.state()
            results = cycle['value']['results']
            if stage in ('world', 'outcome'):
                instruction = WORLD if stage == 'world' else OUTCOME
                world = copy.deepcopy(state)
                for event in world['history']:
                    if event['stage'] == 'choice':
                        event['value'] = {k: v for k, v in event['value'].items()
                                          if k in ('kind', 'action', 'entity_ids')}
                action = results.get('choice')
                action = {k: action[k] for k in ('kind', 'action', 'entity_ids')} if action else None
                payload = {'world': world, 'director': cycle['value']['brief'],
                           'current_event': results.get('world'), 'chosen_action': action}
            else:
                instruction = self.actor_prompts[actor] + '\n\n' + (CHOICE if stage == 'choice' else self.speech_contract)
                payload = {'life': self.store.actor_view(actor, state),
                           'moment': results.get('world', {}).get('observation'),
                           'outcome': results.get('outcome', {}).get('observation'),
                           'request': cycle['value'].get('speech_request')}
                if stage == 'speech':
                    instruction += '\n\n' + SPEECH_CONTEXT
                    rows = self.store.db.execute(
                        "SELECT e.value,c.value FROM world_events e JOIN world_cycles c ON c.id=e.cycle_id "
                        "WHERE e.actor_id=? AND e.stage='speech' ORDER BY e.rowid DESC", (actor,)).fetchall()
                    surface = cycle['value']['speech_request']['surface']
                    spoken = [json.loads(r[0])['text'] for r in rows
                              if json.loads(r[1]).get('speech_request', {}).get('surface') == surface][:4]
                    payload['recent_spoken'] = list(reversed(spoken))
            self.store.save_input(cycle_id, payload)
            answer, usage = self.backend.generate(stage, instruction, payload)
            self.store.complete(call_id, answer, usage)
            return self.store.cycle(cycle_id)
        except Exception as error:
            rejected = answer if answer is not None else getattr(self.backend, 'last_output', None)
            self.store.fail(call_id, getattr(self.backend, 'last_usage', {}), rejected, type(error).__name__)
            raise
