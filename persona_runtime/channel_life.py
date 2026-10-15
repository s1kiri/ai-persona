"""One explicitly enabled Telegram life, with persistent memory and voluntary posts."""
import argparse
from datetime import datetime
import hashlib
import json
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


def fresh_start(store, connection, now=None, environment=()):
    """Seed a NEW history only. Never delete or inherit old experimental events."""
    now = time.time() if now is None else now
    binding = connection.get('binding')
    if not binding:
        raise TelegramError('Канал и обсуждение должны быть подключены до запуска жизни.')
    with store.lock, store.db:
        if store.get('channel_epoch') or store.db.execute('SELECT 1 FROM world_cycles').fetchone():
            raise WorldConflict('fresh start requires an unused world; existing history was not reset')
        minute = datetime.fromtimestamp(now, MOSCOW).hour * 60 + datetime.fromtimestamp(now, MOSCOW).minute
        store.put('minute', minute)
        store.put('channel_epoch', {'wall': now, 'minute': minute})
        store.put('channel_start', {'desire': START_DESIRE, 'basis': 'author_requested_starting_desire'})
        store.put('channel_connection', {
            'channel_name': binding['channel'].get('title', 'Свой канал'),
            'available': True,
            'explanation': 'Канал уже создан, бот подключён и получил права. Создавать его заново, просить пароль или токен не надо. '
                           'Ты можешь написать готовый пост, ответить на доступный комментарий или оставить мысль при себе. '
                           'Выбранные тексты хост отправляет дословно; успешная доставка отмечается квитанцией. '
                           'Можно иногда выбрать фотографию наблюдаемого предмета через подключённый инструмент. '
                           'Это твой канал, не рабочий отчёт: свободный формат и мат допустимы. '
                           'Обязательного графика и нормы постов нет. Комментарии реальных читателей приходят отдельно; чужой совет не обязан становиться поступком.'})
        store.put('life_controls', DEFAULT_CONTROLS)
        store.put('life_status', {'status': 'paused', 'next_wake': now, 'last_cycle': None, 'error': None})
        store.put('channel_life', {'enabled': True, 'run': store.directory.name})
        settings = {**store.get('settings'), 'events_per_hour': 1.5, 'surprise': 65,
                    'quiet': 15, 'max_threads': 5, 'post_cooldown_minutes': 0}
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
    def __init__(self, lab, connection, persona, renderer=None, *, now=time.time, photo_factory=None, social_enabled=False):
        self.lab, self.store, self.connection = lab, lab.store, connection
        self.persona, self.renderer, self.now = persona, renderer, now
        self.cancel = lab.session.cancel
        self.photo_factory = photo_factory
        self.worker = None
        self.active_lock = threading.Lock()
        self.current_phase = None
        self.social = None
        with self.store.lock, self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS channel_jobs(post_id TEXT PRIMARY KEY,status TEXT NOT NULL,
                    receipts TEXT NOT NULL,photo TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS channel_comments(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                    audience_id TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS channel_read_batches(id TEXT PRIMARY KEY,considered INTEGER NOT NULL DEFAULT 0);
            ''')
            self.store.db.execute("UPDATE channel_jobs SET status='unknown',error='process stopped before full delivery; no replay' WHERE status='sending'")
            columns={r[1] for r in self.store.db.execute('PRAGMA table_info(channel_read_batches)')}
            if 'deferred_until' not in columns:
                self.store.db.execute('ALTER TABLE channel_read_batches ADD COLUMN deferred_until REAL')
            self.store.put('life_controls',{**DEFAULT_CONTROLS,**self.store.get('life_controls')})
        if social_enabled:
            from .channel_social import SocialMemory
            self.social=SocialMemory(self.store,now=self.now)
            self.lab.session.channel_extension=self.social
            self.backfill_people()

    def state(self):
        with self.store.lock:
            status = self.store.get('life_status')
            jobs = [dict(r) for r in self.store.db.execute('SELECT * FROM channel_jobs ORDER BY rowid DESC LIMIT 12')]
            for job in jobs:
                job['receipts'] = json.loads(job['receipts'])
                if job['photo']:
                    job['photo'] = json.loads(job['photo'])
            value = {**status, 'paused': self.store.get('paused'), 'busy': self.lab.busy,
                    'phase': self.current_phase, 'controls': self.store.get('life_controls'),
                    'world_calls': self.store.monitor()['call_count'], 'jobs': jobs,
                    'awake': self.store.get('awake_status'), 'cadence': 'Свободно: графика и обязательных постов нет.'}
            if self.social:
                value.update(self.social.context())
                value['attention']=self.store.get('channel_visit')
            return value

    def configure(self, value):
        checked = controls_checked({**self.store.get('life_controls'),**value})
        with self.store.lock, self.store.db:
            self.store.put('life_controls', checked)

    def action(self, running):
        if type(running) is not bool:
            raise ValueError('invalid life action')
        with self.store.lock, self.store.db:
            status = self.store.get('life_status')
            if running:
                if status['status'] == 'error' or self.store.db.execute("SELECT 1 FROM channel_jobs WHERE status IN ('unknown','sending')").fetchone():
                    raise TelegramError('Есть незавершённая операция. Нужна проверка, автоматического повтора нет.')
                if self.connection.poll_blocked or self.connection.get('binding') is None:
                    raise TelegramError('Сначала восстанови подключение Telegram.')
                status.update(status='running', next_wake=min(status['next_wake'], self.now()))
            else:
                status['status'] = 'paused'
            self.store.put('life_status', status)
            self.store.put('paused', not running)

    def block(self, message):
        with self.store.lock, self.store.db:
            status = self.store.get('life_status')
            status.update(status='error', error=message)
            self.store.put('life_status', status)
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
            repairs = self.store.get('delivery_repairs') or []
            repairs.append({'post_id': post_id, 'basis': 'owner_explicitly_checked_absence_in_channel',
                            'at': self.now(), 'previous_job': dict(job)})
            self.store.put('delivery_repairs', repairs)
            self.store.db.execute("UPDATE channel_jobs SET status='reviewed_absent',error='owner checked absence; new explicit attempt allowed' WHERE post_id=?", (post_id,))
            status = self.store.get('life_status')
            status.update(status='paused', error=None)
            self.store.put('life_status', status)
            self.lab.failure = None

    def minute_now(self):
        epoch = self.store.get('channel_epoch')
        return epoch['minute'] + max(0, int((self.now() - epoch['wall']) // 60))

    def channel_cycle(self, *, bootstrap=False):
        if self.social:
            import uuid
            from .world import director
            with self.store.lock,self.store.db:
                if self.store.db.execute("SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked')").fetchone():
                    raise WorldConflict('another stage is active')
                self.store.put('minute',max(self.store.get('minute'),self.minute_now()))
                self.store.put('revision',self.store.get('revision')+1)
                cid='cycle-'+uuid.uuid4().hex
                value={'brief':director(self.store.state(),'garik',1,1),'inputs':{},'results':{},
                       'personal_life':self.lab.session.personal_life,'lossless_intentions':self.lab.session.lossless_intentions,
                       'direct_channel':True,'active_world':self.lab.session.active_world,'entry_phase':'share',
                       'bootstrap':bootstrap,'channel_visit':True,
                       'channel_purpose':(self.store.get('channel_attention') or {}).get('purpose','comments')}
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                      (cid,'garik','ready','share',self.store.get('revision'),json.dumps(value),self.now()))
                self.store.put('bootstrap_prepared',True)
                self.store.snapshot()
                return self.store.cycle(cid)
        cycle = self.lab.session.prepare('garik', minutes=1, draw=1)
        with self.store.lock, self.store.db:
            value = self.store.cycle(cycle['id'])['value']
            value.update(bootstrap=bootstrap, entry_phase='share')
            self.store.db.execute("UPDATE world_cycles SET phase='share',value=? WHERE id=?",
                                  (json.dumps(value, ensure_ascii=False), cycle['id']))
            self.store.put('bootstrap_prepared', True)
        return self.store.cycle(cycle['id'])

    def import_comments(self):
        """Only replies to posts delivered by this fresh life enter its memory."""
        with self.connection.lock:
            group = self.connection.get('binding')['discussion']['id']
            rows = [dict(r) for r in self.connection.db.execute(
                "SELECT m.*,f.post_id AS telegram_post FROM messages m JOIN forwards f ON f.group_id=m.chat_id AND f.root_id=m.root_id "
                "WHERE m.chat_id=? AND m.kind='comment' ORDER BY m.received,m.id", (group,))]
        with self.store.lock, self.store.db:
            delivered = {}
            for job in self.store.db.execute("SELECT post_id,receipts FROM channel_jobs WHERE status='sent'"):
                for receipt in json.loads(job['receipts']):
                    if receipt['chat_id'] == self.connection.get('binding')['channel']['id']:
                        delivered[receipt['message_id']] = job['post_id']
            count = 0
            for row in rows:
                if row['telegram_post'] not in delivered or self.store.db.execute(
                        'SELECT 1 FROM channel_comments WHERE chat_id=? AND message_id=?', (group, row['id'])).fetchone():
                    continue
                audience_id = 'tg-comment-' + str(row['id'])
                cycle_id = 'received-' + str(row['id'])
                value = {'post_id': delivered[row['telegram_post']],
                         'comments': [{'speaker': row['speaker'][:60], 'text': row['text']}],
                         'simulated': False, 'telegram': {'chat_id': group, 'message_id': row['id'],
                                                        'post_id': row['telegram_post'], 'received': row['received']}}
                if self.social:
                    from .channel_social import person_key
                    key=person_key(row,group)
                    value['person_key']=key
                    self.social.register(row['speaker'][:60],key,row['user_id'] is None)
                cycle_value = {'results': {'audience': value}, 'inputs': {}, 'external_receipt': True}
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                      (cycle_id, 'garik', 'complete', 'done', self.store.get('revision'), json.dumps(cycle_value, ensure_ascii=False), row['received']))
                self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                      (audience_id, cycle_id, 'garik', 'audience', self.store.get('minute'), json.dumps(value, ensure_ascii=False)))
                self.store.db.execute('INSERT INTO channel_comments VALUES(?,?,?)', (group, row['id'], audience_id))
                self.store.db.execute('INSERT INTO channel_read_batches(id) VALUES(?)', (audience_id,))
                count += 1
            if count:
                self.store.put('revision', self.store.get('revision') + 1)
                self.store.snapshot()
            return count

    def photo(self, post):
        if not self.store.get('life_controls')['photos'] or self.renderer is None or post['value'].get('reply_to'):
            return None
        from .world_photo import photo_context
        cycle = self.store.cycle(post['cycle_id'])
        state = self.store.state()
        context = photo_context('garik', state['entities'], state['history'],
                                cycle['value']['results'].get('experience', {}).get('note', ''))
        if not context['sources']:
            return None  # An initial thought has no occurred object scene to photograph.
        context['ready_post'] = post['value']['text']
        directory = self.store.directory / 'photos' / post['id']
        from .world_media import MediaReferenceLibrary
        from .photo_case import AutonomousPhotoCase
        library = MediaReferenceLibrary(self.store.directory / 'visual-memory')
        try:
            factory = self.photo_factory or AutonomousPhotoCase
            case = factory(directory, self.lab.session.backend, self.renderer, self.persona,
                           reference_library=library, cancel=self.cancel)
            return case.run(context)
        finally:
            library.close()

    def deliver(self, post):
        with self.store.lock, self.store.db:
            old = self.store.db.execute('SELECT status FROM channel_jobs WHERE post_id=?', (post['id'],)).fetchone()
            if old:
                if old[0] == 'sent':
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
        image = image_result['image'] if image_result and image_result['status'] == 'passed' else None
        reply_to = None
        if post['value'].get('reply_to'):
            audience = next(e for e in self.store.channel_context('garik') if e['id'] == post['value']['audience_id'])
            if audience['value'].get('simulated') is not False:
                raise TelegramError('Вымышленный комментарий не может получить настоящий ответ.')
            reply_to = audience['value']['telegram']['message_id']
        self.current_phase = 'delivery'
        text = post['value']['text']
        receipts = []
        key = 'life:' + self.store.directory.name + ':' + post['id']
        attempts = sum(r['post_id'] == post['id'] for r in (self.store.get('delivery_repairs') or []))
        if attempts:
            key += ':review' + str(attempts)
        if image and len(text) <= 1024:
            receipts.append(self.connection.send(key + ':photo', text, photo=Path(image['image_path']).read_bytes()))
        else:
            for index, part in enumerate(split_text(text)):
                receipts.append(self.connection.send(key + ':part' + str(index), part, reply_to=reply_to))
            if image:
                receipts.append(self.connection.send(key + ':photo', '', photo=Path(image['image_path']).read_bytes()))
        compact = [{k: r[k] for k in ('chat_id', 'message_id', 'key', 'status')} for r in receipts]
        photo_summary = {'status': image_result['status'], 'choice': image_result['choice']} if image_result else None
        if image:
            from .world_media import MediaReferenceLibrary
            library = MediaReferenceLibrary(self.store.directory / 'visual-memory')
            try:
                library.register(image, 'garik', image_result['choice']['framing'], source='channel',
                                 channel_post_id=str(receipts[-1]['message_id']))
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
            start = self.store.get('channel_start')
            if start and not start.get('fulfilled') and not post['value'].get('reply_to'):
                self.store.put('channel_start', {**start, 'fulfilled': True,
                                                 'first_post': post['id'], 'delivery': compact})
            self.store.snapshot()

    def backfill_people(self):
        from .channel_social import person_key
        with self.connection.lock:
            messages={(r['chat_id'],r['id']):dict(r) for r in self.connection.db.execute('SELECT * FROM messages')}
        with self.store.lock,self.store.db:
            for row in self.store.db.execute("SELECT id,value FROM world_events WHERE stage='audience' AND actor_id='garik'").fetchall():
                value=json.loads(row['value']);tg=value.get('telegram',{})
                message=messages.get((tg.get('chat_id'),tg.get('message_id')))
                if not message:continue
                key=person_key(message,tg['chat_id']);value['person_key']=key
                self.social.register(message['speaker'][:60],key,message['user_id'] is None)
                self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value,ensure_ascii=False),row['id']))

    def focus(self, fulfill=None, *, own_channel=False):
        if own_channel or fulfill:
            with self.store.lock,self.store.db:
                self.store.put('channel_attention',{'purpose':'own_channel','focus_ids':[],
                    'fulfill_intent':fulfill,'allowed_reactions':[]})
            return []
        with self.store.lock:
            ids=[r[0] for r in self.store.db.execute(
                'SELECT id FROM channel_read_batches WHERE considered=0 AND (deferred_until IS NULL OR deferred_until<=?) ORDER BY rowid LIMIT 8',(self.now(),))]
            cache=self.store.get('reaction_capabilities')
        if not ids and not fulfill:
            with self.store.lock,self.store.db:
                if self.store.get('channel_attention')!={'focus_ids':[],'fulfill_intent':None,'allowed_reactions':[]}:
                    self.store.put('channel_attention',{'focus_ids':[],'fulfill_intent':None,'allowed_reactions':[]})
            return []
        if not cache or self.now()-cache['checked']>300:
            cache={'checked':self.now(),'emojis':self.connection.reaction_options()}
        with self.store.lock,self.store.db:
            self.store.put('reaction_capabilities',cache)
            self.store.put('channel_attention',{'purpose':'comments','focus_ids':ids,'fulfill_intent':fulfill,'allowed_reactions':cache['emojis']})
        return ids

    def finish_visit_turn(self):
        with self.store.lock,self.store.db:
            visit=self.store.get('channel_visit') or {'started':self.now(),'turns':0,'away_until':0}
            if visit.get('away_until',0)>self.now():
                visit={'started':self.now(),'turns':0,'away_until':0}
            visit['turns']+=1
            if (self.store.get('visit_leave_requested') or visit['turns']>=self.store.get('life_controls')['visit_turns'] or
                    self.now()-visit['started']>=120):
                visit['away_until']=self.now()+self.store.get('life_controls')['away_minutes']*60
            self.store.put('channel_visit',visit)

    def deliver_reaction(self,row):
        event=self.social.evidence()[row['audience_id']]
        if event['value'].get('simulated') is not False:raise TelegramError('Реакции доступны только на реальные комментарии.')
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE channel_reaction_jobs SET status='sending' WHERE id=?",(row['id'],))
        self.current_phase='reaction'
        receipt=self.connection.react('reaction:'+self.store.directory.name+':'+row['id'],event['value']['telegram']['message_id'],row['emoji'])
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE channel_reaction_jobs SET status='sent',receipt=? WHERE id=?",(json.dumps(receipt),row['id']))

    def tick_social(self,status):
        if not self.store.get('social_review_complete'):
            self.current_phase='memory';self.lab.busy=True
            return self.social.bootstrap(self.lab.session.backend,self.persona,self.cancel)
        with self.store.lock:
            uncertain=self.store.db.execute("SELECT 1 FROM channel_jobs WHERE status='unknown' UNION ALL SELECT 1 FROM channel_reaction_jobs WHERE status IN ('sending','unknown')").fetchone()
            cycles=[self.store.cycle(r[0]) for r in self.store.db.execute("SELECT id FROM world_cycles WHERE status IN ('ready','running','blocked') ORDER BY rowid LIMIT 1")]
            pending=[{**dict(r),'value':json.loads(r['value'])} for r in self.store.db.execute(
                "SELECT * FROM world_events WHERE stage='post' AND id NOT IN (SELECT post_id FROM channel_jobs WHERE status='sent') ORDER BY rowid LIMIT 1")]
            reactions=[dict(r) for r in self.store.db.execute("SELECT * FROM channel_reaction_jobs WHERE status='pending' ORDER BY rowid LIMIT 1")]
        if uncertain or (cycles and cycles[0]['status']=='blocked'):
            return self.block('Есть незавершённая операция. Автоматического повтора нет.')
        if self.store.get('minute')>self.minute_now() and not (cycles and cycles[0]['value'].get('bootstrap')):
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
                goal=(self.store.get('channel_attention') or {}).get('fulfill_intent') if cycle['value'].get('channel_visit') else None
                self.focus(goal,own_channel=not cycle['value'].get('channel_visit') or cycle['value'].get('bootstrap',False) or cycle['value'].get('channel_purpose')=='own_channel')
            self.lab.session.step(cycle['id']);updated=self.store.cycle(cycle['id'])
            if updated['status']=='complete':
                if cycle['value'].get('channel_visit'):
                    if (self.store.get('channel_attention') or {}).get('purpose')=='comments':self.finish_visit_turn()
                else:
                    with self.store.lock,self.store.db:
                        status=self.store.get('life_status')
                        status.update(last_cycle=cycle['id'],next_wake=self.now()+self.store.get('life_controls')['wake_minutes']*60)
                        self.store.put('life_status',status)
            return
        self.import_comments()
        if self.store.get('own_channel_check_requested'):
            self.focus(own_channel=True)
            with self.store.lock,self.store.db:
                self.channel_cycle()
                self.store.put('own_channel_check_requested',False)
            return
        # Own life has its own deadline. Chat replies cannot move it into the future.
        if self.now()>=status['next_wake'] and self.store.get('bootstrap_prepared'):
            with self.store.lock,self.store.db:self.store.put('channel_attention',{})
            self.lab.session.prepare('garik',minutes=max(0,min(1440,self.minute_now()-self.store.get('minute'))))
            return
        with self.store.lock:
            goal=self.store.db.execute("SELECT key FROM personal_intentions WHERE actor_id='garik' AND kind='post' AND status='pending' AND due<=? ORDER BY due LIMIT 1",(self.now(),)).fetchone()
            visit=self.store.get('channel_visit') or {}
        if goal:
            self.focus(goal[0]);self.channel_cycle();return
        if not self.store.get('bootstrap_prepared'):
            self.focus(own_channel=True);self.channel_cycle(bootstrap=True);return
        if visit.get('away_until',0)>self.now():
            self.current_phase='away';return
        ids=self.focus()
        if ids:
            with self.store.lock,self.store.db:
                if not visit or visit.get('away_until',0):self.store.put('channel_visit',{'started':self.now(),'turns':0,'away_until':0})
            self.channel_cycle()
        else:self.current_phase='waiting'

    def tick(self):
        if not self.active_lock.acquire(blocking=False):
            return
        try:
            status = self.store.get('life_status')
            if status['status'] != 'running' or self.store.get('paused'):
                return
            if self.connection.poll_blocked:
                return self.block('Приём Telegram остановлен. Следующие публикации и вызовы на паузе.')
            if self.social:
                return self.tick_social(status)
            with self.store.lock:
                uncertain = self.store.db.execute("SELECT 1 FROM channel_jobs WHERE status='unknown'").fetchone()
                cycles = [self.store.cycle(r[0]) for r in self.store.db.execute(
                    "SELECT id FROM world_cycles WHERE status IN ('ready','running','blocked') ORDER BY rowid LIMIT 1")]
                pending = [{**dict(r), 'value': json.loads(r['value'])} for r in self.store.db.execute(
                    "SELECT * FROM world_events WHERE stage='post' AND id NOT IN (SELECT post_id FROM channel_jobs WHERE status='sent') ORDER BY rowid LIMIT 1")]
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
                        status = self.store.get('life_status')
                        status.update(last_cycle=cycle['id'], next_wake=self.now() + self.store.get('life_controls')['wake_minutes'] * 60)
                        self.store.put('life_status', status)
                        self.store.db.execute('UPDATE channel_read_batches SET considered=1')
                return
            self.import_comments()
            with self.store.lock:
                unread = self.store.db.execute('SELECT 1 FROM channel_read_batches WHERE considered=0').fetchone()
            if not self.store.get('bootstrap_prepared'):
                self.channel_cycle(bootstrap=True)
            elif unread:
                self.channel_cycle()
            elif self.now() >= status['next_wake']:
                elapsed = max(1, min(1440, self.minute_now() - self.store.get('minute')))
                self.lab.session.prepare('garik', minutes=elapsed)
            else:
                self.current_phase = 'waiting'
        except Exception as error:
            with self.store.lock, self.store.db:
                self.store.put('life_error_detail', {'type': type(error).__name__, 'phase': self.current_phase,
                                                    'telegram_code': error.code if isinstance(error, TelegramError) else None})
            self.block(str(error) if isinstance(error, TelegramError) else
                       'Жизнь остановлена после ошибки. Память и результат сохранены; автоматического повтора нет.')
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
            if status != self.life.store.get('awake_status'):
                self.life.store.put('awake_status', status)

    def close(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)
        self.child = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--telegram-dir', type=Path, required=True)
    parser.add_argument('--persona', type=Path, required=True)
    parser.add_argument('--speech-contract', type=Path, required=True)
    parser.add_argument('--environment', type=Path)
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--awake-hours', type=int, default=6)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    directory = args.data_dir.expanduser().resolve()
    if root / '.local' not in directory.parents or not 0 <= args.awake_hours <= 12:
        parser.error('live world must be under .local/; awake duration is 1–12 hours, or 0 while process runs')
    if args.background:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        arguments = list(argv if argv is not None else sys.argv[1:])
        arguments.remove('--background')
        fd = os.open(directory / 'live.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'persona_runtime.channel_life', *arguments],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic()+12
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
        raise RuntimeError('life startup readiness timeout')
    from .proxy import SharedTransport
    from .world_backend import WorldBackend
    from .world_image_backend import CodexImageBackend
    store = WorldStore(directory)
    connection = TelegramConnection(args.telegram_dir)
    if connection.get('binding') is None:
        raise TelegramError('Telegram ещё не привязан к каналу.')
    connection.bind(connection.get('binding')['channel']['id'])
    owner = SharedTransport()
    stopping = threading.Event()
    backend = WorldBackend(directory/'transport', transport_factory=owner.borrow)
    research = WorldBackend(directory/'research-transport', research_host=True, transport_factory=owner.borrow, max_known_tokens=50000)
    renderer = CodexImageBackend(directory/'image-transport', transport_factory=owner.borrow)
    persona, contract = args.persona.read_text(), args.speech_contract.read_text()
    if not persona.strip() or not contract.strip():
        raise ValueError('empty persona')
    if args.fresh:
        fresh_start(store, connection, environment=json.loads(args.environment.read_text()) if args.environment else ())
    if not store.get('channel_epoch'):
        raise ValueError('uninitialized channel life')
    session = WorldSession(store, backend, {'garik': persona}, contract, autonomous_posts=True, personal_life=True,
                           free_intentions=True, research_enabled=True, research_backend=research, lossless_intentions=True,
                           active_world=True, direct_channel=True, continuous=True, external_channel=True, cancel=stopping)
    lab = Laboratory(store, session)
    life = ChannelLife(lab, connection, persona, renderer, social_enabled=True)
    setup = Setup(connection, life=life)
    servers, threads, awake = [], [], AwakeGuard(life, args.awake_hours)
    try:
        for port, serve in ((8766, world_handler(lab)), (8767, setup_handler(setup))):
            server = LocalServer(('127.0.0.1', port), serve)
            servers.append(server)
        if args.start:
            life.action(True)
        connection.start()
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
            thread.start()
            threads.append(thread)
        worker = threading.Thread(target=life.loop, args=(stopping,), name='garik-live-life', daemon=True)
        worker.start()
        threads.append(worker)
        descriptor = {'pid': os.getpid(), 'world_url': 'http://127.0.0.1:8766', 'telegram_url': 'http://127.0.0.1:8767',
                      'data': str(directory), 'model_enabled': True}
        private_json(directory/'live.json', descriptor)
        private_json(directory/'lab.json', {'pid': os.getpid(), 'port': 8766, 'model_enabled': True})
        private_json(connection.directory/'panel.json', {'pid': os.getpid(), 'port': 8767})
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
        for server in servers:
            server.shutdown()
            server.server_close()
        if 'worker' in locals():
            worker.join(40)
        backend.close(); research.close(); renderer.close(); owner.close()
        connection.close(); store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Жизнь остановлена. Память сохранена; подробности ошибок и секреты скрыты.', file=sys.stderr)
        sys.exit(1)
