"""Structured decisions; the host alone applies memory and journal actions."""
import json


def obj(properties):
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}


def text():
    return {'type':'string'}


NOTE = obj({'key':text(),'kind':{'type':'string','enum':['person','event','project']},
            'basis':{'type':'string','enum':['reported','observed','fiction']},'text':text(),
            'visibility':{'type':'string','enum':['person','self']},
            'entities':{'type':'array','items':text()}})
RELATION = obj({'key':text(),'subject':text(),'predicate':text(),'object':text(),'text':text(),
                'basis':{'type':'string','enum':['reported','observed','fiction']},
                'visibility':{'type':'string','enum':['person','self']}})
JOURNAL = obj({'kind':{'type':'string','enum':['reflection','plan','story']},'title':text(),'text':text()})
SCHEMA = obj({'reply':{'type':['string','null']},'state':text(),'goals':text(),
              'wake_after_minutes':{'type':'integer','minimum':5,'maximum':10080},
              'memories':{'type':'array','items':NOTE},
              'relations':{'type':'array','items':RELATION},
              'journal':{'type':'array','items':JOURNAL},
              'memory_query':{'type':['string','null']}})
BASE_FIELDS = {'reply','state','goals','wake_after_minutes','memories'}


def bounded_text(value, field, limit, allow_empty=False):
    if not isinstance(value,str) or len(value)>limit or (not allow_empty and not value.strip()):
        raise ValueError('invalid %s (text limit %d)' % (field,limit))
    return value


def validate(value):
    # V1 is accepted for archived/synthetic decisions. CLI output is constrained by the full V2 schema.
    if not isinstance(value,dict) or not BASE_FIELDS <= set(value) or not set(value) <= set(SCHEMA['required']):
        raise ValueError('decision must contain exactly the protocol fields')
    if value['reply'] is not None:
        bounded_text(value['reply'],'reply',12000)
    bounded_text(value['state'],'state',6000)
    bounded_text(value['goals'],'goals',6000)
    wake=value['wake_after_minutes']
    if type(wake) is not int or not 5<=wake<=10080:
        raise ValueError('wake_after_minutes must be an integer between 5 and 10080')
    for field,limit in (('memories',8),('relations',8),('journal',2)):
        entries=value.get(field,[])
        if not isinstance(entries,list) or len(entries)>limit:
            raise ValueError('invalid action count: '+field)
        keys=set()
        for entry in entries:
            if not isinstance(entry,dict):
                raise ValueError('invalid '+field)
            if field=='journal':
                if set(entry)!=set(JOURNAL['required']) or entry['kind'] not in ('reflection','plan','story'):
                    raise ValueError('invalid journal entry')
                bounded_text(entry['title'],'journal title',160)
                bounded_text(entry['text'],'journal text',5000)
                continue
            required={'key','kind','basis','text'} if field=='memories' else set(RELATION['required'])
            allowed=set(NOTE['required']) if field=='memories' else required
            if not required<=set(entry) or not set(entry)<=allowed:
                raise ValueError('invalid '+field+' fields')
            bounded_text(entry['key'],'memory key',80)
            bounded_text(entry['text'],'memory text',2000)
            if entry['basis'] not in ('reported','observed','fiction'):
                raise ValueError('invalid memory basis')
            if entry.get('visibility','person') not in ('person','self'):
                raise ValueError('invalid visibility')
            if entry['key'] in keys:
                raise ValueError('duplicate memory key in decision')
            keys.add(entry['key'])
            if field=='memories':
                if entry['kind'] not in ('person','event','project'):
                    raise ValueError('invalid memory kind')
                entities=entry.get('entities',[])
                if not isinstance(entities,list) or len(entities)>6:
                    raise ValueError('invalid entities')
                for entity in entities:
                    bounded_text(entity,'entity',80)
            else:
                for k in ('subject','predicate','object'):
                    bounded_text(entry[k],k,80)
    query=value.get('memory_query')
    if query is not None:
        bounded_text(query,'memory query',400)
        if value['reply'] is not None or value['memories'] or value.get('relations') or value.get('journal'):
            raise ValueError('memory query must not also speak or write actions')
    return value


def decode(text):
    return validate(json.loads(text))
