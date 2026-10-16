"""One explicitly enabled Telegram life, with persistent memory and voluntary posts."""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

from .telegram import TelegramConnection, TelegramError, private_json
from .world import WorldStore, WorldSession, WorldConflict
from .world_lab import Laboratory, handler as world_handler
from .telegram_setup import Setup, LocalServer, handler as setup_handler

MOSCOW = ZoneInfo('Europe/Moscow')
START_DESIRE = 'Хочу завести свой Telegram-канал и делиться там своей жизнью: затеями, находками, мыслями и тем, что происходит. Пока это желание, а не уже написанный пост.'
DEFAULT_CONTROLS = {'wake_minutes': 15, 'photos': True, 'visit_turns': 3, 'away_minutes': 5, 'episode_minutes': 15, 'sharing_appetite': 50}


def live_interpreter(root, executable, platform):
    """Avoid the Apple CLT Python/SQLite runtime observed spinning in cache purge.

    Keep the configured venv path (not its resolved base binary) so its packages
    are available. Other hosts and already configured interpreters are unchanged.
    """
    if platform=='darwin' and executable.startswith('/Library/Developer/CommandLineTools/'):
        candidate=Path(root)/'.venv/bin/python'
        if not candidate.is_file():
            raise ValueError('Configure .venv and start channel_life with .venv/bin/python')
        return str(candidate)
    return None


def controls_checked(value):
    if not isinstance(value,dict) or set(value) not in (set(DEFAULT_CONTROLS),set(DEFAULT_CONTROLS)-{'sharing_appetite'},set(DEFAULT_CONTROLS)-{'sharing_appetite','episode_minutes'},{'wake_minutes','photos'}):
        raise ValueError('invalid live life controls')
    value={**DEFAULT_CONTROLS,**value}
    if (
            type(value['wake_minutes']) is not int or not 5 <= value['wake_minutes'] <= 90 or
            type(value['photos']) is not bool or type(value['visit_turns']) is not int or not 1<=value['visit_turns']<=6 or
            type(value['away_minutes']) is not int or not 1<=value['away_minutes']<=30 or
            type(value['episode_minutes']) is not int or not 5<=value['episode_minutes']<=90 or
            type(value['sharing_appetite']) is not int or not 0<=value['sharing_appetite']<=100):
        raise ValueError('invalid live life controls')
    return dict(value)


def fresh_start(store, connection, now=None, environment=(), *, actor_id='garik'):
    """Seed a NEW history only. Never delete or inherit old experimental events."""
    return _start_channel(store, connection, now, environment, actor_id=actor_id, keep_history=False)


def activate_existing_world(store, connection, now=None, *, actor_id='garik'):
    """Explicit promotion of an idle laboratory; retain its past without publishing drafts."""
    return _start_channel(store, connection, now, (), actor_id=actor_id, keep_history=True)


def _start_channel(store, connection, now, environment, *, actor_id, keep_history):
    now = time.time() if now is None else now
    binding = connection.get('binding')
    if not binding:
        raise TelegramError('Канал и обсуждение должны быть подключены до запуска жизни.')
    with store.lock, store.db:
        if actor_id not in {a['id'] for a in store.state()['actors']}:
            raise WorldConflict('install the character profile before starting its life')
        if store.actor_get(actor_id, 'channel_epoch'):
            raise WorldConflict('fresh start requires an unused world; existing history was not reset')
        if keep_history:
            if not store.get('paused') or store.db.execute(
                    "SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked')").fetchone():
                raise WorldConflict('promote only a paused laboratory without pending stages')
            posts = list(store.db.execute("SELECT id,value FROM world_events WHERE stage='post'"))
            if any(not json.loads(p['value']).get('draft') or json.loads(p['value']).get('delivery') for p in posts):
                raise WorldConflict('laboratory promotion cannot reinterpret existing delivery history')
            store.db.execute('''CREATE TABLE IF NOT EXISTS channel_jobs(post_id TEXT PRIMARY KEY,status TEXT NOT NULL,
                receipts TEXT NOT NULL,photo TEXT,error TEXT)''')
            for post in posts:
                store.db.execute('INSERT INTO channel_jobs VALUES(?,?,?,?,?)',
                                 (post['id'], 'local_only', '[]', None, 'Laboratory draft retained; never dispatched'))
            store.actor_put(actor_id, 'channel_activation', {'basis': 'explicit_laboratory_promotion',
                            'at': now, 'local_only_posts': [p['id'] for p in posts]})
            minute = store.get('minute')
            store.put('revision', store.get('revision') + 1)
        else:
            if store.db.execute('SELECT 1 FROM world_cycles').fetchone():
                raise WorldConflict('fresh start requires an unused world; existing history was not reset')
            minute = datetime.fromtimestamp(now, MOSCOW).hour * 60 + datetime.fromtimestamp(now, MOSCOW).minute
        store.put('minute', minute)
        store.actor_put(actor_id, 'channel_epoch', {'wall': now, 'minute': minute})
        store.actor_put(actor_id, 'channel_start', {'desire': START_DESIRE, 'basis': 'author_requested_starting_desire'})
        store.actor_put(actor_id, 'channel_connection', {
            'channel_name': binding['channel'].get('title', 'Свой канал'),
            'available': True,
            'explanation': 'Канал уже создан, бот подключён и получил права. Создавать его заново, просить пароль или токен не надо. '
                           'Ты можешь написать готовый пост, ответить на доступный комментарий или оставить мысль при себе. '
                           'Выбранные тексты хост отправляет дословно; успешная доставка отмечается квитанцией. '
                           'Можно иногда выбрать фотографию наблюдаемого предмета через подключённый инструмент. '
                           'Это твой канал, не рабочий отчёт: свободный формат и мат допустимы. '
                           'Обязательного графика и нормы постов нет. Комментарии реальных читателей приходят отдельно; чужой совет не обязан становиться поступком.'})
        store.actor_put(actor_id, 'life_controls', DEFAULT_CONTROLS)
        store.actor_put(actor_id, 'life_status', {'status': 'paused', 'next_wake': now, 'last_cycle': None, 'error': None})
        store.actor_put(actor_id, 'channel_life', {'enabled': True, 'run': store.directory.name})
        settings = ({**store.get('settings'), 'post_cooldown_minutes': 0} if keep_history else
                    {**store.get('settings'), 'events_per_hour': 1.5, 'surprise': 65,
                     'quiet': 15, 'max_threads': 5, 'post_cooldown_minutes': 0})
        store.put('settings', settings)
        # Optional author-approved environment definitions, not generated episode states.
        for entity in environment:
            if entity['id'] in ('garik', 'home', 'shawarma'):
                continue
            from .world_protocol import ENTITY, validate_shape
            validate_shape(entity, ENTITY)
            store.db.execute('INSERT INTO world_entities VALUES(?,?,?)',
                             (entity['id'], json.dumps(entity, ensure_ascii=False), 'author-environment-at-fresh-start'))
        store.snapshot()


def split_text(text, limit=3500):
    """Preserve every character; no paraphrase or editorial shortening."""
    result = []
    while len(text) > limit:
        boundary = text.rfind('\n', 0, limit + 1)
        cut = boundary + 1 if boundary >= limit // 2 else limit
        result.append(text[:cut])
        text = text[cut:]
    if text:
        result.append(text)
    return result


class ChannelLife:
    def __init__(self, lab, connection, persona, renderer=None, *, now=time.time, photo_factory=None, social_enabled=False,
                 actor_id=None, references=None, lively_world=None, recall_enabled=False):
        self.lab, self.store, self.connection = lab, lab.store, connection
        self.actor_id = actor_id or lab.actor_id
        if self.actor_id not in lab.session.actor_prompts:
            raise WorldConflict('life actor has no configured persona')
        self.name = next(a['name'] for a in self.store.state()['actors'] if a['id'] == self.actor_id)
        self.references = references or {}
        self.persona, self.renderer, self.now = persona, renderer, now
        self.cancel = lab.session.cancel
        self.photo_factory = photo_factory
        self.worker = None
        self.active_lock = threading.Lock()
        self.current_phase = None
        self.lab.life = self
        self.social = None
        with self.store.lock, self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS channel_jobs(post_id TEXT PRIMARY KEY,status TEXT NOT NULL,
                    receipts TEXT NOT NULL,photo TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS channel_comments(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                    audience_id TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS channel_read_batches(id TEXT PRIMARY KEY,considered INTEGER NOT NULL DEFAULT 0);
            ''')
            self.store.db.execute("UPDATE channel_jobs SET status='unknown',error='process stopped before full delivery; no replay' WHERE status='sending' AND post_id IN (SELECT id FROM world_events WHERE actor_id=?)", (self.actor_id,))
            columns={r[1] for r in self.store.db.execute('PRAGMA table_info(channel_read_batches)')}
            if 'deferred_until' not in columns:
                self.store.db.execute('ALTER TABLE channel_read_batches ADD COLUMN deferred_until REAL')
            self.put('life_controls',{**DEFAULT_CONTROLS,**(self.get('life_controls') or {})})
            if self.get('world_profile') is None:
                self.put('world_profile', {'active': lab.session.lively_world, 'requested': lab.session.lively_world})
            lab.session.lively_world = self.get('world_profile')['active']
        if lively_world is not None:
            self.request_world_profile(lively_world)
        self.sync_world_profile()
        if social_enabled:
            from .channel_social import SocialMemory
            self.social=SocialMemory(self.store,now=self.now)
            if recall_enabled:
                from .channel_recall import ChannelRecall
                self.social.recall=ChannelRecall(self.store)
            self.social.renderer_available=renderer is not None
            self.social.references=self.references
            self.social.photo_subjects=self.photo_subjects
            self.social.photo_style=self.photo_style
            self.lab.session.channel_extension=self.social
            self.backfill_people()
            self.social.gallery.backfill()

    def request_world_profile(self, lively):
        if type(lively) is not bool:
            raise ValueError('invalid world profile')
        with self.store.lock, self.store.db:
            self.put('world_profile', {**self.get('world_profile'), 'requested': lively})

    def sync_world_profile(self):
        """Finish committed episodes in their original mode, then switch once.

        Unseen possibilities are superseded, not deleted or made into events.
        Already exposed hooks and all actual stories retain their continuity.
        """
        with self.store.lock, self.store.db:
            profile = self.get('world_profile')
            row = self.store.db.execute("SELECT value FROM world_cycles WHERE actor_id=? AND status IN ('ready','running','blocked','waiting') ORDER BY rowid LIMIT 1",(self.actor_id,)).fetchone()
            if row:
                self.lab.session.lively_world = json.loads(row[0]).get('lively_world', False)
                return
            if profile['active'] != profile['requested']:
                superseded = []
                for row in self.store.db.execute('SELECT id,value FROM world_hooks').fetchall():
                    hook = json.loads(row['value'])
                    if self.actor_id not in hook['actor_ids'] or hook.get('superseded') or hook.get('exposed_to') or hook['closes_minute'] < self.store.get('minute'):
                        continue
                    hook['superseded'] = {'reason': 'world_profile_changed', 'at': self.now()}
                    self.store.db.execute('UPDATE world_hooks SET value=? WHERE id=?', (json.dumps(hook, ensure_ascii=False), row['id']))
                    superseded.append(row['id'])
                history = self.get('world_profile_history') or []
                history.append({'at': self.now(), 'from': profile['active'], 'to': profile['requested'], 'superseded_hook_ids': superseded})
                self.put('world_profile_history', history)
                profile = {**profile, 'active': profile['requested']}
                self.put('world_profile', profile)
            self.lab.session.lively_world = profile['active']

    def get(self, key):
        return self.store.actor_get(self.actor_id, key)

    def put(self, key, value):
        self.store.actor_put(self.actor_id, key, value)

    def state(self):
        with self.store.lock:
            status = self.get('life_status')
            jobs = [dict(r) for r in self.store.db.execute('SELECT j.* FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE e.actor_id=? ORDER BY j.rowid DESC LIMIT 12', (self.actor_id,))]
            for job in jobs:
                job['receipts'] = json.loads(job['receipts'])
                if job['photo']:
                    job['photo'] = json.loads(job['photo'])
            value = {**status, 'actor_id': self.actor_id, 'name': self.name,
                    'paused': self.store.get('paused'), 'busy': self.lab.busy,
                    'phase': self.current_phase, 'controls': self.get('life_controls'),
                    'world_calls': self.store.monitor()['call_count'], 'jobs': jobs,
                    'persona_sha256': hashlib.sha256(self.persona.encode()).hexdigest(),
                    'role_boundary': {'active': True, 'on_violation': 'pause_before_delivery'},
                    'world_profile': {**self.get('world_profile'), 'active': self.lab.session.lively_world},
                    'awake': self.store.get('awake_status') if self.store.get('shared_host') else self.get('awake_status'), 'cadence': 'Свободно: графика и обязательных постов нет.'}
            value['activity'] = self.activity()
            value['can_skip_failed_expression'] = self.failed_expression() is not None
            view = self.store.actor_view(self.actor_id)
            value.update(memories=view['memories'], concerns=view['concerns'], undertakings=view['undertakings'])
            if self.social:
                value.update(self.social.context(self.actor_id))
                value['attention']=self.get('channel_visit')
                value['recall']=self.social.recall.report if self.social.recall else {'mode':'disabled'}
            return value

    def activity(self):
        """Owner-visible accepted action and deadline; never expose its future result."""
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT id,status,phase,value FROM world_cycles WHERE actor_id=? "
                "AND status IN ('ready','waiting') ORDER BY rowid", (self.actor_id,)).fetchall()
            for row in rows:
                cycle = json.loads(row['value'])
                if row['status'] != 'ready' or row['phase'] != 'experience' or cycle.get('suspended_cycle_id'):
                    continue
                if 'outcome' not in cycle.get('results', {}) or self.store.get('minute') <= self.minute_now():
                    continue
                epoch = self.get('channel_epoch')
                until = epoch['wall'] + (self.store.get('minute') - epoch['minute']) * 60
                return {'cycle_id': row['id'], 'action': cycle['results']['choice']['action'],
                        'until': until, 'remaining_minutes': max(0, math.ceil((until-self.now())/60))}
            return None

    def failed_expression(self):
        """Only a failed, empty speech service can be discarded without replay."""
        with self.store.lock:
            if self.store.get('shared_host') or not self.store.get('paused') or self.lab.busy or self.get('life_status')['status'] != 'error':
                return None
            blocked = self.store.db.execute(
                "SELECT id FROM world_cycles WHERE actor_id=? AND status IN ('blocked','running')", (self.actor_id,)).fetchall()
            if len(blocked) != 1:
                return None
            cycle = self.store.cycle(blocked[0]['id'])
            if (cycle['status'] != 'blocked' or cycle['phase'] != 'share' or
                    not cycle['value'].get('direct_channel') or cycle['value'].get('results') or
                    cycle['value'].get('rejected') is not None or cycle['revision'] != self.store.get('revision')):
                return None
            call = self.store.db.execute("SELECT * FROM world_calls WHERE cycle_id=? ORDER BY rowid DESC LIMIT 1", (cycle['id'],)).fetchone()
            if not call or call['status'] != 'failed' or call['stage'] != 'share':
                return None
            if (self.store.db.execute('SELECT 1 FROM world_events WHERE cycle_id=?', (cycle['id'],)).fetchone() or
                    self.store.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone()):
                return None
            # Unknown external operations require their own receipt review.
            if (self.store.db.execute("SELECT 1 FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE e.actor_id=? AND j.status IN ('sending','unknown')", (self.actor_id,)).fetchone() or
                    self.store.db.execute("SELECT 1 FROM channel_reaction_jobs WHERE status IN ('sending','unknown')").fetchone()):
                return None
            return {'cycle_id': cycle['id'], 'call_id': call['id']}

    def skip_failed_expression(self):
        """Explicit owner review: retain failure, never manufacture or retry speech."""
        with self.active_lock, self.store.lock, self.store.db:
            review = self.failed_expression()
            if review is None:
                raise TelegramError('Пропуск недоступен: есть результат, действие или доставка, требующие отдельной проверки.')
            self.store.abandon(review['cycle_id'])
            self.put('stage_reviews', [*(self.get('stage_reviews') or []),
                {**review, 'at': self.now(), 'decision': 'skip_empty_failed_expression',
                 'basis': 'owner_explicit_review', 'external_events': 0, 'model_replay': False}])
            status = self.get('life_status')
            self.put('life_status', {**status, 'status': 'paused', 'error': None})
            self.put('waiting_channel_next_check', self.now()+self.get('life_controls')['wake_minutes']*60)
            self.lab.failure = None
            self.current_phase = None

    def configure(self, value):
        checked = controls_checked({**self.get('life_controls'),**value})
        with self.store.lock, self.store.db:
            self.put('life_controls', checked)

    def action(self, running):
        if type(running) is not bool:
            raise ValueError('invalid life action')
        with self.store.lock, self.store.db:
            status = self.get('life_status')
            if running:
                if status['status'] == 'error' or self.store.db.execute("SELECT 1 FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE e.actor_id=? AND j.status IN ('unknown','sending')", (self.actor_id,)).fetchone():
                    raise TelegramError('Есть незавершённая операция. Нужна проверка, автоматического повтора нет.')
                if self.connection.poll_blocked or self.connection.get('binding') is None:
                    raise TelegramError('Сначала восстанови подключение Telegram.')
                status.update(status='running', next_wake=min(status['next_wake'], self.now()))
            else:
                status['status'] = 'paused'
            self.put('life_status', status)
            self.store.put('paused', not running)

    def block(self, message):
        with self.store.lock, self.store.db:
            status = self.get('life_status')
            status.update(status='error', error=message)
            self.put('life_status', status)
            self.store.put('paused', True)
            self.lab.failure = message

    def confirm_absent(self, post_id):
        """Owner-only recovery after explicitly checking absence in Telegram."""
        with self.store.lock, self.store.db:
            if not self.store.get('paused'):
                raise TelegramError('Для разбора доставки сначала нужна пауза.')
            job = self.store.db.execute('SELECT * FROM channel_jobs WHERE post_id=?', (post_id,)).fetchone()
            if not job or job['status'] not in ('sending', 'unknown'):
                raise TelegramError('Эта доставка не требует такого восстановления.')
            repairs = self.get('delivery_repairs') or []
            repairs.append({'post_id': post_id, 'basis': 'owner_explicitly_checked_absence_in_channel',
                            'at': self.now(), 'previous_job': dict(job)})
            self.put('delivery_repairs', repairs)
            self.store.db.execute("UPDATE channel_jobs SET status='reviewed_absent',error='owner checked absence; new explicit attempt allowed' WHERE post_id=?", (post_id,))
            status = self.get('life_status')
            status.update(status='paused', error=None)
            self.put('life_status', status)
            self.lab.failure = None

    def minute_now(self):
        epoch = self.get('channel_epoch')
        return epoch['minute'] + max(0, int((self.now() - epoch['wall']) // 60))

    def channel_cycle(self, *, bootstrap=False, suspended_cycle_id=None):
        if self.social:
            import uuid
            from .world import director
            with self.store.lock,self.store.db:
                active = [r[0] for r in (self.store.db.execute("SELECT id FROM world_cycles WHERE status IN ('ready','running') OR (actor_id=? AND status='blocked')", (self.actor_id,))
                          if self.store.get('shared_host') else self.store.db.execute("SELECT id FROM world_cycles WHERE status IN ('ready','running','blocked')"))]
                if active and (not suspended_cycle_id or active != [suspended_cycle_id]):
                    raise WorldConflict('another stage is active')
                revision = self.store.get('revision')
                self.store.put('minute',max(self.store.get('minute'),self.minute_now()))
                self.store.put('revision',self.store.get('revision')+1)
                if suspended_cycle_id:
                    self.store.rebase_waiting_cycle(suspended_cycle_id, self.actor_id, revision)
                cid='cycle-'+uuid.uuid4().hex
                value={'brief':director(self.store.state(),self.actor_id,1,1),'inputs':{},'results':{},
                       'personal_life':self.lab.session.personal_life,'lossless_intentions':self.lab.session.lossless_intentions,
                       'direct_channel':True,'active_world':self.lab.session.active_world,'entry_phase':'share',
                       'lively_world':self.lab.session.lively_world,
                       'bootstrap':bootstrap,'channel_visit':True,
                       'channel_purpose':(self.get('channel_attention') or {}).get('purpose','comments')}
                if suspended_cycle_id:
                    value.update(suspended_cycle_id=suspended_cycle_id, service_minute=self.minute_now())
                    visible = self.store.state_for_cycle({'actor_id': self.actor_id, 'value': value}, self.store.state())
                    value['brief'] = director(visible, self.actor_id, 1, 1)
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                      (cid,self.actor_id,'ready','share',self.store.get('revision'),json.dumps(value),self.now()))
                self.put('bootstrap_prepared',True)
                self.store.snapshot()
                return self.store.cycle(cid)
        cycle = self.lab.session.prepare(self.actor_id, minutes=1, draw=1)
        with self.store.lock, self.store.db:
            value = self.store.cycle(cycle['id'])['value']
            value.update(bootstrap=bootstrap, entry_phase='share')
            self.store.db.execute("UPDATE world_cycles SET phase='share',value=? WHERE id=?",
                                  (json.dumps(value, ensure_ascii=False), cycle['id']))
            self.put('bootstrap_prepared', True)
        return self.store.cycle(cycle['id'])

    def import_comments(self, *, suspended_cycle_id=None):
        """Only replies to posts delivered by this fresh life enter its memory."""
        with self.connection.lock:
            group = self.connection.get('binding')['discussion']['id']
            rows = [dict(r) for r in self.connection.db.execute(
                "SELECT m.*,f.post_id AS telegram_post FROM messages m JOIN forwards f ON f.group_id=m.chat_id AND f.root_id=m.root_id "
                "WHERE m.chat_id=? AND m.kind='comment' ORDER BY m.received,m.id", (group,))]
        # Fetch new photos outside either database transaction. Failed reads do
        # not block life and cannot be confused with uncertain external sends.
        for row in rows:
            if not self.store.db.execute('SELECT 1 FROM channel_comments WHERE chat_id=? AND message_id=?',(group,row['id'])).fetchone():
                row['photo']=self.connection.comment_photo(group,row['id'])
        with self.store.lock, self.store.db:
            revision = self.store.get('revision')
            delivered = {}
            for job in self.store.db.execute("SELECT j.post_id,j.receipts FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE j.status='sent' AND e.actor_id=?", (self.actor_id,)):
                for receipt in json.loads(job['receipts']):
                    if receipt['chat_id'] == self.connection.get('binding')['channel']['id']:
                        delivered[receipt['message_id']] = job['post_id']
            count = 0
            for row in rows:
                if row['telegram_post'] not in delivered or self.store.db.execute(
                        'SELECT 1 FROM channel_comments WHERE chat_id=? AND message_id=?', (group, row['id'])).fetchone():
                    continue
                scope = (hashlib.sha256((self.actor_id+':'+str(group)).encode()).hexdigest()[:16]+'-') if self.store.get('shared_host') else ''
                audience_id = 'tg-comment-' + scope + str(row['id'])
                cycle_id = 'received-' + scope + str(row['id'])
                value = {'post_id': delivered[row['telegram_post']],
                         'comments': [{'speaker': row['speaker'][:60], 'text': row['text']}],
                         'simulated': False, 'telegram': {'chat_id': group, 'message_id': row['id'],
                                                        'post_id': row['telegram_post'], 'received': row['received']}}
                if row.get('photo'):value['photo']=row['photo']
                if self.social:
                    from .channel_social import person_key
                    key=person_key(row,group)
                    value['person_key']=key
                    self.social.register(row['speaker'][:60],key,row['user_id'] is None,self.actor_id)
                cycle_value = {'results': {'audience': value}, 'inputs': {}, 'external_receipt': True}
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                      (cycle_id, self.actor_id, 'complete', 'done', self.store.get('revision'), json.dumps(cycle_value, ensure_ascii=False), row['received']))
                self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                      (audience_id, cycle_id, self.actor_id, 'audience',
                                       min(self.store.get('minute'), self.minute_now()), json.dumps(value, ensure_ascii=False)))
                self.store.db.execute('INSERT INTO channel_comments VALUES(?,?,?)', (group, row['id'], audience_id))
                self.store.db.execute('INSERT INTO channel_read_batches(id) VALUES(?)', (audience_id,))
                count += 1
            if count:
                self.store.put('revision', self.store.get('revision') + 1)
                if suspended_cycle_id:
                    self.store.rebase_waiting_cycle(suspended_cycle_id, self.actor_id, revision)
                self.store.snapshot()
            return count

    def photo(self, post):
        if not self.get('life_controls')['photos']:
            return None
        from .world_photo import photo_context
        cycle = self.store.cycle(post['cycle_id'])
        output=cycle['value'].get('social_output',{})
        explicit='attachment' in output
        attachment=output.get('attachment')
        if explicit and attachment is None:return None
        if attachment and attachment['mode']=='existing':
            known=[e['id'] for e in self.store.actor_view(post['actor_id'])['entities']]
            item=self.social.gallery.get(post['actor_id'],attachment['gallery_id'],known)
            return {'status':'reused','gallery_id':item['id'],
                    'choice':{'decision':'show','framing':item['view'],'source_ids':item['source_ids']},
                    'image':{k:item[k] for k in ('image_path','sha256','subject_ids')},'image_calls':0}
        if self.renderer is None or post['value'].get('reply_to') and not explicit:return None
        state = self.store.state_for_cycle(cycle, self.store.state())
        context = photo_context(post['actor_id'], state['entities'], state['history'],
                                cycle['value']['results'].get('experience', {}).get('note', ''), portrait_subjects=self.photo_subjects(),photo_style=self.photo_style())
        if attachment and self.store.get('shared_host'):
            # Selection fixed the capture moment; a peer may have left since then.
            context=cycle['value'].get('inputs',{}).get('share',{}).get('photo_scene',context)
        context={**context,'minute':context.get('minute',state['minute'])}
        if not context['sources']:
            return None  # An initial thought has no occurred object scene to photograph.
        context['ready_post'] = post['value']['text']
        directory = self.media_directory / 'photos' / post['id']
        from .world_media import MediaReferenceLibrary
        from .photo_case import AutonomousPhotoCase
        if self.store.get('shared_host'):
            from .shared_media import SharedPhotoReferences
            library=SharedPhotoReferences(self.media_directory/'visual-memory',self.store,context['minute'])
        else:library = MediaReferenceLibrary(self.media_directory / 'visual-memory')
        try:
            factory = self.photo_factory or AutonomousPhotoCase
            case = factory(directory, self.lab.session.backend, self.renderer, self.persona,
                           reference_library=library, cancel=self.cancel, references=self.references)
            if attachment:
                choice={'decision':'show','reason':'Selected by the character with this message',
                        **{k:attachment[k] for k in ('subject_ids','source_ids','framing')},'caption':post['value']['text'][:2000]}
                result=case.run(context,choice=choice)
            else:result=case.run(context)
            if self.social and result['status']=='passed':
                result['gallery_id']=self.social.gallery.register(result['image'],post['actor_id'],result['choice']['framing'],
                    result['choice']['source_ids'],post['value']['text'],new_job=post['id'])
            return result
        finally:
            library.close()

    @property
    def media_directory(self):
        return self.store.directory / 'media' / self.actor_id if self.store.get('shared_host') else self.store.directory

    def photo_subjects(self):
        if self.store.get('shared_host'):
            return [self.actor_id,*self.store.nearby(self.actor_id)]
        return self.references

    def photo_style(self):
        return getattr(getattr(self.lab,'profile',None),'photo_style','')

    def deliver(self, post):
        if post['value'].get('retracted'):return
        from .role_boundary import check_character_speech
        check_character_speech(post['value']['text'],self.store.db.execute('SELECT name FROM world_actors WHERE id=?',(post['actor_id'],)).fetchone()[0])
        with self.store.lock, self.store.db:
            old = self.store.db.execute('SELECT status FROM channel_jobs WHERE post_id=?', (post['id'],)).fetchone()
            if old:
                if old[0] in ('sent','rejected'):
                    return
                if old[0] != 'reviewed_absent':
                    raise TelegramError('Неизвестная доставка прошлого запуска. Автоматический повтор заблокирован.')
                self.store.db.execute("UPDATE channel_jobs SET status='sending' WHERE post_id=?", (post['id'],))
            else:
                self.store.db.execute('INSERT INTO channel_jobs VALUES(?,?,?,?,?)', (post['id'], 'sending', '[]', None, None))
        self.current_phase = 'photo_choice'
        image_result = self.photo(post)
        if self.store.get('paused') or (self.cancel and self.cancel.is_set()):
            # No external request was made. The prepared text/image can be resumed.
            with self.store.lock, self.store.db:
                self.store.db.execute('DELETE FROM channel_jobs WHERE post_id=?', (post['id'],))
            return
        image = image_result['image'] if image_result and image_result['status'] in ('passed','reused') else None
        reply_to = None
        peer_reply=False
        if post['value'].get('reply_to'):
            audience = next(e for e in self.store.channel_context(self.actor_id) if e['id'] == post['value']['audience_id'])
            if audience['value'].get('simulated') is not False:
                raise TelegramError('Вымышленный комментарий не может получить настоящий ответ.')
            reply_to = audience['value']['telegram']['message_id']
            with self.connection.lock:
                author=self.connection.db.execute('SELECT user_id FROM messages WHERE chat_id=? AND id=?',
                    (audience['value']['telegram']['chat_id'],reply_to)).fetchone()
            peer_reply=bool(author and author[0] in getattr(self,'peer_bot_ids',set()))
        self.current_phase = 'delivery'
        text = post['value']['text']
        receipts = []
        key = 'life:' + self.store.directory.name + ':' + post['id']
        attempts = sum(r['post_id'] == post['id'] for r in (self.get('delivery_repairs') or []))
        if attempts:
            key += ':review' + str(attempts)
        options={'_thread_reply':True} if peer_reply else {}
        try:
            if image and len(text) <= 1024:
                receipts.append(self.connection.send(key + ':photo', text, reply_to=reply_to, photo=Path(image['image_path']).read_bytes(),**options))
            else:
                for index, part in enumerate(split_text(text)):
                    receipts.append(self.connection.send(key + ':part' + str(index), part, reply_to=reply_to,**options))
                if image:
                    receipts.append(self.connection.send(key + ':photo', '', reply_to=reply_to, photo=Path(image['image_path']).read_bytes(),**options))
        except TelegramError as error:
            if peer_reply and not receipts and error.code==400 and (error.transport or {}).get('reason')=='reply_not_found':
                with self.store.lock,self.store.db:
                    self.store.db.execute("UPDATE channel_jobs SET status='rejected',error='Telegram reply_not_found; no accepted reply, no replay' WHERE post_id=?",(post['id'],))
                    self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',
                        (json.dumps({**post['value'],'delivery':{'status':'rejected','telegram_code':400,'reason':'reply_not_found'}},ensure_ascii=False),post['id']))
                    self.store.snapshot()
                return
            raise
        compact = [{k: r[k] for k in ('chat_id', 'message_id', 'key', 'status')} for r in receipts]
        photo_summary = {'status': image_result['status'], 'choice': image_result['choice']} if image_result else None
        if image_result and image_result.get('gallery_id'):photo_summary['gallery_id']=image_result['gallery_id']
        if image:
            from .world_media import MediaReferenceLibrary
            library = MediaReferenceLibrary(self.media_directory / 'visual-memory')
            try:
                if image_result['status']!='reused':
                    library.register(image, post['actor_id'], image_result['choice']['framing'], source='channel',
                                     channel_post_id=str(receipts[-1]['chat_id'])+':'+str(receipts[-1]['message_id']))
            finally:
                library.close()
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE channel_jobs SET status='sent',receipts=?,photo=? WHERE post_id=?",
                                  (json.dumps(compact), json.dumps(photo_summary, ensure_ascii=False) if photo_summary else None, post['id']))
            value = {**post['value'], 'draft': False, 'delivery': {'status': 'sent', 'receipts': compact}}
            if image:
                value['photo'] = {'image_path': image['image_path'], 'sha256': image['sha256'], 'subject_ids': image['subject_ids']}
            self.store.db.execute('UPDATE world_events SET value=? WHERE id=?', (json.dumps(value, ensure_ascii=False), post['id']))
            if self.social:
                self.social.delivered(post)
                if image:self.social.gallery.delivered(post['actor_id'],image_result['gallery_id'],post['id'],receipts[-1])
            start = self.get('channel_start')
            if start and not start.get('fulfilled') and not post['value'].get('reply_to'):
                self.put('channel_start', {**start, 'fulfilled': True,
                                                 'first_post': post['id'], 'delivery': compact})
            self.store.snapshot()

    def backfill_people(self):
        from .channel_social import person_key
        with self.connection.lock:
            messages={(r['chat_id'],r['id']):dict(r) for r in self.connection.db.execute('SELECT * FROM messages')}
        with self.store.lock,self.store.db:
            for row in self.store.db.execute("SELECT id,value FROM world_events WHERE stage='audience' AND actor_id=?",(self.actor_id,)).fetchall():
                value=json.loads(row['value']);tg=value.get('telegram',{})
                message=messages.get((tg.get('chat_id'),tg.get('message_id')))
                if not message:continue
                key=person_key(message,tg['chat_id']);value['person_key']=key
                self.social.register(message['speaker'][:60],key,message['user_id'] is None,self.actor_id)
                self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value,ensure_ascii=False),row['id']))

    def focus(self, fulfill=None, *, own_channel=False):
        if own_channel or fulfill:
            with self.store.lock,self.store.db:
                self.put('channel_attention',{'purpose':'own_channel','focus_ids':[],
                    'fulfill_intent':fulfill,'allowed_reactions':[]})
            return []
        with self.store.lock:
            ids=[r[0] for r in self.store.db.execute(
                'SELECT b.id FROM channel_read_batches b JOIN world_events e ON e.id=b.id WHERE e.actor_id=? AND b.considered=0 AND (b.deferred_until IS NULL OR b.deferred_until<=?) ORDER BY b.rowid LIMIT 8',(self.actor_id,self.now()))]
            cache=self.get('reaction_capabilities')
        if not ids and not fulfill:
            with self.store.lock,self.store.db:
                if self.get('channel_attention')!={'focus_ids':[],'fulfill_intent':None,'allowed_reactions':[]}:
                    self.put('channel_attention',{'focus_ids':[],'fulfill_intent':None,'allowed_reactions':[]})
            return []
        if not cache or self.now()-cache['checked']>300:
            self.current_phase='reaction_capabilities'
            try:
                cache={'checked':self.now(),'emojis':self.connection.reaction_options()}
            except TelegramError as error:
                # Optional read-only discovery must not stop life on a network outage.
                # Authentication/rights/API errors and actual reaction sends still block.
                transport=getattr(error,'transport',None)
                with self.store.lock,self.store.db:
                    self.put('last_focus_failure',{'at':self.now(),'type':type(error).__name__,
                        'telegram_code':error.code,'method':'getChat','transport':transport})
                if error.code is not None or not transport or transport.get('method')!='getChat':
                    raise
                cache={'checked':self.now(),'emojis':[],'degraded':True}
        with self.store.lock,self.store.db:
            self.put('reaction_capabilities',cache)
            self.put('channel_attention',{'purpose':'comments','focus_ids':ids,'fulfill_intent':fulfill,'allowed_reactions':cache['emojis']})
        return ids

    def finish_visit_turn(self):
        with self.store.lock,self.store.db:
            visit=self.get('channel_visit') or {'started':self.now(),'turns':0,'away_until':0}
            if visit.get('away_until',0)>self.now():
                visit={'started':self.now(),'turns':0,'away_until':0}
            visit['turns']+=1
            if (self.get('visit_leave_requested') or visit['turns']>=self.get('life_controls')['visit_turns'] or
                    self.now()-visit['started']>=120):
                visit['away_until']=self.now()+self.get('life_controls')['away_minutes']*60
            self.put('channel_visit',visit)

    def confirm_reaction_absent(self,job_id):
        """One explicit owner absence review; retain the uncertain transport row."""
        if self.get('life_status')['status']=='running' or self.lab.busy:
            raise TelegramError('Сначала поставьте автора реакции на паузу.')
        with self.store.lock,self.store.db:
            row=self.store.db.execute('SELECT j.* FROM channel_reaction_jobs j JOIN world_events e ON e.id=j.audience_id WHERE j.id=? AND e.actor_id=?',
                                      (job_id,self.actor_id)).fetchone()
            if not row or row['status'] not in ('sending','unknown'):
                raise TelegramError('Эта реакция не требует такой проверки.')
            repairs=self.get('reaction_delivery_repairs') or []
            attempts=sum(r['job_id']==job_id for r in repairs)
            key='reaction:'+self.store.directory.name+':'+job_id+(':review'+str(attempts) if attempts else '')
            with self.connection.lock:
                dispatch=self.connection.db.execute('SELECT * FROM reactions WHERE key=?',(key,)).fetchone()
            if not dispatch or dispatch['status']!='unknown':
                raise TelegramError('Нет неизвестной отправки для этой проверки.')
            repairs.append({'job_id':job_id,'at':self.now(),'basis':'owner_explicitly_checked_reaction_absence',
                            'previous_job':dict(row),'previous_dispatch':dict(dispatch)})
            self.put('reaction_delivery_repairs',repairs)
            self.store.db.execute("UPDATE channel_reaction_jobs SET status='pending' WHERE id=?",(job_id,))
            status=self.get('life_status');status.update(status='paused',error=None)
            self.put('life_status',status);self.lab.failure=None

    def deliver_reaction(self,row):
        event=self.social.evidence(self.actor_id)[row['audience_id']]
        if event['value'].get('simulated') is not False:raise TelegramError('Реакции доступны только на реальные комментарии.')
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE channel_reaction_jobs SET status='sending' WHERE id=?",(row['id'],))
        self.current_phase='reaction'
        key='reaction:'+self.store.directory.name+':'+row['id']
        repairs=self.get('reaction_delivery_repairs') or []
        attempts=sum(r['job_id']==row['id'] for r in repairs)
        if attempts:key+=':review'+str(attempts)
        try:
            receipt=self.connection.react(key,event['value']['telegram']['message_id'],row['emoji'])
        except Exception as error:
            with self.connection.lock:
                dispatch=self.connection.db.execute('SELECT status FROM reactions WHERE key=?',(key,)).fetchone()
                transport=getattr(error,'transport',None) or getattr(self.connection.api,'last_failure',None)
            with self.store.lock,self.store.db:
                if dispatch is None:
                    # react claims its Telegram journal before setMessageReaction.
                    # A failed getChat preflight is not an uncertain mutation.
                    self.store.db.execute("UPDATE channel_reaction_jobs SET status='pending' WHERE id=?",(row['id'],))
                self.store.put('last_reaction_failure',{'job_id':row['id'],'key':key,
                    'dispatch_recorded':dispatch is not None,'transport':transport})
                if dispatch is not None and isinstance(error,TelegramError) and error.code==400:
                    # An explicit API rejection is evidence of no accepted
                    # reaction. Keep the original transport record; never retry.
                    self.store.db.execute("UPDATE channel_reaction_jobs SET status='rejected',receipt=? WHERE id=?",
                        (json.dumps({'status':'rejected','telegram_code':400,'key':key}),row['id']))
                    return
            raise
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE channel_reaction_jobs SET status='sent',receipt=? WHERE id=?",(json.dumps(receipt),row['id']))

    def tick_social(self,status):
        if not self.get('social_review_complete'):
            self.current_phase='memory';self.lab.busy=True
            return self.social.bootstrap(self.lab.session.backend,self.persona,self.cancel,actor=self.actor_id)
        with self.store.lock:
            uncertain=self.store.db.execute("SELECT 1 FROM channel_jobs WHERE status='unknown' UNION ALL SELECT 1 FROM channel_reaction_jobs WHERE status IN ('sending','unknown')").fetchone()
            cycles=[self.store.cycle(r[0]) for r in self.store.db.execute("SELECT id FROM world_cycles WHERE status IN ('ready','running','blocked') ORDER BY rowid")]
            pending=[{**dict(r),'value':json.loads(r['value'])} for r in self.store.db.execute(
                "SELECT * FROM world_events WHERE stage='post' AND id NOT IN (SELECT post_id FROM channel_jobs WHERE status IN ('sent','local_only','rejected')) ORDER BY rowid LIMIT 1")]
            reactions=[dict(r) for r in self.store.db.execute("SELECT * FROM channel_reaction_jobs WHERE status='pending' ORDER BY rowid LIMIT 1")]
        if uncertain or any(c['status']=='blocked' for c in cycles):
            return self.block('Есть незавершённая операция. Автоматического повтора нет.')
        visiting = next((c for c in cycles if c['value'].get('suspended_cycle_id')), None)
        if visiting:
            self.current_phase='share'; self.lab.busy=True
            self.lab.session.step(visiting['id'])
            self.finish_visit_turn()
            return
        if self.store.get('minute')>self.minute_now() and not (cycles and cycles[0]['value'].get('bootstrap')):
            if pending and self.store.cycle(pending[0]['cycle_id'])['value'].get('suspended_cycle_id'):
                self.lab.busy=True; self.deliver(pending[0]); return
            if reactions:
                self.lab.busy=True; self.deliver_reaction(reactions[0]); return
            waiting = next((c for c in cycles if c['status']=='ready' and c['phase']=='experience' and
                            'outcome' in c['value']['results']), None)
            if waiting:
                self.import_comments(suspended_cycle_id=waiting['id'])
                visit = self.get('channel_visit') or {}
                if visit.get('away_until', 0) <= self.now() and self.focus():
                    if not visit or visit.get('away_until', 0):
                        with self.store.lock, self.store.db:
                            self.put('channel_visit', {'started': self.now(), 'turns': 0, 'away_until': 0})
                    self.channel_cycle(suspended_cycle_id=waiting['id'])
                    return
                # A long physical action must not suppress the actor's own channel.
                # Use a separate persisted check; never move the accepted action deadline.
                with self.store.lock:
                    goal = self.store.db.execute("SELECT key FROM personal_intentions WHERE actor_id=? AND kind='post' AND status='pending' AND due<=? ORDER BY due LIMIT 1", (self.actor_id, self.now())).fetchone()
                requested = self.get('own_channel_check_requested')
                next_check = self.get('waiting_channel_next_check')
                due = (0 if goal else status['next_wake']) if next_check is None else next_check
                if requested or self.now() >= due:
                    self.focus(goal[0] if goal else None, own_channel=True)
                    with self.store.lock, self.store.db:
                        self.channel_cycle(suspended_cycle_id=waiting['id'])
                        self.put('waiting_channel_next_check', self.now()+self.get('life_controls')['wake_minutes']*60)
                        self.put('own_channel_check_requested', False)
                    return
            self.current_phase='living';return
        if pending:
            self.lab.busy=True;self.deliver(pending[0]);return
        if reactions:
            self.lab.busy=True;self.deliver_reaction(reactions[0]);return
        if cycles:
            cycle=cycles[0]
            if cycle['status']!='ready':return
            self.current_phase=cycle['phase'];self.lab.busy=True
            if cycle['phase']=='share':
                goal=(self.get('channel_attention') or {}).get('fulfill_intent') if cycle['value'].get('channel_visit') else None
                self.focus(goal,own_channel=not cycle['value'].get('channel_visit') or cycle['value'].get('bootstrap',False) or cycle['value'].get('channel_purpose')=='own_channel')
            self.lab.session.step(cycle['id']);updated=self.store.cycle(cycle['id'])
            if updated['status']=='complete':
                if cycle['value'].get('channel_visit'):
                    if (self.get('channel_attention') or {}).get('purpose')=='comments':self.finish_visit_turn()
                else:
                    with self.store.lock,self.store.db:
                        status=self.get('life_status')
                        status.update(last_cycle=cycle['id'],next_wake=self.now()+self.get('life_controls')['wake_minutes']*60)
                        self.put('life_status',status)
            return
        self.import_comments()
        if self.get('own_channel_check_requested'):
            self.focus(own_channel=True)
            with self.store.lock,self.store.db:
                self.channel_cycle()
                self.put('own_channel_check_requested',False)
            return
        # Own life has its own deadline. Chat replies cannot move it into the future.
        if self.now()>=status['next_wake'] and self.get('bootstrap_prepared'):
            with self.store.lock,self.store.db:self.put('channel_attention',{})
            self.lab.session.prepare(self.actor_id,minutes=max(0,min(1440,self.minute_now()-self.store.get('minute'))))
            return
        with self.store.lock:
            goal=self.store.db.execute("SELECT key FROM personal_intentions WHERE actor_id=? AND kind='post' AND status='pending' AND due<=? ORDER BY due LIMIT 1",(self.actor_id,self.now())).fetchone()
            visit=self.get('channel_visit') or {}
        if goal:
            self.focus(goal[0]);self.channel_cycle();return
        if not self.get('bootstrap_prepared'):
            self.focus(own_channel=True);self.channel_cycle(bootstrap=True);return
        if visit.get('away_until',0)>self.now():
            self.current_phase='away';return
        ids=self.focus()
        if ids:
            with self.store.lock,self.store.db:
                if not visit or visit.get('away_until',0):self.put('channel_visit',{'started':self.now(),'turns':0,'away_until':0})
            self.channel_cycle()
        else:self.current_phase='waiting'

    def tick(self):
        if not self.active_lock.acquire(blocking=False):
            return
        try:
            status = self.get('life_status')
            if status['status'] != 'running' or self.store.get('paused'):
                return
            if self.connection.poll_blocked:
                return self.block('Приём Telegram остановлен. Следующие публикации и вызовы на паузе.')
            self.sync_world_profile()
            if self.social:
                return self.tick_social(status)
            with self.store.lock:
                uncertain = self.store.db.execute("SELECT 1 FROM channel_jobs WHERE status='unknown'").fetchone()
                cycles = [self.store.cycle(r[0]) for r in self.store.db.execute(
                    "SELECT id FROM world_cycles WHERE status IN ('ready','running','blocked') ORDER BY rowid LIMIT 1")]
                pending = [{**dict(r), 'value': json.loads(r['value'])} for r in self.store.db.execute(
                    "SELECT * FROM world_events WHERE stage='post' AND id NOT IN (SELECT post_id FROM channel_jobs WHERE status IN ('sent','local_only','rejected')) ORDER BY rowid LIMIT 1")]
            if uncertain or (cycles and cycles[0]['status'] == 'blocked'):
                return self.block('Есть незавершённое действие. Автоматический повтор не выполняется.')
            # Wait for the fictional action's actual elapsed time before sharing its outcome.
            if self.store.get('minute') > self.minute_now() and not (cycles and cycles[0]['value'].get('bootstrap')):
                self.current_phase = 'living'
                return
            if pending:
                self.lab.busy = True
                self.deliver(pending[0])
                return
            if cycles:
                cycle = cycles[0]
                if cycle['status'] != 'ready':
                    return
                self.current_phase, self.lab.busy = cycle['phase'], True
                self.lab.session.step(cycle['id'])
                updated = self.store.cycle(cycle['id'])
                if updated['status'] == 'complete':
                    with self.store.lock, self.store.db:
                        status = self.get('life_status')
                        status.update(last_cycle=cycle['id'], next_wake=self.now() + self.get('life_controls')['wake_minutes'] * 60)
                        self.put('life_status', status)
                        self.store.db.execute('UPDATE channel_read_batches SET considered=1')
                return
            self.import_comments()
            with self.store.lock:
                unread = self.store.db.execute('SELECT 1 FROM channel_read_batches WHERE considered=0').fetchone()
            if not self.get('bootstrap_prepared'):
                self.channel_cycle(bootstrap=True)
            elif unread:
                self.channel_cycle()
            elif self.now() >= status['next_wake']:
                elapsed = max(1, min(1440, self.minute_now() - self.store.get('minute')))
                self.lab.session.prepare(self.actor_id, minutes=elapsed)
            else:
                self.current_phase = 'waiting'
        except Exception as error:
            with self.store.lock, self.store.db:
                self.put('life_error_detail', {'type': type(error).__name__, 'phase': self.current_phase,
                                                    'telegram_code': error.code if isinstance(error, TelegramError) else None})
            from .role_boundary import RoleBoundaryError
            from .codex import CodexError
            message = ('Реплика вышла из роли. Отправка заблокирована; текст сохранён для разбора.'
                       if isinstance(error, RoleBoundaryError) else 'Модель не завершила реплику. На этом этапе отправки в Telegram не было. '
                       'Несостоявшуюся реплику можно пропустить после проверки; автоматического повтора нет.'
                       if isinstance(error, CodexError) and self.current_phase == 'share' else
                       'Жизнь остановлена после ошибки. Память и результат сохранены; автоматического повтора нет.')
            self.block(str(error) if isinstance(error, TelegramError) else message)
        finally:
            self.lab.busy = False
            self.active_lock.release()

    def loop(self, stopping):
        while not stopping.wait(1):
            self.tick()


class AwakeGuard:
    """Scoped assertion; hours=0 holds while this process runs, even on error."""
    def __init__(self, life, hours=6):
        self.life = life
        self.deadline = time.time() + hours * 3600 if hours else None
        self.child = None

    def tick(self):
        wanted = sys.platform == 'darwin' and (self.deadline is None or
                 (not self.life.store.get('paused') and time.time() < self.deadline))
        if self.child and (not wanted or self.child.poll() is not None):
            self.close()
        if wanted and self.child is None:
            options = ['-i', '-s'] if self.deadline is None else ['-i', '-t', str(max(1, int(self.deadline-time.time())))]
            self.child = subprocess.Popen(['/usr/bin/caffeinate', *options, '-w', str(os.getpid())],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self.life.store.lock, self.life.store.db:
            status = {'active': self.child is not None, 'until': self.deadline,
                      'pid': self.child.pid if self.child else None}
            if status != self.life.get('awake_status'):
                self.life.put('awake_status', status)

    def close(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)
        self.child = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--telegram-dir', type=Path, required=True)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--persona', type=Path)
    parser.add_argument('--speech-contract', type=Path)
    parser.add_argument('--environment', type=Path)
    parser.add_argument('--world-port', type=int)
    parser.add_argument('--telegram-port', type=int)
    parser.add_argument('--lively-world', action='store_true', default=None,
                        help='enable incident dynamics after the current episode; persisted across restarts')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--no-photos', action='store_true')
    parser.add_argument('--awake-hours', type=int, default=6)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    if args.profile and (args.persona or args.speech_contract or args.environment):
        parser.error('--profile includes persona, contract and environment; overrides are not allowed')
    if not args.profile and (not args.persona or not args.speech_contract):
        parser.error('supply --profile or both --persona and --speech-contract')
    from .character_profile import CharacterProfile
    from .profile_host import open_world, bind_connection, effective_profile
    profile = effective_profile(args.data_dir, CharacterProfile.load(args.profile)) if args.profile else None
    actor = profile.actor_id if profile else 'garik'
    persona, contract = ((profile.persona, profile.speech_contract) if profile else
                         (args.persona.read_text(), args.speech_contract.read_text()))
    if not persona.strip() or not contract.strip():
        parser.error('empty persona or speech contract')
    root = Path(__file__).resolve().parent.parent
    interpreter=live_interpreter(root,sys.executable,sys.platform)
    if interpreter:
        os.execv(interpreter,[interpreter,'-m','persona_runtime.channel_life',*(argv if argv is not None else sys.argv[1:])])
    directory = args.data_dir.expanduser().resolve()
    telegram_directory = args.telegram_dir.expanduser().resolve()
    if root / '.local' not in directory.parents or not 0 <= args.awake_hours <= 12:
        parser.error('live world must be under .local/; awake duration is 1–12 hours, or 0 while process runs')
    if telegram_directory == directory or (root in telegram_directory.parents and
            not any(p in telegram_directory.parents for p in (root / '.local', root / '.runtime'))):
        parser.error('use a separate local Telegram data directory')
    world_port = args.world_port if args.world_port is not None else (8866 if profile else 8766)
    telegram_port = args.telegram_port if args.telegram_port is not None else (8867 if profile else 8767)
    if (not 0 <= world_port <= 65535 or not 0 <= telegram_port <= 65535 or
            world_port == telegram_port and world_port != 0):
        parser.error('invalid or conflicting panel ports')
    if args.background:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        arguments = list(argv if argv is not None else sys.argv[1:])
        arguments.remove('--background')
        fd = os.open(directory / 'live.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'persona_runtime.channel_life', *arguments],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        # Initial binding performs several bounded read-only Telegram calls.
        # A short local readiness timer must not kill a healthy slow binding.
        deadline = time.monotonic()+180
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError('life process stopped before readiness')
            try:
                value=json.loads((directory/'live.json').read_text())
                if value['pid']==process.pid:
                    print(json.dumps(value),flush=True)
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.1)
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError('life startup readiness timeout')
    from contextlib import ExitStack
    from .proxy import SharedTransport
    from .world_backend import WorldBackend
    from .world_image_backend import CodexImageBackend
    stopping = threading.Event()
    with ExitStack() as resources:
        store = open_world(directory, profile)
        resources.callback(store.close)
        owner = SharedTransport()
        resources.callback(owner.close)
        from .telegram import BotAPI
        connection = TelegramConnection(telegram_directory,api_factory=lambda token:BotAPI(token,transport_factory=owner.borrow))
        resources.callback(connection.close)
        bind_connection(connection, actor, directory)
        if connection.get('binding') is None:
            raise TelegramError('Telegram ещё не привязан к каналу.')
        connection.bind(connection.get('binding')['channel']['id'])
        if args.fresh:
            fresh_start(store, connection, actor_id=actor,
                        environment=json.loads(args.environment.read_text()) if args.environment else ())
        if not store.actor_get(actor, 'channel_epoch'):
            raise ValueError('uninitialized channel life; use --fresh only for a new world')
        if args.no_photos:
            with store.lock, store.db:
                store.actor_put(actor, 'life_controls', {**store.actor_get(actor, 'life_controls'), 'photos': False})
        model_settings = profile.runtime if profile else {}
        backend = WorldBackend(directory/'transport', transport_factory=owner.borrow, **model_settings)
        resources.callback(backend.close)
        research = WorldBackend(directory/'research-transport', research_host=True,
                                transport_factory=owner.borrow, max_known_tokens=50000, **model_settings)
        resources.callback(research.close)
        renderer = CodexImageBackend(directory/'image-transport', transport_factory=owner.borrow, **model_settings)
        resources.callback(renderer.close)
        session = WorldSession(store, backend, {actor: persona}, contract, autonomous_posts=True, personal_life=True,
                               free_intentions=True, research_enabled=True, research_backend=research, lossless_intentions=True,
                               active_world=True, direct_channel=True, continuous=True, external_channel=True, cancel=stopping)
        lab = Laboratory(store, session)
        lab.profile = profile
        life = ChannelLife(lab, connection, persona, renderer, social_enabled=True,
                           references=profile.references if profile else None, lively_world=args.lively_world, recall_enabled=True)
        setup = Setup(connection, life=life)
        servers, threads, serving, awake = [], [], [], AwakeGuard(life, args.awake_hours)
        try:
            for port, serve in ((world_port, world_handler(lab)), (telegram_port, setup_handler(setup))):
                server = LocalServer(('127.0.0.1', port), serve)
                servers.append(server)
            if args.start:
                life.action(True)
            connection.start()
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
                thread.start()
                serving.append(server)
                threads.append(thread)
            worker = threading.Thread(target=life.loop, args=(stopping,), name=actor+'-live-life', daemon=True)
            worker.start()
            threads.append(worker)
            world_port, telegram_port = [s.server_port for s in servers]
            lab.world_url = 'http://127.0.0.1:'+str(world_port)
            lab.telegram_url = 'http://127.0.0.1:'+str(telegram_port)
            descriptor = {'pid': os.getpid(), 'actor_id': actor, 'name': lab.actor_name,
                          'world_url': 'http://127.0.0.1:'+str(world_port),
                          'telegram_url': 'http://127.0.0.1:'+str(telegram_port),
                          'data': str(directory), 'model_enabled': True, 'python':sys.executable, 'sqlite_version':sqlite3.sqlite_version}
            private_json(directory/'live.json', descriptor)
            private_json(directory/'lab.json', {'pid': os.getpid(), 'port': world_port, 'model_enabled': True})
            private_json(connection.directory/'panel.json', {'pid': os.getpid(), 'port': telegram_port})
            print(json.dumps(descriptor, ensure_ascii=False), flush=True)
            def stop(*unused):
                stopping.set()
                store.pause(True)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while not stopping.wait(2):
                awake.tick()
        finally:
            stopping.set()
            store.pause(True)
            awake.close()
            for server in serving:
                server.shutdown()
            for server in servers:
                server.server_close()
            if 'worker' in locals():
                worker.join(40)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Жизнь остановлена. Память сохранена; подробности ошибок и секреты скрыты.', file=sys.stderr)
        sys.exit(1)
