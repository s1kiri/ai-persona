"""The only actions accepted from the character in the first local milestone."""
import json


SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'reply': {'type': ['string', 'null']},
        'state': {'type': 'string'},
        'goals': {'type': 'string'},
        'wake_after_minutes': {'type': 'integer', 'minimum': 5, 'maximum': 10080},
        'memories': {
            'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'key': {'type': 'string'},
                    'kind': {'type': 'string', 'enum': ['person', 'event', 'project']},
                    'basis': {'type': 'string', 'enum': ['reported', 'observed', 'fiction']},
                    'text': {'type': 'string'},
                }, 'required': ['key', 'kind', 'basis', 'text'],
            },
        },
    },
    'required': ['reply', 'state', 'goals', 'wake_after_minutes', 'memories'],
}


def bounded_text(value, field, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError('invalid %s (text limit %d)' % (field, limit))
    return value


def validate(value):
    """Validate before ANY memory, goal or outbox writes; schema is not sufficient."""
    if not isinstance(value, dict) or set(value) != set(SCHEMA['required']):
        raise ValueError('decision must contain exactly the protocol fields')
    if value['reply'] is not None:
        bounded_text(value['reply'], 'reply', 12000)
    bounded_text(value['state'], 'state', 6000)
    bounded_text(value['goals'], 'goals', 6000)
    wake = value['wake_after_minutes']
    if type(wake) is not int or not 5 <= wake <= 10080:
        raise ValueError('wake_after_minutes must be an integer between 5 and 10080')
    memories = value['memories']
    if not isinstance(memories, list) or len(memories) > 8:
        raise ValueError('at most 8 memories per turn')
    keys = set()
    for memory in memories:
        if not isinstance(memory, dict) or set(memory) != {'key', 'kind', 'basis', 'text'}:
            raise ValueError('invalid memory fields')
        bounded_text(memory['key'], 'memory key', 80)
        bounded_text(memory['text'], 'memory text', 2000)
        if memory['kind'] not in ('person', 'event', 'project'):
            raise ValueError('invalid memory kind')
        if memory['basis'] not in ('reported', 'observed', 'fiction'):
            raise ValueError('invalid memory basis')
        if memory['key'] in keys:
            raise ValueError('duplicate memory key in decision')
        keys.add(memory['key'])
    return value


def decode(text):
    return validate(json.loads(text))
