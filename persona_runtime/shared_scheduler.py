"""Serial model calls, independent physical deadlines, actor-owned channels and peer speech."""
import json
import threading
import time
import uuid

from .channel_life import ChannelLife
from .shared_protocol import PEER_CHOICE, PEER_INSTRUCTIONS
from .telegram import TelegramError
from .world import WorldConflict, director
from .world_protocol import validate_shape


class SharedChannelLife(ChannelLife):
    """Reuse delivery/photos/memory; scheduling and pause belong to the shared owner."""
    def minute_now(self):
        epoch = self.store.get('shared_epoch')
        return epoch['minute'] + max(0, int((self.now() - epoch['wall']) // 60))

    def state(self):
        value = super().state()
        value.update(paused=self.store.get('paused') or value['status'] != 'running', shared=True)
        return value

    def action(self, running):
        if type(running) is not bool:
            raise ValueError('invalid life action')
        with self.store.lock, self.store.db:
            status = self.get('life_status')
            if running:
                unknown = self.store.db.execute("SELECT 1 FROM peer_jobs WHERE sender=? AND status IN ('unknown','sending')", (self.actor_id,)).fetchone()
                unknown = unknown or self.store.db.execute("SELECT 1 FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE e.actor_id=? AND j.status IN ('unknown','sending')", (self.actor_id,)).fetchone()
                blocked = self.store.db.execute("SELECT 1 FROM world_cycles WHERE actor_id=? AND status IN ('blocked','running')", (self.actor_id,)).fetchone()
                if unknown or blocked or status['status'] == 'error' or self.connection.poll_blocked:
                    raise TelegramError('Сначала разберите незавершённую операцию этого персонажа.')
            status.update(status='running' if running else 'paused')
            self.put('life_status', status)
            active = any((self.store.actor_get(a['id'], 'life_status') or {}).get('status') == 'running'
                         for a in self.store.state()['actors'])
            self.store.put('paused', not active)

    def block(self, message):
        with self.store.lock, self.store.db:
            status = self.get('life_status'); status.update(status='error', error=message)
            self.put('life_status', status)
            self.lab.failure = message
            active = any((self.store.actor_get(a['id'], 'life_status') or {}).get('status') == 'running'
                         for a in self.store.state()['actors'])
            self.store.put('paused', not active)

    def confirm_absent(self, post_id):
        if self.get('life_status')['status'] == 'running' or self.lab.busy:
            raise TelegramError('Сначала поставьте этого персонажа на паузу.')
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT * FROM channel_jobs WHERE post_id=?', (post_id,)).fetchone()
            event = self.store.db.execute('SELECT actor_id FROM world_events WHERE id=?', (post_id,)).fetchone()
            if not row or row['status'] not in ('unknown','sending') or not event or event[0] != self.actor_id:
                raise TelegramError('Эта доставка не принадлежит этому персонажу или не требует разбора.')
            repairs = self.get('delivery_repairs') or []
            repairs.append({'post_id': post_id, 'basis': 'owner_explicitly_checked_absence_in_channel',
                            'at': self.now(), 'previous_job': dict(row)})
            self.put('delivery_repairs', repairs)
            self.store.db.execute("UPDATE channel_jobs SET status='reviewed_absent',error='owner checked absence; new explicit attempt allowed' WHERE post_id=?", (post_id,))
            status = self.get('life_status'); status.update(status='paused', error=None)
            self.put('life_status', status); self.lab.failure = None


class SharedScheduler:
    def __init__(self, store, lives, *, now=time.time):
        if len(lives) != 2 or len(set(lives)) != 2:
            raise ValueError('the shared host requires exactly two configured characters')
        self.store, self.lives, self.now = store, dict(lives), now
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.last_actor = store.get('shared_last_actor')

    def enabled(self, actor):
        return self.store.actor_get(actor, 'life_status')['status'] == 'running'

    def advance(self):
        epoch = self.store.get('shared_epoch')
        minute = epoch['minute'] + max(0, int((self.now()-epoch['wall'])//60))
        with self.store.lock, self.store.db:
            if minute > self.store.get('minute'):
                self.store.put('minute', minute)
                self.store.put('revision', self.store.get('revision')+1)
                self.store.snapshot()

    def circulate_posts(self):
        for row in self.store.db.execute("SELECT * FROM world_events WHERE stage='post' AND rowid>? ORDER BY rowid", (self.store.get('shared_public_floor') or 0,)).fetchall():
            post = {**dict(row), 'value': json.loads(row['value'])}
            value = post['value']
            if value.get('retracted') or value.get('delivery', {}).get('status') != 'sent':
                continue
            if value.get('reply_to'):
                self.relay_delivered_reply(post)
                continue
            for recipient in self.lives:
                if recipient != row['actor_id']:
                    self.store.relay(row['actor_id'], recipient, 'public_post', value['text'],
                        delivery_id='public:'+row['id']+':'+recipient,
                        details={'post_id': row['id'], 'receipt': value['delivery']})

    def relay_delivered_reply(self, post):
        """Bot polling does not reliably expose another bot's answer to its addressee."""
        sender=post['actor_id'];value=post['value']
        audience=self.store.db.execute("SELECT value FROM world_events WHERE id=? AND actor_id=? AND stage='audience'",
                                      (value.get('audience_id'),sender)).fetchone()
        if not audience:return
        audience=json.loads(audience[0]);tg=audience.get('telegram',{})
        if audience.get('simulated') is not False:return
        connection=self.lives[sender].connection
        with connection.lock:
            message=connection.db.execute('SELECT user_id FROM messages WHERE chat_id=? AND id=? AND kind=?',
                                          (tg.get('chat_id'),tg.get('message_id'),'comment')).fetchone()
        recipient=next((a for a,life in self.lives.items() if a!=sender and message and message[0]==life.connection.get('bot')['id']),None)
        if not recipient:return
        with self.store.lock,self.store.db:
            # This incoming peer utterance was answered with a confirmed own reply.
            for job in self.store.db.execute("SELECT id,receipt FROM peer_jobs WHERE sender=? AND recipient=? AND status='sent' AND receipt IS NOT NULL",(recipient,sender)).fetchall():
                receipt=json.loads(job['receipt'])
                if (receipt.get('chat_id'),receipt.get('message_id'))!=(tg.get('chat_id'),tg.get('message_id')):continue
                incoming=self.store.db.execute('SELECT in_event FROM peer_deliveries WHERE id=?',(job['id'],)).fetchone()
                if incoming:self.store.db.execute('INSERT OR IGNORE INTO peer_reads VALUES(?,?)',(sender,incoming[0]))
            self.store.relay(sender,recipient,'comment',value['text'],delivery_id='public-reply:'+post['id']+':'+recipient,
                             details={'post_id':value['reply_to'],'media_post_id':post['id'],'receipt':value['delivery']})

    def peer_turn(self, actor):
        life = self.lives[actor]
        cid = 'peerturn-'+uuid.uuid4().hex
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                (cid, actor, 'ready', 'peer_choice', self.store.get('revision'),
                 json.dumps({'inputs': {}, 'results': {}, 'peer_service': True}), self.now()))
        call, cycle = self.store.claim(cid, None)
        answer = None
        try:
            payload = life.social.with_source_ids({'life': self.store.actor_view(actor),
                       'peer_context': self.store.peer_context(actor),
                       'clock': {'minute': self.store.get('minute')}},actor)
            payload['available_source_ids']=payload.pop('_source_ids')
            from .shared_media import enrich_public_photos
            enrich_public_photos(self.store,actor,payload)
            self.store.save_input(cid, payload)
            from .role_boundary import ROLE_RULES
            answer, usage = life.lab.session.backend.generate('peer_choice',
                life.persona+'\n'+life.lab.session.speech_contract+'\n'+PEER_INSTRUCTIONS+'\n'+ROLE_RULES, payload,
                **({'cancel': life.cancel} if life.cancel is not None else {}))
            self.validate_peer(actor, answer, payload)
            with self.store.lock, self.store.db:
                current = self.store.cycle(cid)
                if current['status'] != 'running' or current['revision'] != self.store.get('revision') or self.store.get('paused'):
                    raise WorldConflict('peer decision was interrupted or became stale')
                current['value']['results']['peer_choice'] = answer
                if answer['decision'] != 'keep':
                    self.store.db.execute('INSERT INTO peer_jobs VALUES(?,?,?,?,?,?,?)',
                        (cid, actor, answer['recipient_actor_id'], json.dumps(answer, ensure_ascii=False), 'pending', None, None))
                for eid in answer['read_ids']:
                    self.store.db.execute('INSERT OR IGNORE INTO peer_reads VALUES(?,?)', (actor, eid))
                self.store.actor_put(actor, 'peer_next_check', self.now()+300)
                self.store.put('revision', self.store.get('revision')+1)
                self.store.db.execute("UPDATE world_cycles SET status='complete',phase='done',revision=?,value=? WHERE id=?",
                    (self.store.get('revision'), json.dumps(current['value'], ensure_ascii=False), cid))
                self.store.db.execute("UPDATE world_calls SET status='completed',usage=? WHERE id=?", (json.dumps(usage), call))
                self.store.snapshot()
        except Exception as error:
            self.store.fail(call, getattr(life.lab.session.backend,'last_usage',{}), answer, type(error).__name__)
            raise

    def validate_peer(self, actor, answer, payload):
        validate_shape(answer, PEER_CHOICE)
        from .role_boundary import check_character_speech
        check_character_speech(answer['text'],self.lives[actor].name)
        peer = payload['peer_context']; decision = answer['decision']; recipient = answer['recipient_actor_id']
        if not set(answer['source_ids']) <= set(payload['available_source_ids']) or not set(answer['read_ids']) <= set(peer['unread_ids']):
            raise ValueError('peer decision uses private or unavailable evidence')
        if decision == 'keep':
            if answer['text'] or any(answer[k] is not None for k in ('recipient_actor_id','invitation_id','place_id','post_id')):
                raise ValueError('keep cannot send words or select a target')
            return
        if recipient not in self.lives or recipient == actor or not answer['text'].strip():
            raise ValueError('peer decision needs own words and the other registered actor')
        if decision == 'say' and recipient not in peer['nearby_actor_ids']:
            raise ValueError('a scene requires confirmed co-presence')
        if decision == 'invite':
            places = {e['id'] for e in payload['life']['entities'] if e['kind']=='place'}
            if answer['place_id'] not in places:
                raise ValueError('invitation needs a known place')
        elif decision not in ('accept','decline') and answer['place_id'] is not None:
            raise ValueError('only an invitation chooses a place')
        if decision in ('accept','decline'):
            invite = next((r for r in peer['invitations'] if r['id']==answer['invitation_id']), None)
            if not invite or invite['recipient'] != actor or invite['sender'] != recipient or invite['status'] != 'pending':
                raise ValueError('only the actual recipient can answer an open invitation')
            if answer['place_id'] is not None and answer['place_id']!=invite['place_id']:
                raise ValueError('a reply cannot change the invited place')
        elif answer['invitation_id'] is not None:
            raise ValueError('unexpected invitation reference')
        if decision == 'comment':
            source = next((e for e in peer['events'] if e['id'] in answer['source_ids'] and
                           e['value']['kind'] in ('public_post','comment') and e['value'].get('post_id')==answer['post_id'] and
                           e['value']['sender_actor_id']==recipient and not e['value']['outbound']), None)
            if not source:
                raise ValueError('a comment needs a received, actually delivered post')
            root=self.store.db.execute("SELECT actor_id,value FROM world_events WHERE id=? AND stage='post'",(answer['post_id'],)).fetchone()
            if not root or root['actor_id']!=recipient or json.loads(root['value']).get('delivery',{}).get('status')!='sent':
                raise ValueError('peer comment must target the other actor\'s delivered channel post')
        elif answer['post_id'] is not None:
            raise ValueError('unexpected post target')

    def deliver_peer(self, row):
        value = json.loads(row['value']); sender, recipient = row['sender'], row['recipient']
        kind = value['decision']; receipt = None
        if kind == 'comment':
            target = self.lives[recipient].connection
            binding = target.get('binding')
            event = json.loads(self.store.db.execute('SELECT value FROM world_events WHERE id=?', (value['post_id'],)).fetchone()[0])
            root_receipt = next((r for r in event['delivery']['receipts'] if r['chat_id']==binding['channel']['id']), None)
            if not root_receipt:
                raise ValueError('no channel receipt for peer comment')
            with target.lock:
                if not target.db.execute('SELECT 1 FROM forwards WHERE channel_id=? AND post_id=?',
                    (binding['channel']['id'], root_receipt['message_id'])).fetchone():
                    return False  # Not dispatched; wait for the channel's automatic forward.
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE peer_jobs SET status='sending' WHERE id=?", (row['id'],))
            try:
                attempts = sum(r['job_id']==row['id'] for r in (self.store.actor_get(sender,'peer_delivery_repairs') or []))
                key = 'peer:'+row['id']+(':review'+str(attempts) if attempts else '')
                receipt = self.lives[sender].connection.send_peer_comment(key, value['text'], target, root_receipt['message_id'])
            except Exception:
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE peer_jobs SET status='unknown',error='no confirmed peer comment receipt; no replay' WHERE id=?", (row['id'],))
                raise
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE peer_jobs SET status=status WHERE id=?', (row['id'],))
            eid = self.store.relay(sender, recipient, kind, value['text'], source_ids=value['source_ids'],
                                   delivery_id=row['id'], details={'post_id': value['post_id']} if kind=='comment' else None)
            if kind == 'invite':
                self.store.db.execute('INSERT INTO peer_invitations VALUES(?,?,?,?,?,?)',
                    (row['id'], sender, recipient, value['place_id'], 'pending', eid))
                # An address deliberately shared by the inviter is now known, not every private place.
                place = self.store.db.execute('SELECT value FROM world_entities WHERE id=?', (value['place_id'],)).fetchone()
                entity = json.loads(place[0]); entity['known_to'] = list(dict.fromkeys([*entity['known_to'], recipient]))
                self.store.db.execute('UPDATE world_entities SET value=? WHERE id=?', (json.dumps(entity,ensure_ascii=False), value['place_id']))
            elif kind in ('accept','decline'):
                self.store.db.execute('UPDATE peer_invitations SET status=? WHERE id=?',
                    ('accepted' if kind=='accept' else 'declined', value['invitation_id']))
            self.store.db.execute("UPDATE peer_jobs SET status='sent',receipt=? WHERE id=?",
                                  (json.dumps(receipt) if receipt else None, row['id']))
            self.store.snapshot()
        if receipt:
            self.accept_peer_comment(sender, recipient, value['post_id'], value['text'], receipt, row['id'])
        return True

    def confirm_peer_absent(self, actor, job_id):
        """Explicit owner check, scoped to one paused sender and one uncertain comment."""
        life=self.lives[actor]
        if self.enabled(actor) or life.lab.busy:
            raise TelegramError('Сначала поставьте автора комментария на паузу.')
        with self.store.lock,self.store.db:
            row=self.store.db.execute('SELECT * FROM peer_jobs WHERE id=? AND sender=?',(job_id,actor)).fetchone()
            if not row or row['status']!='unknown' or json.loads(row['value'])['decision']!='comment':
                raise TelegramError('Этот комментарий не требует такого разбора.')
            repairs=self.store.actor_get(actor,'peer_delivery_repairs') or []
            repairs.append({'job_id':job_id,'basis':'owner_explicitly_checked_absence_in_target_discussion',
                            'at':self.now(),'previous_job':dict(row)})
            self.store.actor_put(actor,'peer_delivery_repairs',repairs)
            self.store.db.execute("UPDATE peer_jobs SET status='pending',error=NULL WHERE id=?",(job_id,))
            status=life.get('life_status');status.update(status='paused',error=None)
            life.put('life_status',status);life.lab.failure=None

    def sync_received_comments(self):
        for row in self.store.db.execute("SELECT * FROM peer_jobs WHERE status='sent' AND receipt IS NOT NULL").fetchall():
            receipt=json.loads(row['receipt']);target=self.lives[row['recipient']].connection
            known=self.store.db.execute('SELECT 1 FROM channel_comments WHERE chat_id=? AND message_id=?',
                                        (receipt['chat_id'],receipt['message_id'])).fetchone()
            if not known:
                value=json.loads(row['value'])
                self.accept_peer_comment(row['sender'],row['recipient'],value['post_id'],value['text'],receipt,row['id'])

    def accept_peer_comment(self, sender, recipient, post_id, text, receipt, job_id):
        """Import a real bot-to-bot comment from its receipt, independent of Telegram polling."""
        target = self.lives[recipient].connection
        group, mid = receipt['chat_id'], receipt['message_id']
        with target.lock, target.db:
            if group != target.get('binding')['discussion']['id'] or receipt['status']!='sent':
                raise ValueError('wrong peer comment receipt')
            root = target.db.execute('SELECT root_id FROM forwards WHERE group_id=? AND post_id=?',
                (group, next(r['message_id'] for r in json.loads(self.store.db.execute('SELECT value FROM world_events WHERE id=?', (post_id,)).fetchone()[0])['delivery']['receipts'] if r['chat_id']==target.get('binding')['channel']['id']))).fetchone()[0]
            target.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)',
                (group, mid, root, text, self.lives[sender].name, 'comment', self.lives[sender].connection.get('bot')['id'], self.now()))
        self.lives[recipient].import_comments()

    def _run_cycle(self, cycle):
        actor = cycle['actor_id']; life = self.lives[actor]
        if cycle['phase']=='outcome' and cycle['value'].get('shared_window') and self.store.get('minute') < cycle['value']['shared_window']['due_minute']:
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE world_cycles SET status='waiting' WHERE id=?", (cycle['id'],))
            life.current_phase = 'living'
            return
        if cycle['phase']=='share':
            life.focus(own_channel=not cycle['value'].get('channel_visit') or cycle['value'].get('channel_purpose')=='own_channel')
        life.current_phase = cycle['phase']; life.lab.busy = True
        life.lab.session.step(cycle['id'])
        updated = self.store.cycle(cycle['id'])
        if cycle['phase']=='outcome' and updated['value'].get('shared_window',{}).get('destination_id') and updated['value']['results']['outcome']['status']=='succeeded':
            with self.store.lock,self.store.db:
                previous_revision=self.store.get('revision')
                self.store.db.execute('UPDATE world_cycles SET revision=revision WHERE id=?',(cycle['id'],))
                place=updated['value']['shared_window']['destination_id']
                for peer in self.store.nearby(actor):
                    self.store.relay(actor,peer,'arrival',life.name+' пришёл в общую локацию.',
                        source_ids=[cycle['id']+'-outcome'],delivery_id='arrival:'+cycle['id']+':'+peer,details={'place_id':place})
                if self.store.get('revision')!=previous_revision:
                    self.store.rebase_waiting_cycle(cycle['id'],actor,previous_revision)
                self.store.db.execute("UPDATE peer_invitations SET status='met' WHERE place_id=? AND status='accepted' AND sender IN (SELECT actor FROM peer_positions WHERE place_id=?) AND recipient IN (SELECT actor FROM peer_positions WHERE place_id=?)",(place,place,place))
                self.store.snapshot()
        if updated['status']=='complete' and not cycle['value'].get('channel_visit'):
            with self.store.lock, self.store.db:
                status = life.get('life_status'); status.update(last_cycle=cycle['id'],next_wake=self.now()+life.get('life_controls')['wake_minutes']*60)
                life.put('life_status',status)
        elif updated['status']=='complete':
            life.finish_visit_turn()

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        actor = None
        try:
            if self.store.get('paused'):
                return
            ready = list(self.store.db.execute("SELECT id,actor_id FROM world_cycles WHERE status IN ('ready','running') ORDER BY rowid"))
            if ready:
                actor = ready[0]['actor_id']
                if self.enabled(actor):
                    self._run_cycle(self.store.cycle(ready[0]['id']))
                    return
                # A paused actor's accepted pending input is preserved. Park it,
                # not another actor's calls or already completed action.
                with self.store.lock,self.store.db:
                    self.store.db.execute("UPDATE world_cycles SET status='held' WHERE id=? AND status='ready'",(ready[0]['id'],))
            self.advance()
            intervention=self.store.db.execute("SELECT * FROM world_interventions WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if intervention and any(self.enabled(a) for a in self.lives):
                from .world_interventions import realize
                realize(self,intervention)
                return
            self.circulate_posts()
            self.sync_received_comments()
            ordered = list(self.lives)
            if self.last_actor in ordered:
                index = ordered.index(self.last_actor)+1; ordered = ordered[index:]+ordered[:index]
            for actor in ordered:
                life = self.lives[actor]
                if not self.enabled(actor):
                    continue
                self.last_actor = actor
                with self.store.lock,self.store.db:self.store.put('shared_last_actor',actor)
                life.sync_world_profile()
                if life.connection.poll_blocked:
                    life.block('Приём Telegram этого героя остановлен.'); continue
                uncertain = self.store.db.execute("SELECT 1 FROM peer_jobs WHERE sender=? AND status IN ('unknown','sending') UNION ALL SELECT 1 FROM world_cycles WHERE actor_id=? AND status='blocked'",(actor,actor)).fetchone()
                uncertain = uncertain or self.store.db.execute("SELECT 1 FROM channel_jobs j JOIN world_events e ON e.id=j.post_id WHERE e.actor_id=? AND j.status IN ('sending','unknown')",(actor,)).fetchone()
                if uncertain:
                    life.block('Незавершённая операция этого героя требует проверки; автоматического повтора нет.'); continue
                held = self.store.db.execute("SELECT id FROM world_cycles WHERE actor_id=? AND status='held' LIMIT 1", (actor,)).fetchone()
                waiting = self.store.db.execute("SELECT id,value FROM world_cycles WHERE actor_id=? AND status='waiting' LIMIT 1", (actor,)).fetchone()
                if held or waiting and self.store.get('minute')>=json.loads(waiting['value'])['shared_window']['due_minute']:
                    cid = held[0] if held else waiting['id']
                    with self.store.lock,self.store.db:
                        self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?", (self.store.get('revision'),cid))
                    self._run_cycle(self.store.cycle(cid)); return
                job = self.store.db.execute("SELECT * FROM peer_jobs WHERE sender=? AND status='pending' ORDER BY rowid LIMIT 1",(actor,)).fetchone()
                if job and self.deliver_peer(job):
                    return
                pending = self.store.db.execute("SELECT e.* FROM world_events e WHERE e.actor_id=? AND e.stage='post' AND e.id NOT IN (SELECT post_id FROM channel_jobs WHERE status IN ('sent','local_only')) ORDER BY e.rowid LIMIT 1",(actor,)).fetchone()
                if pending:
                    life.lab.busy=True; life.deliver({**dict(pending),'value':json.loads(pending['value'])}); return
                reaction = self.store.db.execute("SELECT j.* FROM channel_reaction_jobs j JOIN world_events e ON e.id=j.audience_id WHERE e.actor_id=? AND j.status IN ('pending','sending','unknown') ORDER BY j.rowid LIMIT 1",(actor,)).fetchone()
                if reaction:
                    if reaction['status']!='pending':life.block('Неизвестная доставка реакции; нужен разбор.'); continue
                    life.deliver_reaction(reaction); return
                if not life.get('social_review_complete'):
                    life.lab.busy=True;life.social.bootstrap(life.lab.session.backend,life.persona,life.cancel,actor=actor);return
                life.import_comments()
                # Own life wins over chat when due; a parked physical action is never replaced.
                status = life.get('life_status')
                if not waiting and self.now()>=status['next_wake'] and life.get('bootstrap_prepared'):
                    life.lab.session.prepare(actor,minutes=0);return
                if life.get('own_channel_check_requested') or not life.get('bootstrap_prepared'):
                    life.focus(own_channel=True);life.channel_cycle(bootstrap=not life.get('bootstrap_prepared'))
                    with self.store.lock,self.store.db:life.put('own_channel_check_requested',False)
                    return
                goal = self.store.db.execute("SELECT key FROM personal_intentions WHERE actor_id=? AND kind='post' AND status='pending' AND due<=? ORDER BY due LIMIT 1",(actor,self.now())).fetchone()
                if goal:
                    life.focus(goal[0],own_channel=True);life.channel_cycle();return
                visit = life.get('channel_visit') or {}
                if visit.get('away_until',0)<=self.now() and life.focus():
                    life.channel_cycle();return
                if self.now()>=(life.get('peer_next_check') or 0):
                    life.lab.busy=True;self.peer_turn(actor);return
                life.current_phase='living' if waiting else 'waiting'
        except Exception as error:
            if actor:
                self.lives[actor].block('Этап остановлен: '+type(error).__name__+'. Результат сохранён; автоматического повтора нет.')
            else:
                with self.store.lock,self.store.db:
                    self.store.put('shared_error','Общий этап остановлен: '+type(error).__name__+'. Нужна проверка; повтора нет.')
                    self.store.put('paused',True)
        finally:
            for life in self.lives.values():
                life.lab.busy=False
            self.lock.release()

    def loop(self):
        while not self.stopping.wait(.25):
            self.tick()
