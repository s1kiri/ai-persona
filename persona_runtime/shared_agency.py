"""Durable social agreements; words, attempted actions and outcomes stay distinct.

All writes run inside the owner's transaction. Append-only versions let a frozen
chat visit keep its original time boundary instead of learning a future result.
"""
import copy
import json

from .world import WorldConflict

ACTIVE_MEETINGS = ('pending', 'accepted', 'met')
ACTIVE_PROMISES = ('pending', 'in_progress', 'needs_review')


def setup(store):
    store.db.executescript('''
        CREATE TABLE IF NOT EXISTS peer_agency_events(
            seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,item_id TEXT NOT NULL,
            actor TEXT,value TEXT NOT NULL,minute INTEGER NOT NULL,source_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS peer_commitments(
            id TEXT PRIMARY KEY,actor TEXT NOT NULL,value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS peer_agency_time ON peer_agency_events(kind,minute,item_id);
    ''')


def record(store, kind, item, source):
    store.db.execute('INSERT INTO peer_agency_events(kind,item_id,actor,value,minute,source_id) VALUES(?,?,?,?,?,?)',
        (kind,item['id'],item.get('actor'),json.dumps(item,ensure_ascii=False),store.get('minute'),source))


def meeting(store, identity, status, source):
    row=store.db.execute('SELECT * FROM peer_invitations WHERE id=?',(identity,)).fetchone()
    if not row:raise WorldConflict('meeting does not exist')
    item={**dict(row),'status':status,'status_source_id':source}
    store.db.execute('UPDATE peer_invitations SET status=? WHERE id=?',(status,identity))
    record(store,'meeting',item,source)
    if status not in ACTIVE_MEETINGS:
        for promise in promises(store):
            if promise['invitation_id']==identity and promise['status'] in ACTIVE_PROMISES:
                # Ending a meeting removes the arrival obligation, not the
                # actor's unfinished project. Its fate remains their decision.
                next_status='cancelled' if promise.get('goal_kind')=='arrival' else 'needs_review'
                save_promise(store,{**promise,'status':next_status,'closed_by_meeting':status},source)


def promises(store):
    return [json.loads(r[0]) for r in store.db.execute('SELECT value FROM peer_commitments ORDER BY rowid')]


def save_promise(store, value, source):
    store.db.execute('INSERT INTO peer_commitments VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
        (value['id'],value['actor'],json.dumps(value,ensure_ascii=False)))
    record(store,'commitment',value,source)


def view(store, actor, minute):
    versions={}
    for row in store.db.execute('SELECT * FROM peer_agency_events WHERE seq IN '
            '(SELECT MAX(seq) FROM peer_agency_events WHERE minute<=? GROUP BY kind,item_id) ORDER BY seq',(minute,)):
        versions[(row['kind'],row['item_id'])]=json.loads(row['value'])
    # Legacy invitations keep their original proposal and status until explicitly
    # reviewed. They aren't expired merely because the program was restarted.
    meetings=[]
    for row in store.db.execute('SELECT i.*,e.minute FROM peer_invitations i JOIN world_events e ON e.id=i.source_id WHERE sender=? OR recipient=?',(actor,actor)):
        value=versions.get(('meeting',row['id']))
        if value is None:
            if row['minute']>minute:continue
            # A version created later must not leak its final status backwards.
            has_versions=store.db.execute("SELECT 1 FROM peer_agency_events WHERE kind='meeting' AND item_id=?",(row['id'],)).fetchone()
            value={k:row[k] for k in ('id','sender','recipient','place_id','status','source_id')}
            if has_versions:value['status']='pending'
        meetings.append(value)
    own=[value for (kind,_),value in versions.items() if kind=='commitment' and value['actor']==actor]
    return meetings,own


def reconsiderable(store, actor, minute):
    row=store.db.execute("SELECT id FROM world_cycles WHERE actor_id=? AND status='waiting' AND phase='outcome'",(actor,)).fetchone()
    if not row:return None
    cycle=store.cycle(row[0]);window=cycle['value'].get('shared_window',{})
    if (not window or cycle['value'].get('interruption') or
            not window['started_minute']<minute<window['due_minute'] or
            store.db.execute("SELECT 1 FROM world_calls WHERE cycle_id=? AND stage='outcome'",(row[0],)).fetchone()):return None
    return {'cycle_id':row[0],'started_minute':window['started_minute'],
            'elapsed_minutes':minute-window['started_minute'],'original_due_minute':window['due_minute']}


def validate_peer(store, actor, answer, payload):
    peers=payload['peer_context'];available=peers.get('own_commitments',[])
    if answer['activity']=='reconsider':
        action=peers.get('reconsider_action')
        if not action or action!=reconsiderable(store,actor,store.get('minute')):
            raise ValueError('only a waiting, uncomputed action can be reconsidered')
        triggers=[e for e in peers['events'] if e['id'] in answer['source_ids'] and
                  not e['value']['outbound'] and e['minute']>=action['started_minute']]
        if not triggers:raise ValueError('reconsideration needs a received message during this action')
    promise=answer['commitment']
    if promise:
        if answer['decision']=='keep' or not promise['action'].strip():
            raise ValueError('a new promise requires delivered words')
        places={e['id'] for e in payload['life']['entities'] if e['kind']=='place'}
        if promise['destination_id'] is not None and promise['destination_id'] not in places:
            raise ValueError('promise destination must be known')
    ids=[u['id'] for u in answer['commitment_updates']]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate commitment update')
    for update in answer['commitment_updates']:
        item=next((p for p in available if p['id']==update['id'] and p['status'] in ACTIVE_PROMISES),None)
        if not item:raise ValueError('only own active commitments can be updated')
        if update['status']=='fulfilled':
            source=update['source_id']
            row=store.db.execute("SELECT cycle_id FROM world_events WHERE id=? AND actor_id=? AND stage='outcome'",(source,actor)).fetchone()
            if source not in payload['available_source_ids'] or not row or row[0] not in item.get('attempt_ids',[]):
                raise ValueError('fulfilled promise needs its own linked outcome receipt')
        elif update['source_id'] is not None and update['source_id'] not in payload['available_source_ids']:
            raise ValueError('commitment update source unavailable')


def apply_local(store, actor, answer, source):
    """Own decision is accepted once, independently of outgoing delivery success."""
    for update in answer['commitment_updates']:
        item=next(p for p in promises(store) if p['id']==update['id'] and p['actor']==actor)
        save_promise(store,{**item,'status':update['status'],'resolution_source_id':update['source_id']},source)
    if answer['activity']!='reconsider':return
    info=reconsiderable(store,actor,store.get('minute'))
    if not info:raise WorldConflict('action no longer reconsiderable')
    cycle=store.cycle(info['cycle_id']);value=cycle['value']
    value['original_shared_window']=copy.deepcopy(value['shared_window'])
    value['interruption']={'decision_source_id':source,'message_source_ids':answer['source_ids'],
        'at_minute':store.get('minute'),'meaning':'Only elapsed prefix is being resolved; remaining plan is unexecuted.'}
    value['shared_window'].update(duration_minutes=info['elapsed_minutes'],due_minute=store.get('minute'))
    # Remain parked until the scheduler rebases the due prefix against the fresh
    # world. An outgoing reply may be delivered before this next tick.
    store.db.execute("UPDATE world_cycles SET status='waiting',revision=?,value=? WHERE id=?",
        (store.get('revision')+1,json.dumps(value,ensure_ascii=False),cycle['id']))
    record(store,'interruption',{'id':cycle['id'],'actor':actor,**value['interruption']},source)


def delivered(store, actor, answer, source, job_id):
    """Promises activate only once the actual words have a delivery receipt."""
    item=answer.get('commitment')
    invite=job_id if answer['decision']=='invite' else answer.get('invitation_id') if answer['decision']=='accept' else None
    if answer['decision']=='accept':
        row=store.db.execute('SELECT place_id FROM peer_invitations WHERE id=?',(invite,)).fetchone()
        item=item or {'action':'Добраться на принятую встречу.','destination_id':row[0]}
    if not item:return
    identity='promise-'+job_id.removeprefix('peerturn-')
    if store.db.execute('SELECT 1 FROM peer_commitments WHERE id=?',(identity,)).fetchone():return
    # A new explicit promise replaces an unfulfilled equivalent, without losing
    # the old words or claiming they were completed.
    for old in promises(store):
        if (old['actor']==actor and old['status'] in ACTIVE_PROMISES and
                ((invite and old['invitation_id']==invite) or old['action']==item['action'])):
            save_promise(store,{**old,'status':'cancelled','superseded_by':identity},source)
    save_promise(store,{'id':identity,'actor':actor,'action':item['action'],
        'destination_id':item['destination_id'],'invitation_id':invite,
        'goal_kind':'arrival' if answer['decision']=='accept' and item['destination_id']==row[0] else 'action',
        'status':'pending','quote':answer['text'],'source_id':source,'attempt_ids':[]},source)


def bind(store, cycle, identities):
    for identity in identities:
        item=next((p for p in promises(store) if p['id']==identity and p['actor']==cycle['actor_id'] and p['status'] in ACTIVE_PROMISES),None)
        if not item:raise ValueError('physical action must link an own active promise')
        save_promise(store,{**item,'status':'in_progress','attempt_ids':[*item['attempt_ids'],cycle['id']]},cycle['id']+'-choice')


def outcome(store, cycle):
    actor=cycle['actor_id'];source=cycle['id']+'-outcome'
    place=cycle['value']['position_receipt']['final_place_id']
    for item in promises(store):
        if item['actor']!=actor or cycle['id'] not in item['attempt_ids'] or item['status'] not in ACTIVE_PROMISES:continue
        # Arrival only fulfills the arrival leg. An arbitrary promised project
        # needs the actor's explicit assessment of its linked physical outcome.
        arrived=item.get('goal_kind')=='arrival' and place is not None and place==item['destination_id']
        save_promise(store,{**item,'status':'fulfilled' if arrived else 'needs_review',
                           'outcome_source_id':source},source)
    for row in list(store.db.execute("SELECT * FROM peer_invitations WHERE (sender=? OR recipient=?) AND status IN ('accepted','met')",(actor,actor))):
        if row['status']=='met' and place is not None and place!=row['place_id']:
            meeting(store,row['id'],'completed',source)
        elif row['status']=='accepted':
            positions={r['actor']:r['place_id'] for r in store.db.execute('SELECT * FROM peer_positions')}
            if positions.get(row['sender'])==positions.get(row['recipient'])==row['place_id']:
                meeting(store,row['id'],'met',source)
