"""One owner and clock; parked intentions have no precomputed future outcome."""
import hashlib
import json
import time
import uuid
from contextlib import nullcontext

from .world import WorldStore, WorldConflict
from .world_protocol import validate_shape
from .shared_protocol import TIMED_CHOICE, TIMED_INTENT


class SharedWorldStore(WorldStore):
    def __init__(self, directory):
        super().__init__(directory, seed_garik=False)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS peer_deliveries(id TEXT PRIMARY KEY,sender TEXT NOT NULL,
                recipient TEXT NOT NULL,kind TEXT NOT NULL,in_event TEXT NOT NULL,
                out_event TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS peer_reads(actor TEXT NOT NULL,event_id TEXT NOT NULL,
                PRIMARY KEY(actor,event_id));
            CREATE TABLE IF NOT EXISTS peer_invitations(id TEXT PRIMARY KEY,sender TEXT NOT NULL,
                recipient TEXT NOT NULL,place_id TEXT NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS peer_positions(actor TEXT PRIMARY KEY,place_id TEXT,source_id TEXT);
            CREATE TABLE IF NOT EXISTS peer_jobs(id TEXT PRIMARY KEY,sender TEXT NOT NULL,
                recipient TEXT NOT NULL,value TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS world_interventions(id TEXT PRIMARY KEY,text TEXT NOT NULL,
                status TEXT NOT NULL,cycle_id TEXT,result TEXT,error TEXT,created REAL NOT NULL,minute INTEGER);
        ''')
        with self.db:
            self.db.execute("UPDATE peer_jobs SET status='unknown',error='process stopped before receipt; no replay' WHERE status='sending'")
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id IN (SELECT cycle_id FROM world_interventions WHERE status='started')")
            self.db.execute("UPDATE world_interventions SET status='failed',error='Запуск прерван; автоматического повтора нет' WHERE status='started'")

    def state(self):
        state = super().state()
        state['positions'] = [dict(r) for r in self.db.execute('SELECT * FROM peer_positions')] if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='peer_positions'").fetchone() else []
        return state

    def state_for_cycle(self, cycle, state):
        visible = super().state_for_cycle(cycle, state)
        if cycle.get('phase') == 'outcome' and cycle['value'].get('shared_window'):
            visible = {**visible, 'outcome_at_current_minute': True}
        return visible

    def actor_view(self, actor_id, state=None):
        visible = super().actor_view(actor_id, state)
        visible.pop('positions', None)
        visible['peer_context'] = self.peer_context(actor_id, at_minute=visible['minute'])
        return visible

    def complete(self, call_id, value, usage, *args, **kwargs):
        call = self.db.execute('SELECT cycle_id FROM world_calls WHERE id=?', (call_id,)).fetchone()
        if call:
            cycle = self.cycle(call[0])
            if cycle['phase'] == 'world' and set(value.get('visible_to', [])) - {cycle['actor_id'], *self.nearby(cycle['actor_id'])}:
                raise ValueError('a distant actor cannot observe a private world event')
        return super().complete(call_id, value, usage, *args, **kwargs)

    def nearby(self, actor):
        row = self.db.execute('SELECT place_id FROM peer_positions WHERE actor=?', (actor,)).fetchone()
        return [r[0] for r in self.db.execute('SELECT actor FROM peer_positions WHERE place_id=? AND actor!=?',
                                             (row[0], actor))] if row and row[0] else []

    def peer_context(self, actor, at_minute=None):
        at_minute = self.get('minute') if at_minute is None else at_minute
        events = [{**dict(r), 'value': json.loads(r['value'])} for r in self.db.execute(
            "SELECT id,actor_id,minute,value FROM world_events WHERE actor_id=? AND stage='peer' AND minute<=? ORDER BY rowid DESC LIMIT 16",
            (actor, at_minute))]
        unread = [r[0] for r in self.db.execute(
            'SELECT d.in_event FROM peer_deliveries d JOIN world_events e ON e.id=d.in_event '
            'WHERE d.recipient=? AND e.minute<=? AND d.in_event NOT IN '
            '(SELECT event_id FROM peer_reads WHERE actor=?) ORDER BY d.rowid LIMIT 8', (actor, at_minute, actor))]
        # Retain an unread source even after it leaves the recent window.
        known = {e['id'] for e in events}
        for eid in unread:
            if eid not in known:
                r = self.db.execute('SELECT id,actor_id,minute,value FROM world_events WHERE id=?', (eid,)).fetchone()
                events.append({**dict(r), 'value': json.loads(r['value'])})
        own = self.db.execute('SELECT place_id FROM peer_positions WHERE actor=?', (actor,)).fetchone()
        waiting = self.db.execute("SELECT value FROM world_cycles WHERE actor_id=? AND status='waiting'", (actor,)).fetchone()
        invitations=[]
        for row in self.db.execute("SELECT * FROM peer_invitations WHERE (sender=? OR recipient=?) AND status IN ('pending','accepted','met') ORDER BY rowid DESC LIMIT 8",(actor,actor)):
            invitation=dict(row)
            source=self.db.execute('SELECT value FROM world_events WHERE id=?',(row['source_id'],)).fetchone()
            invitation['proposal']=json.loads(source[0])['text'] if source else ''
            invitation['responses']=[{'actor_id':r['sender'],'text':json.loads(r['value'])['text']}
                for r in self.db.execute("SELECT sender,value FROM peer_jobs WHERE status='sent' AND json_extract(value,'$.invitation_id')=? ORDER BY rowid",(row['id'],))]
            invitations.append(invitation)
        return {'events': list(reversed(events)), 'unread_ids': unread,
                'invitations':list(reversed(invitations)),
                'place_id': own[0] if own else None, 'nearby_actor_ids': self.nearby(actor),
                'available_actor_ids': [r[0] for r in self.db.execute('SELECT id FROM world_actors WHERE id!=?', (actor,))],
                'ongoing_action': json.loads(waiting[0])['results']['choice']['action'] if waiting else None}

    def relay(self, sender, recipient, kind, text, *, source_ids=(), delivery_id=None, details=None):
        """Atomic, once-only acceptance; copy only chosen words or delivered public text."""
        actors = {a['id'] for a in self.state()['actors']}
        if sender == recipient or {sender, recipient} - actors or not isinstance(text, str) or not text.strip():
            raise ValueError('invalid peer delivery')
        identity = delivery_id or uuid.uuid4().hex
        token = hashlib.sha256(identity.encode()).hexdigest()[:32]
        with self.lock, (nullcontext() if self.db.in_transaction else self.db):
            old = self.db.execute('SELECT * FROM peer_deliveries WHERE id=?', (identity,)).fetchone()
            if old:
                original = json.loads(self.db.execute('SELECT value FROM world_events WHERE id=?', (old['in_event'],)).fetchone()[0])
                if (old['sender'], old['recipient'], old['kind'], original['text']) != (sender, recipient, kind, text):
                    raise WorldConflict('peer delivery id already belongs to different words')
                return old['in_event']
            ids = ['peer-' + token + '-in', 'peer-' + token + '-out']
            for eid, actor, outbound in [(ids[0], recipient, False), (ids[1], sender, True)]:
                cid = 'relay-' + token + ('-out' if outbound else '-in')
                self.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                (cid, actor, 'complete', 'done', self.get('revision'),
                                 json.dumps({'results': {}, 'inputs': {}, 'peer_delivery': True}), time.time()))
                value = {'kind': kind, 'sender_actor_id': sender, 'recipient_actor_id': recipient,
                         'text': text, 'outbound': outbound, 'status': 'delivered', **(details or {})}
                # Recipient never receives sender's private evidence chain.
                if outbound:
                    value['source_ids'] = list(source_ids)
                self.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                (eid, cid, actor, 'peer', self.get('minute'), json.dumps(value, ensure_ascii=False)))
            self.db.execute('INSERT INTO peer_deliveries VALUES(?,?,?,?,?,?,?)',
                            (identity, sender, recipient, kind, ids[0], ids[1], time.time()))
            self.put('revision', self.get('revision') + 1)
            self.snapshot()
            return ids[0]


class SharedTimeline:
    choice_instruction = '''
duration_minutes — реалистичный план времени дороги, ожидания и выбранного
занятия, от 1 до 1440 минут; это не результат и не квота публикаций.
destination_id — известная локация, куда персонаж явно решил добраться
или где явно решил провести выбранное занятие, иначе null. Она подтверждается
только успешным outcome. Если остаёшься в известном месте, укажи его ID;
null означает неподтверждённое местоположение, а не присутствие в прежнем.
Приглашение/согласие само по себе не дорога.
life.peer_context.invitations хранит доставленные предложения и ответы.
Принятая встреча может стать твоим ближайшим делом, когда освободишься:
дойти, встретиться, затем выбрать собственное участие в предложенной затее.
proposal — зачем позвали, responses — что каждый действительно ответил.
met означает только встречу, а не готовый результат общего занятия. Не теряй
его смысл после прихода. Можно отложить, отказаться или предложить другой ход;
чужое участие и реплики не назначай. Завершённое дело помнится по outcome,
оно не становится новым долгом из-за сохранённого старого приглашения.
Не выбирай движения и ответы за другого управляемого героя.
'''
    outcome_instruction = '''
shared_action_window — уже прошедший промежуток общего мира. Ты впервые
разрешаешь попытку сейчас, по актуальному состоянию вещей. Будущий итог
заранее не рассчитывался. elapsed_minutes и сумма time_costs должны равняться
duration_minutes этого окна: эти минуты УЖЕ прошли, clock показывает конец.
Не назначай второму управляемому герою реплики, согласие, чувства или новое
действие. Учитывай только его уже совершённое и доставленные слова.
Не меняй entities для actor_id из world.actors; положение учитывает хост.
'''

    def __init__(self, store):
        self.store = store

    def enrich(self, stage, payload, cycle):
        if stage in ('world','outcome','horizon'):
            from .world_interventions import facts
            payload['author_world_facts']=facts(self.store)
        if stage == 'outcome':
            window = cycle['value'].get('shared_window')
            if not window or self.store.get('minute') < window['due_minute']:
                raise WorldConflict('outcome cannot run before its independent deadline')
            payload['shared_action_window'] = window
            payload['shared_scene'] = {'nearby_actor_ids': self.store.nearby(cycle['actor_id']),
                'meetings':[i for i in self.store.peer_context(cycle['actor_id'])['invitations'] if i['status'] in ('accepted','met')],
                'delivered_words': [e for e in self.store.peer_context(cycle['actor_id'])['events']
                                    if e['value']['kind'] in ('say', 'accept', 'decline')]}
        return payload

    def extract(self, answer, payload, cycle):
        schema = TIMED_INTENT if cycle['value'].get('lossless_intentions') else TIMED_CHOICE
        validate_shape(answer, schema)
        destination = answer['destination_id']
        known = {e['id'] for e in payload.get('known_entities', payload.get('life', {}).get('entities', [])) if e['kind'] == 'place'}
        if destination is not None and destination not in known:
            raise ValueError('a destination must be an observed place')
        return ({k: v for k, v in answer.items() if k not in ('duration_minutes', 'destination_id')},
                {k: answer[k] for k in ('duration_minutes', 'destination_id')})

    def apply(self, stage, timing, cycle):
        actor = cycle['actor_id']
        if stage == 'choice':
            start = self.store.get('minute')
            location = self.store.db.execute('SELECT place_id FROM peer_positions WHERE actor=?', (actor,)).fetchone()
            cycle['value']['shared_window'] = {**timing, 'started_minute': start,
                'due_minute': start + timing['duration_minutes'], 'origin_id': location[0] if location else None,
                'present_actor_ids': self.store.nearby(actor)}
            if not location or timing['destination_id'] != location[0]:
                self.store.db.execute('UPDATE peer_positions SET place_id=NULL,source_id=? WHERE actor=?', (cycle['id']+'-choice', actor))
        elif stage == 'outcome':
            window = cycle['value']['shared_window']
            outcome = cycle['value']['results']['outcome']
            if outcome['elapsed_minutes'] != window['duration_minutes']:
                raise ValueError('shared outcome cannot change elapsed wall time')
            if window['destination_id'] and outcome['status'] == 'succeeded':
                self.store.db.execute('UPDATE peer_positions SET place_id=?,source_id=? WHERE actor=?',
                                     (window['destination_id'], cycle['id']+'-outcome', actor))
            observers = set(window['present_actor_ids']) & set(self.store.nearby(actor))
            # Shared consequences, no private diary/intent. Nobody gains knowledge remotely.
            row = self.store.db.execute('SELECT value FROM world_events WHERE id=?', (cycle['id']+'-outcome',)).fetchone()
            value = json.loads(row[0]); value['visible_to'] = [actor, *sorted(observers)]
            self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',
                                 (json.dumps(value, ensure_ascii=False), cycle['id']+'-outcome'))
