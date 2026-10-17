"""Native models report discoveries; the owner preserves knowledge and assigns references."""
import copy
import json

MODE='observer_patch_v1'


def observer(payload):
    return payload.get('world_contract',{}).get('observer_actor_id') or payload.get('director',{}).get('actor_id')


def schema(value,stage,payload):
    value=copy.deepcopy(value);props=value['properties'];actor=observer(payload)
    props['patch_mode']={'type':'string','enum':[MODE]};value['required'].append('patch_mode')
    for field in ('entities','place_links'):
        item=props[field]['items'];audience=item['properties'].pop('known_to')
        audience['maxItems']=1
        if actor:audience['items']={'type':'string','enum':[actor]}
        item['properties']['learned_by']=audience
        item['required']=[('learned_by' if x=='known_to' else x) for x in item['required']]
    if stage=='world':
        if actor:props['visible_to']={'type':'array','items':{'type':'string','enum':[actor]},'minItems':1,'maxItems':1}
        refs=props.pop('entity_ids')
        known=[e['id'] for e in payload.get('world',{}).get('entities',[]) if actor in e['known_to']]
        if known:refs['items']={'type':'string','enum':known}
        else:refs['maxItems']=0
        props['observed_ids']=refs
        value['required']=[('observed_ids' if x=='entity_ids' else x) for x in value['required']]
    return value


def project(wire,stage,payload):
    if wire.get('patch_mode')!=MODE:raise ValueError('unknown world patch format')
    actor=observer(payload)
    if not actor:raise ValueError('world patch needs its observer')
    value=copy.deepcopy(wire);value.pop('patch_mode')
    allowed={actor}
    if stage=='world':allowed &= set(value['visible_to'])
    learned=[]
    for field,table in (('entities','entities'),('place_links','place_links')):
        old={e['id']:e for e in payload['world'].get(table,[])}
        for item in value[field]:
            audience=item.pop('learned_by')
            if not set(audience)<=allowed:raise ValueError('discovery needs the current observer')
            item['known_to']=list(dict.fromkeys([*old.get(item['id'],{}).get('known_to',[]),*audience]))
            if field=='entities' and actor in audience:learned.append(item['id'])
    if stage=='world':
        refs=value.pop('observed_ids')
        known={e['id'] for e in payload['world']['entities'] if actor in e['known_to']}
        if not set(refs)<=known:raise ValueError('observed reference must be known or explicitly discovered')
        candidates=list(dict.fromkeys([*learned,*refs]))
        anchors=payload.get('director',{}).get('exposure',{}).get('anchor_ids',[])
        # References are bounded metadata; all proposed facts still undergo core validation.
        value['entity_ids']=list(dict.fromkeys([*(a for a in anchors if a in candidates),*candidates]))[:6]
    return value


def extract(wire,stage,payload,store,cycle):
    if 'patch_mode' not in wire:return wire  # Existing saved answers keep the strict legacy contract.
    with store.lock,store.db:
        current=store.cycle(cycle['id']);current['value'].setdefault('native_world_outputs',{})[stage]=wire
        store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(current['value'],ensure_ascii=False),cycle['id']))
    return project(wire,stage,payload)


INSTRUCTIONS='''
В нативном формате patch_mode=observer_patch_v1 поля known_to нет.
В entities/place_links указывай learned_by: [] для отсутствия нового знания
или [текущий actor_id], если наблюдатель узнал это в текущем событии.
Прежних знающих хост сохранит сам; удалённому другу новое знание не выдаётся.
Общее прошлое и знакомая дорога не требуют приписывать ему наблюдение сейчас.
В world observed_ids выбирай из уже известных наблюдателю entities.
Чтобы открыть ему ещё неизвестную улицу/вещь, передай её полный entities-патч
с learned_by=[actor_id]; хост сам добавит ссылку наблюдения. Новые entities
также получают ссылку из learned_by. ID place_links сюда не попадают.
Все остальные поля патча сохраняют прежний смысл; текст и факты хост не пишет.
'''
