"""The fictional world proposes state changes; only the host commits them."""
import re

from .protocol import obj, bounded_text

DOMAINS = ('home', 'work', 'food', 'people', 'leisure', 'city', 'learning', 'money', 'rest')
BEATS = ('opportunity', 'encounter', 'discovery', 'complication', 'consequence', 'return', 'progress', 'closure', 'quiet')


def string(limit):
    return {'type': 'string', 'maxLength': limit}


def enum(values):
    return {'type': 'string', 'enum': list(values)}


def array(value, limit):
    return {'type': 'array', 'items': value, 'maxItems': limit}


ENTITY = obj({'id': string(80), 'kind': enum(('person', 'place', 'thing')),
              'name': string(100), 'description': string(600),
              'condition': string(600), 'known_to': array(string(80), 8),
              'world_note': string(400)})
THREAD = obj({'id': string(80), 'title': string(140), 'domain': enum(DOMAINS),
              'status': enum(('open', 'waiting', 'closed')),
              'situation': string(800), 'open_question': string(300),
              'due_minute': {'type': ['integer', 'null'], 'minimum': 0},
              'entity_ids': array(string(80), 6), 'visible_to': array(string(80), 8)})
PATCH = {'entities': array(ENTITY, 5), 'threads': array(THREAD, 4)}
EVENT_SCHEMA = obj({'beat': enum(BEATS), 'domain': enum(DOMAINS),
                    'pattern': string(80), 'thread_id': {'type': ['string', 'null'], 'maxLength': 80},
                    'causes': array(string(80), 4), 'entity_ids': array(string(80), 6),
                    'observation': string(1800), 'world_note': string(800),
                    'visible_to': array(string(80), 8),
                    'surprise': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                    **PATCH})
CHOICE_SCHEMA = obj({'desire': string(500), 'kind': enum(('act', 'wait', 'rest', 'research')),
                     'action': string(1024), 'entity_ids': array(string(80), 6),
                     'note': string(800), 'reaction': string(600),
                     'undertakings': array(obj({'id': string(80), 'want': string(500),
                                               'status': enum(('active', 'waiting', 'closed'))}), 3),
                     'revisit_minutes': {'type': 'integer', 'minimum': 5, 'maximum': 10080}})
OUTCOME_SCHEMA = obj({'status': enum(('succeeded', 'partial', 'failed', 'pending')),
                      'observation': string(1800), 'world_note': string(800),
                      'elapsed_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
                      'time_costs': {**array(obj({'activity': string(140),
                                                 'minutes': {'type': 'integer', 'minimum': 0, 'maximum': 1440}}), 8),
                                     'minItems': 1},
                      **PATCH})
SHARE_SCHEMA = obj({'decision': enum(('post', 'reply', 'keep')), 'focus': string(600),
                    'source_ids': array(string(80), 6)})
CONCERN = obj({'id': string(80), 'text': string(500), 'stake': string(300),
               'next_hint': string(300), 'status': enum(('active', 'waiting', 'closed')),
               'source_ids': array(string(80), 4)})
EXPERIENCE_SCHEMA = obj({'note': string(800), 'concerns': array(CONCERN, 3),
                          'director_signal': string(300)})
AUDIENCE_SCHEMA = obj({'post_id': string(80),
                       'comments': array(obj({'speaker': string(60), 'text': string(500)}), 3)})
SCHEMAS = {'world': EVENT_SCHEMA, 'choice': CHOICE_SCHEMA, 'outcome': OUTCOME_SCHEMA,
           'share': SHARE_SCHEMA, 'experience': EXPERIENCE_SCHEMA, 'audience': AUDIENCE_SCHEMA}


def clock_at(minute):
    return {'minute': minute, 'day': minute // 1440 + 1,
            'time': f'{minute % 1440 // 60:02d}:{minute % 60:02d}'}


def validate_clock(observation, minute):
    # Only explicit claims about "now"; appointment times remain legitimate.
    for match in re.finditer(r'\b(?:сейчас|на часах)\s+(?:(?:уже|около|примерно)\s+)*(\d{1,2}):(\d{2})', observation, re.I):
        hour, minutes = map(int, match.groups())
        if hour > 23 or minutes > 59 or hour * 60 + minutes != minute % 1440:
            raise ValueError('narrated current time disagrees with the world clock')


def validate_shape(value, schema, path='result'):
    """Validate the same closed schema locally, including exact types and sizes."""
    kind = schema['type']
    if isinstance(kind, list):
        if value is None and 'null' in kind:
            return
        kind = next(k for k in kind if k != 'null')
    if kind == 'object':
        if not isinstance(value, dict) or set(value) != set(schema['required']):
            raise ValueError('invalid fields: ' + path)
        for key, item in value.items():
            validate_shape(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list) or not schema.get('minItems', 0) <= len(value) <= schema['maxItems']:
            raise ValueError('invalid array: ' + path)
        for item in value:
            validate_shape(item, schema['items'], path)
    elif kind == 'string':
        bounded_text(value, path, schema.get('maxLength', 2000), allow_empty=True)
    elif kind == 'integer':
        if type(value) is not int or not schema.get('minimum', 0) <= value <= schema.get('maximum', 10**12):
            raise ValueError('invalid integer: ' + path)
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('invalid boolean: ' + path)
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('invalid enum: ' + path)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,79}', value):
        raise ValueError('invalid fictional identity')


def validate_patch(value, state, settings):
    ids = {e['id'] for e in state['entities']}
    actors = {a['id'] for a in state['actors']}
    patch_ids = set()
    for entity in value['entities']:
        identifier(entity['id'])
        if entity['id'] in patch_ids:
            raise ValueError('duplicate entity')
        if not entity['name'].strip():
            raise ValueError('empty entity name')
        previous = next((e for e in state['entities'] if e['id'] == entity['id']), None)
        if entity['id'] in actors and (not previous or any(entity[k] != previous[k] for k in entity if k != 'known_to')):
            raise ValueError('actor identity and state cannot be rewritten by the world')
        if entity['id'] in ('home', 'shawarma') and previous and any(entity[k] != previous[k] for k in ('name', 'kind', 'description')):
            raise ValueError('author-established place cannot be rewritten')
        if previous and previous['kind'] != entity['kind']:
            raise ValueError('entity kind cannot change')
        if not set(entity['known_to']) <= actors:
            raise ValueError('unknown observing actor')
        if previous and not set(previous['known_to']) <= set(entity['known_to']):
            raise ValueError('known entity cannot become unknown')
        patch_ids.add(entity['id'])
    ids |= patch_ids
    entity_map = {e['id']: e for e in state['entities']}
    entity_map.update({e['id']: e for e in value['entities']})
    threads = {t['id']: t for t in state['threads']}
    updated = set()
    for thread in value['threads']:
        identifier(thread['id'])
        if thread['id'] in updated or not thread['title'].strip() or not thread['situation'].strip():
            raise ValueError('invalid thread update')
        if not set(thread['visible_to']) <= actors:
            raise ValueError('unknown thread audience')
        if not set(thread['entity_ids']) <= ids:
            raise ValueError('unknown thread entity')
        if any(observer not in entity_map[entity]['known_to'] for observer in thread['visible_to'] for entity in thread['entity_ids']):
            raise ValueError('story reveals an undiscovered entity')
        old = threads.get(thread['id'])
        if old and old['status'] == 'closed' and thread['status'] != 'closed':
            raise ValueError('closed history needs a new thread referencing it')
        if thread['status'] == 'closed' and thread['due_minute'] is not None:
            raise ValueError('closed thread cannot have a deadline')
        if thread['due_minute'] is not None and thread['due_minute'] < state['minute']:
            raise ValueError('thread deadline is in the past')
        threads[thread['id']] = thread
        updated.add(thread['id'])
    active_before = sum(t['status'] != 'closed' for t in state['threads'])
    active_after = sum(t['status'] != 'closed' for t in threads.values())
    # Lowering a slider does not destroy existing stories, but blocks new ones.
    if active_after > max(active_before, settings['max_threads']):
        raise ValueError('too many open stories')
    return ids, threads


def validate_result(stage, value, state, brief):
    validate_shape(value, SCHEMAS[stage])
    if stage == 'audience':
        if any(not c['speaker'].strip() or not c['text'].strip() for c in value['comments']):
            raise ValueError('empty fictional comment')
        return value
    if stage == 'experience':
        ids = set()
        for concern in value['concerns']:
            identifier(concern['id'])
            if concern['id'] in ids or not concern['text'].strip() or not concern['source_ids']:
                raise ValueError('personal concern needs identity, text and evidence')
            ids.add(concern['id'])
            if not set(concern['source_ids']) <= set(state.get('personal_evidence_ids', [])):
                raise ValueError('personal concern refers to an unobserved source')
        return value
    if stage == 'share':
        available = {e['id'] for e in state['history'] if e['stage'] in ('world', 'outcome', 'research', 'audience') and
                     brief['actor_id'] in e['value'].get('visible_to', [e['actor_id']])}
        available.update(state.get('public_evidence_ids', []))
        if not set(value['source_ids']) <= available:
            raise ValueError('post source must be an observed world event')
        if value['decision'] != 'keep' and (not value['focus'].strip() or not value['source_ids']):
            raise ValueError('post needs a personal focus and a stored event')
        return value
    if not value.get('observation', value.get('action', '')).strip():
        raise ValueError('empty observation/action')
    actor = brief['actor_id']
    known = {e['id'] for e in state['entities'] if actor in e['known_to']}
    if stage == 'choice':
        if not set(value['entity_ids']) <= known:
            raise ValueError('Garik cannot act on undiscovered entities')
        if not value['desire'].strip():
            raise ValueError('empty personal desire')
        ids = [u['id'] for u in value['undertakings']]
        for identity in ids:
            identifier(identity)
        if len(ids) != len(set(ids)) or any(not u['want'].strip() for u in value['undertakings']):
            raise ValueError('invalid personal undertaking')
        return value
    if stage == 'outcome':
        if sum(t['minutes'] for t in value['time_costs']) != value['elapsed_minutes']:
            raise ValueError('elapsed time must equal travel, waiting and activity costs')
        state = {**state, 'minute': state['minute'] + value['elapsed_minutes']}
    validate_clock(value['observation'], state['minute'])
    ids, threads = validate_patch(value, state, brief['settings'])
    if stage == 'world':
        if not brief['new_event_allowed']:
            existing_entities = {e['id'] for e in state['entities']}
            existing_threads = {t['id'] for t in state['threads']}
            if any(e['id'] not in existing_entities for e in value['entities']) or any(t['id'] not in existing_threads for t in value['threads']):
                raise ValueError('new incidents are not due; continue existing causes or stay quiet')
        if value['beat'] not in brief['allowed_beats']:
            raise ValueError('beat is unavailable at this moment')
        identifier(value['pattern'])
        if value['surprise'] > brief['surprise_ceiling']:
            raise ValueError('surprise exceeds current setting')
        if value['beat'] == 'quiet' and (value['entities'] or value['threads'] or value['surprise']):
            raise ValueError('quiet must not smuggle in new world events')
        if value['beat'] != 'quiet' and value['beat'] in ('opportunity', 'encounter', 'discovery', 'complication') and value['pattern'] in brief['recent_patterns']:
            raise ValueError('recent situation pattern repeated')
        if value['beat'] in ('opportunity', 'encounter', 'discovery', 'complication') and value['domain'] not in brief['allowed_domains']:
            raise ValueError('recently overused domain without a due consequence')
        if not set(value['causes']) <= set(brief['cause_ids']):
            raise ValueError('invented causal receipt')
        if not set(value['entity_ids']) <= ids:
            raise ValueError('unknown observation entity')
        if actor not in value['visible_to'] or not set(value['visible_to']) <= {a['id'] for a in state['actors']}:
            raise ValueError('invalid observation audience')
        visible = known | {e['id'] for e in value['entities'] if actor in e['known_to']}
        if not set(value['entity_ids']) <= visible:
            raise ValueError('observation points at backstage entities')
        entity_map = {e['id']: e for e in state['entities']}
        entity_map.update({e['id']: e for e in value['entities']})
        if any(observer not in entity_map[entity]['known_to'] for observer in value['visible_to'] for entity in value['entity_ids']):
            raise ValueError('shared observation reveals an undiscovered entity')
        thread_id = value['thread_id']
        if thread_id is not None and thread_id not in threads:
            raise ValueError('unknown story')
        if value['beat'] in ('consequence', 'return', 'closure') and not value['causes']:
            raise ValueError('continuation requires a stored cause')
    return value
