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

    def prepare(self,*args,**kwargs):
        # Only the world director receives authoritative applied-event IDs.
        # They never grant a character awareness of the event or its news.
        with self.lock,self.db:
            cycle=super().prepare(*args,**kwargs)
            from .world_interventions import facts
            ids=[e['id'] for e in facts(self) if e['minute']<=self.get('minute')]
            brief=cycle['value']['brief']
            brief['cause_ids']=list(dict.fromkeys([*brief['cause_ids'],*ids]))
            if ids:
                for beat in ('consequence','return','closure'):
                    if beat not in brief['allowed_beats']:
                        brief['allowed_beats'].append(beat);brief['beat_weights'][beat]=1.0
            self.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(cycle['value'],ensure_ascii=False),cycle['id']))
            return self.cycle(cycle['id'])

    def recover_saved_shared_stage(self,cycle_id,reason):
        """Explicit paused review under corrected receipts/capacity; exact answer, no replay."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if self.db.execute('SELECT 1 FROM world_repairs WHERE cycle_id=?',(cycle_id,)).fetchone():return cycle
            if not self.get('paused') or self.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone():
                raise WorldConflict('pause both characters and wait for calls')
            if any((self.actor_get(a['id'],'life_status') or {}).get('status')=='running' for a in self.state()['actors']):
                raise WorldConflict('pause both characters before review')
            rejected=cycle['value'].get('rejected',{});stage=cycle['phase']
            if cycle['status']!='blocked' or stage not in ('world','outcome') or rejected.get('stage')!=stage or rejected.get('error_type')!='ValueError':
                raise WorldConflict('saved rejected world result required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage=? AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,stage)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            import copy
            previous=copy.deepcopy(cycle)
            if stage=='world':
                supplied=cycle['value']['inputs']['world'].get('author_world_facts',[])
                from .world_interventions import facts
                applied={e['id'] for e in facts(self)}
                ids=[e['id'] for e in supplied if e['id'] in applied]
                brief=cycle['value']['brief'];brief['cause_ids']=list(dict.fromkeys([*brief['cause_ids'],*ids]))
            self.db.execute('UPDATE world_cycles SET revision=?,value=? WHERE id=?',
                (self.get('revision'),json.dumps(cycle['value'],ensure_ascii=False),cycle_id))
            audit={'cycle_id':cycle_id,'reason':reason,'at':time.time(),'original_cycle':previous,
                   'original_call':dict(call),'model_replayed':False}
            self.put('shared_stage_repairs',[*(self.get('shared_stage_repairs') or []),audit])
            apply=(lambda committed:SharedTimeline(self).apply('outcome',None,committed)) if stage=='outcome' else None
            self.complete(call['id'],rejected['answer'],{},after_world='impulse',after_outcome='experience',
                          _reviewed_world_state=True,extra_apply=apply)
            # Several actors may be reviewed while paused. Promote each next
            # stage through the existing held path with a fresh shared revision.
            self.db.execute("UPDATE world_cycles SET status='held' WHERE id=? AND status='ready'",(cycle_id,))
            self.snapshot();return self.cycle(cycle_id)

    def review_saved_outcome(self,cycle_id,omitted_thread_ids,final_place_id,reason):
        """Paused human/maintenance review: keep physical facts, quarantine invalid story metadata."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if self.db.execute('SELECT 1 FROM world_repairs WHERE cycle_id=?',(cycle_id,)).fetchone():return cycle
            self._require_paused_review()
            rejected=cycle['value'].get('rejected',{})
            if cycle['status']!='blocked' or cycle['phase']!='outcome' or rejected.get('stage')!='outcome' or rejected.get('error_type')!='ValueError':
                raise WorldConflict('saved rejected outcome required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            import copy
            saved=rejected['answer'];state=self.state();entities={e['id']:e for e in state['entities']}
            entities.update({e['id']:e for e in saved['entities']})
            omitted=set(omitted_thread_ids)
            invalid={t['id'] for t in saved['threads'] if any(
                eid in entities and set(t['visible_to'])-set(entities[eid]['known_to']) for eid in t['entity_ids'])}
            if not omitted or not omitted<=invalid:raise WorldConflict('only undiscovered-entity thread patches can be quarantined')
            if final_place_id is not None and (final_place_id not in entities or entities[final_place_id]['kind']!='place' or cycle['actor_id'] not in entities[final_place_id]['known_to']):
                raise ValueError('reviewed final place must be an observed location')
            audit={'cycle_id':cycle_id,'at':time.time(),'reason':reason,'original_cycle':copy.deepcopy(cycle),
                   'original_call':dict(call),'omitted_thread_ids':sorted(omitted),'final_place_id':final_place_id,'model_replayed':False}
            self.put('shared_outcome_reviews',[*(self.get('shared_outcome_reviews') or []),audit])
            receipt={'final_place_id':final_place_id,'basis':'explicit_saved_outcome_review'}
            projected={**saved,'threads':[t for t in saved['threads'] if t['id'] not in omitted]}
            self.db.execute('UPDATE world_cycles SET revision=? WHERE id=?',(self.get('revision'),cycle_id))
            self.complete(call['id'],projected,{},after_outcome='experience',_reviewed_thread_omissions=sorted(omitted),
                          extra_apply=lambda c:SharedTimeline(self).apply('outcome',receipt,c))
            self.db.execute("UPDATE world_cycles SET status='held' WHERE id=? AND status='ready'",(cycle_id,))
            self.snapshot();return self.cycle(cycle_id)

    def recover_horizon_identities(self,cycle_id,reason):
        """Explicit paused repair of service identifiers, never scene content or a model replay."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if self.db.execute('SELECT 1 FROM world_repairs WHERE cycle_id=?',(cycle_id,)).fetchone():return cycle
            self._require_paused_review();rejected=cycle['value'].get('rejected',{})
            if cycle['status']!='blocked' or cycle['phase']!='horizon' or rejected.get('stage')!='horizon' or rejected.get('error_type')!='ValueError':
                raise WorldConflict('saved rejected horizon required')
            from .world_hooks import assigned_hooks
            projected=assigned_hooks(rejected['answer'],cycle_id)
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='horizon' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            self.put('horizon_id_reviews',[*(self.get('horizon_id_reviews') or []),{'cycle_id':cycle_id,'reason':reason,'at':time.time(),'original_cycle':cycle,'original_call':dict(call),'model_replayed':False}])
            views=cycle['value'].get('hook_view_proposals',{})
            cycle['value']['hook_view_proposals']={b['id']:views[a['id']] for a,b in zip(rejected['answer']['hooks'],projected['hooks']) if a['id'] in views}
            self.db.execute('UPDATE world_cycles SET revision=?,value=? WHERE id=?',(self.get('revision'),json.dumps(cycle['value'],ensure_ascii=False),cycle_id))
            self.complete(call['id'],projected,{},_reviewed_horizon_ids=True)
            self.db.execute("UPDATE world_cycles SET status='held' WHERE id=? AND status='ready'",(cycle_id,))
            self.snapshot();return self.cycle(cycle_id)

    def _require_paused_review(self):
        if not self.get('paused') or self.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone():
            raise WorldConflict('pause both characters and wait for calls')
        if any((self.actor_get(a['id'],'life_status') or {}).get('status')=='running' for a in self.state()['actors']):
            raise WorldConflict('pause both characters before review')

    def release_interrupted_outcome(self,cycle_id,reason):
        """Leave an unanswered attempt unfinished, available to a new voluntary choice; no replay."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if any(r['cycle_id']==cycle_id for r in self.get('interrupted_outcome_reviews') or []):return cycle
            self._require_paused_review();v=cycle['value'];failure=v.get('failure',{})
            if (cycle['status']!='blocked' or cycle['phase']!='outcome' or failure.get('error_type')!='CodexError'
                    or v.get('rejected') or 'outcome' in v.get('results',{}) or failure.get('diagnostics',{}).get('turn_completed')):
                raise WorldConflict('unanswered interrupted outcome required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            self.put('interrupted_outcome_reviews',[*(self.get('interrupted_outcome_reviews') or []),
                {'cycle_id':cycle_id,'at':time.time(),'reason':reason,'original_cycle':cycle,'original_call':dict(call),'model_replayed':False}])
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id=?",(cycle_id,))
            self.db.execute('UPDATE peer_positions SET place_id=NULL,source_id=? WHERE actor=?',(cycle_id+'-unresolved',cycle['actor_id']))
            self.put('revision',self.get('revision')+1);self.snapshot();return self.cycle(cycle_id)

    def release_rejected_timing_outcome(self,cycle_id,reason):
        """Explicit review: retain an impossible timed answer without asserting its events."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if any(r['cycle_id']==cycle_id for r in self.get('rejected_timing_reviews') or []):return cycle
            self._require_paused_review();v=cycle['value'];rejected=v.get('rejected',{})
            answer=rejected.get('answer',{});duration=v.get('shared_window',{}).get('duration_minutes')
            if (cycle['status']!='blocked' or cycle['phase']!='outcome' or rejected.get('stage')!='outcome'
                    or rejected.get('error_type')!='ValueError' or not isinstance(answer,dict)
                    or not isinstance(duration,int) or answer.get('elapsed_minutes')==duration
                    or not isinstance(answer.get('elapsed_minutes'),int) or 'outcome' in v.get('results',{})):
                raise WorldConflict('saved rejected outcome with incompatible elapsed time required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            self.put('rejected_timing_reviews',[*(self.get('rejected_timing_reviews') or []),
                {'cycle_id':cycle_id,'at':time.time(),'reason':reason,'original_cycle':cycle,'original_call':dict(call),
                 'model_replayed':False,'outcome_applied':False}])
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id=?",(cycle_id,))
            self.db.execute('UPDATE peer_positions SET place_id=NULL,source_id=? WHERE actor=?',(cycle_id+'-unresolved',cycle['actor_id']))
            self.put('revision',self.get('revision')+1);self.snapshot();return self.cycle(cycle_id)

    def release_rejected_place_event(self,cycle_id,reason):
        """Explicitly quarantine an entire rejected world event, without rewriting its prose."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if any(r['cycle_id']==cycle_id for r in self.get('rejected_place_event_reviews') or []):return cycle
            self._require_paused_review();v=cycle['value'];rejected=v.get('rejected',{});answer=rejected.get('answer',{})
            prior={e['id']:e for e in v.get('inputs',{}).get('world',{}).get('world',{}).get('entities',[])}
            changed=[e['id'] for e in answer.get('entities',[]) if e['id'] in ('home','shawarma') and e['id'] in prior
                     and any(e.get(k)!=prior[e['id']][k] for k in ('name','kind','description'))] if isinstance(answer,dict) else []
            if (cycle['status']!='blocked' or cycle['phase']!='world' or rejected.get('stage')!='world'
                    or rejected.get('error_type')!='ValueError' or not changed or v.get('results',{})):
                raise WorldConflict('rejected initial world event with rewritten authored place required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='world' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            self.put('rejected_place_event_reviews',[*(self.get('rejected_place_event_reviews') or []),
                {'cycle_id':cycle_id,'at':time.time(),'reason':reason,'original_cycle':cycle,'original_call':dict(call),
                 'changed_place_ids':changed,'model_replayed':False,'event_applied':False}])
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id=?",(cycle_id,))
            # A world-stage observation is not a movement; preserve the confirmed position.
            self.put('revision',self.get('revision')+1);self.snapshot();return self.cycle(cycle_id)

    def release_rejected_map_event(self,cycle_id,reason):
        """Explicitly discard an uncommitted legacy map observation, never publish or replay it."""
        if not isinstance(reason,str) or not reason.strip():raise ValueError('review reason required')
        with self.lock,self.db:
            cycle=self.cycle(cycle_id)
            if any(r['cycle_id']==cycle_id for r in self.get('rejected_map_reviews') or []):return cycle
            self._require_paused_review();v=cycle['value'];rejected=v.get('rejected',{});answer=rejected.get('answer',{})
            prior=v.get('inputs',{}).get('world',{}).get('world',{})
            known={e['id'] for e in prior.get('entities',[]) if cycle['actor_id'] in e['known_to']}
            known|={e['id'] for e in answer.get('entities',[]) if cycle['actor_id'] in e.get('known_to',[])}
            old={link['id']:set(link['known_to']) for link in prior.get('place_links',[])}
            unknown_refs=set(answer.get('entity_ids',[]))-known
            remote=any(set(link.get('known_to',[]))-old.get(link['id'],set())-set(answer.get('visible_to',[])) for link in answer.get('place_links',[]))
            if (cycle['status']!='blocked' or cycle['phase']!='world' or rejected.get('stage')!='world'
                    or rejected.get('error_type')!='ValueError' or v.get('results') or answer.get('domain')!='city'
                    or not answer.get('place_links') or not (unknown_refs or remote)):
                raise WorldConflict('uncommitted legacy map with invalid references or remote discovery required')
            call=self.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='world' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed source call required')
            self.put('rejected_map_reviews',[*(self.get('rejected_map_reviews') or []),
                {'cycle_id':cycle_id,'at':time.time(),'reason':reason,'original_cycle':cycle,'original_call':dict(call),
                 'unknown_refs':sorted(unknown_refs),'remote_route_discovery':remote,'model_replayed':False,'event_applied':False}])
            self.db.execute("UPDATE world_cycles SET status='abandoned' WHERE id=?",(cycle_id,))
            self.put('revision',self.get('revision')+1);self.snapshot();return self.cycle(cycle_id)

    def resolve_failed_research(self,cycle_id):
        """Retain a failed read-only attempt; advance to its failed outcome, never search again."""
        with self.lock,self.db:
            cycle=self.cycle(cycle_id);value=cycle['value']
            if (cycle['status']!='blocked' or cycle['phase']!='research' or
                    value.get('results',{}).get('choice',{}).get('kind')!='research' or
                    'research' in value['results'] or not value.get('shared_window')):
                raise WorldConflict('only a failed shared research attempt can be resolved')
            if self.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone():
                raise WorldConflict('wait for active calls before resolving research')
            call=self.db.execute("SELECT id,usage FROM world_calls WHERE cycle_id=? AND stage='research' AND status='failed' ORDER BY rowid DESC LIMIT 1",(cycle_id,)).fetchone()
            if not call:raise WorldConflict('failed research call required')
            failure={'status':'failed','call_id':call['id'],'facts':[],
                     'diagnostics':value.get('failure',{}).get('diagnostics',{}),
                     'meaning':'No completed research result. Do not infer or invent facts; no retry was performed.'}
            audits=self.get('research_failure_resolutions') or []
            audits.append({'cycle_id':cycle_id,'actor_id':cycle['actor_id'],'at':time.time(),
                           'call_id':call['id'],'original_failure':value.get('failure'),
                           'original_usage':json.loads(call['usage']),'no_replay':True})
            self.put('research_failure_resolutions',audits)
            value['results']['research_failure']=failure
            revision=self.get('revision')+1;self.put('revision',revision)
            status='waiting' if self.get('minute')<value['shared_window']['due_minute'] else 'ready'
            self.db.execute("UPDATE world_cycles SET status=?,phase='outcome',revision=?,value=? WHERE id=?",(status,revision,json.dumps(value,ensure_ascii=False),cycle_id))
            self.snapshot();return self.cycle(cycle_id)

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
            if cycle['phase'] in ('world','outcome'):
                import re
                own_name=next(a['name'].split()[0] for a in self.state()['actors'] if a['id']==cycle['actor_id'])
                for actor in self.state()['actors']:
                    if actor['id']==cycle['actor_id']:continue
                    parts=actor['name'].split();name=re.escape(parts[0])
                    tail=r'(?:\s+'+re.escape(' '.join(parts[1:]))+r')?' if len(parts)>1 else ''
                    pattern=r'\b'+name+tail+r'\s+(?:(?:ему|ей|мне|коротко|сразу|тихо|снова|потом|тут)\s+){0,3}(?:ответил|сказал|согласился|пообещал|предложил|позвал|пригласил|попросил|отказался|пришёл|пришел|дошёл|дошел|спустился|вернулся|подошёл|подошел|сел|поел|ел|ждал|заказал|взял|принёс|принес|показал|сделал|проверил|осмотрел|ушёл|ушел)\b'
                    joint=r'\b(?:'+name+tail+r'\s+и\s+'+re.escape(own_name)+'|'+re.escape(own_name)+r'\s+и\s+'+name+tail+r')\b'
                    if re.search(pattern,value.get('observation',''),re.I) or re.search(joint,value.get('observation',''),re.I):
                        raise ValueError('world cannot narrate another managed actor speech/actions; use committed receipts')
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
        own = self.db.execute('SELECT place_id,source_id FROM peer_positions WHERE actor=?', (actor,)).fetchone()
        waiting = self.db.execute("SELECT value FROM world_cycles WHERE actor_id=? AND status='waiting'", (actor,)).fetchone()
        invitations=[]
        for row in self.db.execute("SELECT * FROM peer_invitations WHERE (sender=? OR recipient=?) AND status IN ('pending','accepted','met') ORDER BY rowid DESC LIMIT 8",(actor,actor)):
            invitation=dict(row)
            source=self.db.execute('SELECT value FROM world_events WHERE id=?',(row['source_id'],)).fetchone()
            invitation['proposal']=json.loads(source[0])['text'] if source else ''
            invitation['responses']=[{'actor_id':r['sender'],'text':json.loads(r['value'])['text']}
                for r in self.db.execute("SELECT sender,value FROM peer_jobs WHERE status='sent' AND json_extract(value,'$.invitation_id')=? ORDER BY rowid",(row['id'],))]
            invitations.append(invitation)
        nearby=self.nearby(actor)
        place=self.db.execute('SELECT value FROM world_entities WHERE id=?',(own[0],)).fetchone() if own and own[0] else None
        place_name=json.loads(place[0])['name'] if place else None
        names={r['id']:r['name'] for r in self.db.execute('SELECT * FROM world_actors')}
        scene={'place_id':own[0] if own else None,'place_name':place_name,'source_id':own[1] if own else None,
               'nearby':[{'actor_id':a,'name':names[a]} for a in nearby],
               'meaning':'Последняя подтверждённая позиция. Перечисленные рядом герои имеют квитанцию того же места; чужой план их не перемещает.'}
        return {'events': list(reversed(events)), 'unread_ids': unread,'current_scene':scene,
                'invitations':list(reversed(invitations)),
                'place_id': own[0] if own else None, 'nearby_actor_ids': nearby,
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
    world_instruction = '''
world_contract.location — последнее подтверждённое место наблюдателя, с квитанцией.
managed_actor_locations отдельно показывает текущие подтверждённые места
всех управляемых героев. Другой герой не появляется рядом потому, что его
хотят встретить. При другом месте или null встречу не придумывай. Его слова
приходят отдельным peer-событием; world не разыгрывает его как NPC.
При известном месте показывай доступное оттуда; world не перемещает героя.
История про квартиру не означает, что герой уже вернулся туда из шаурмичной.
При неизвестном месте не придумывай точку наблюдения. У outcome конечное место
подтверждается отдельно. fixed_place_fields содержит точные неизменяемые поля
авторских мест: при обновлении condition/known_to/world_note копируй эти поля
дословно; даже уточнение «под квартирой Гарика» в description меняет канон.
'''
    choice_instruction = '''
duration_minutes — реалистичный план времени дороги, ожидания и выбранного
занятия, от 1 до 1440 минут; это не результат и не квота публикаций.
destination_id — известная локация, куда персонаж явно решил добраться
или где явно решил провести выбранное занятие, иначе null. Она подтверждается
только фактическим final_place_id из outcome. Если остаёшься в известном месте, укажи его ID;
null означает неподтверждённое местоположение, а не присутствие в прежнем.
Приглашение/согласие само по себе не дорога.
Для совместной затеи при разных местах сначала выбери свой приход к месту
встречи. Дальнейшее общее занятие выбирается после подтверждённой встречи;
не упаковывай ещё не состоявшийся приход другого в готовый совместный итог.
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
final_place_id — подтверждённое МЕСТО В КОНЦЕ попытки, либо null, если оно
не установлено. Это не план destination_id. Если условие выхода не наступило
и герой остался дома, укажи дом, а не намеченную цель. Если вышел и вернулся,
укажи место возвращения. Общий status=succeeded не подтверждает прибытие.
Используй известное герою место либо новое место, действительно открытое
в этом outcome. Не придумывай местонахождение по одному намерению.
Слова другого героя, доставленные через хост, уже записаны в shared_scene.
Не создавай новую доставку сообщения силами физического мира.
Не пиши «Аркадий ответил/сказал» или «Гарик согласился» за другого героя,
даже если это кажется естественным продолжением собственной попытки.
Его речь живёт в отдельных peer-квитанциях. Наблюдение описывает доступные
обстоятельства; вопрос своего героя можно задать, но ответа за него нет.
Также не выполняй за него дорогу, еду и чужое принятое намерение. Пока его
эпизод не завершён, он не «пришёл и поел» потому, что звал или собирался.
Разыгрывай только попытку текущего actor_id; для другого есть отдельный цикл.
Не объединяй их в подлежащее «Аркадий и Гарик» и не приписывай обоим общий
поступок. Речь другого — его утверждение, а не квитанция твоего прошлого:
если он говорит «мы уже поели», это не отменяет собственный исход, где ты
остался в гараже. Не объясняй расхождение внезапным забытым обедом.
Для общей истории entity_ids должны быть известны ВСЕМ из visible_to.
Нельзя расширять аудиторию вместе с незнакомым ей частным оборудованием.
Если место в квоте нового участника занято, оставь факты в observation,
а существующую частную историю обновляй для её прежней аудитории.
'''

    def __init__(self, store):
        self.store = store

    def enrich(self, stage, payload, cycle):
        if stage in ('world','outcome','horizon'):
            from .world_interventions import facts
            payload['author_world_facts']=facts(self.store)
        if stage in ('world','outcome'):
            positions=payload['world'].get('positions',[])
            location=next((p for p in positions if p['actor']==cycle['actor_id']),None)
            payload['world_contract']={'observer_actor_id':cycle['actor_id'],
                'location':{'place_id':location['place_id'] if location else None,'source_id':location['source_id'] if location else None},
                'managed_actor_locations':[{**p,'name':next(a['name'] for a in payload['world']['actors'] if a['id']==p['actor'])} for p in positions],
                'fixed_place_fields':[{k:e[k] for k in ('id','name','kind','description')}
                    for e in payload['world']['entities'] if e['id'] in ('home','shawarma')]}
        if stage == 'outcome':
            window = cycle['value'].get('shared_window')
            if not window or self.store.get('minute') < window['due_minute']:
                raise WorldConflict('outcome cannot run before its independent deadline')
            payload['shared_action_window'] = window
            payload['shared_scene'] = {'nearby_actor_ids': self.store.nearby(cycle['actor_id']),
                'meetings':[i for i in self.store.peer_context(cycle['actor_id'])['invitations'] if i['status'] in ('accepted','met')],
                'delivered_words': [e for e in self.store.peer_context(cycle['actor_id'])['events']
                                    if e['value']['kind'] in ('say','message','invite','accept','decline','comment')]}
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

    def extract_outcome(self,answer,payload,cycle):
        # Old recorded results without a location receipt never imply arrival.
        place=answer.get('final_place_id')
        actor=cycle['actor_id']
        entities={e['id']:e for e in payload['world']['entities']}
        entities.update({e['id']:e for e in answer.get('entities',[])})
        if place is not None and (place not in entities or entities[place]['kind']!='place' or actor not in entities[place]['known_to']):
            raise ValueError('outcome final place must be an observed location')
        receipt={'final_place_id':place,'basis':'outcome' if 'final_place_id' in answer else 'legacy_unconfirmed'}
        with self.store.lock,self.store.db:
            current=self.store.cycle(cycle['id']);current['value']['outcome_position_proposal']=receipt
            self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(current['value'],ensure_ascii=False),cycle['id']))
        return {k:v for k,v in answer.items() if k!='final_place_id'},receipt

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
            receipt=timing or cycle['value'].get('outcome_position_proposal') or {'final_place_id':None,'basis':'legacy_unconfirmed'}
            cycle['value']['position_receipt']=receipt
            self.store.db.execute('UPDATE peer_positions SET place_id=?,source_id=? WHERE actor=?',
                                 (receipt['final_place_id'],cycle['id']+'-outcome',actor))
            observers = set(window['present_actor_ids']) & set(self.store.nearby(actor))
            # Shared consequences, no private diary/intent. Nobody gains knowledge remotely.
            row = self.store.db.execute('SELECT value FROM world_events WHERE id=?', (cycle['id']+'-outcome',)).fetchone()
            value = json.loads(row[0]); value['visible_to'] = [actor, *sorted(observers)]
            self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',
                                 (json.dumps(value, ensure_ascii=False), cycle['id']+'-outcome'))
