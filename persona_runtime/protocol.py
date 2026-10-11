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
MESSAGE = obj({'text':text(),'delay_seconds':{'type':'integer','minimum':0,'maximum':30}})
ACTIVITY = obj({'key':text(),'title':text(),'kind':{'type':'string','enum':['research','journal','reflection','fiction','conversation']},
                'status':{'type':'string','enum':['active','waiting','done','abandoned']},
                'next_step':text(),'reason':text(),'due_after_minutes':{'type':'integer','minimum':5,'maximum':10080}})
SOURCE = obj({'url':text(),'title':text(),'claim':text()})
SCHEMA = obj({'reply':{'type':['string','null']},'state':text(),'goals':text(),
              'wake_after_minutes':{'type':'integer','minimum':5,'maximum':10080},
              'memories':{'type':'array','items':NOTE},
              'relations':{'type':'array','items':RELATION},
              'journal':{'type':'array','items':JOURNAL},
              'memory_query':{'type':['string','null']},
              'web_query':{'type':['string','null']},
              'web_sources':{'type':'array','items':SOURCE},
              'messages':{'type':'array','items':MESSAGE,'maxItems':3},
              'activity':{'anyOf':[ACTIVITY,{'type':'null'}]},
              'speech_preference':{'type':['string','null'],'enum':['clean','contextual',None]},
              'outreach':{'type':['boolean','null']}})
BASE_FIELDS = {'reply','state','goals','wake_after_minutes','memories'}


def bounded_text(value, field, limit, allow_empty=False):
    if not isinstance(value,str) or len(value)>limit or (not allow_empty and not value.strip()):
        raise ValueError('invalid %s (text limit %d)' % (field,limit))
    return value


def validate(value):
    # V1/V2 are accepted for archived/synthetic decisions. CLI uses the full V3 schema.
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
    messages=value.get('messages',[])
    if not isinstance(messages,list) or len(messages)>3 or (messages and value['reply'] is not None):
        raise ValueError('choose reply or up to three separate messages')
    for message in messages:
        if not isinstance(message,dict) or set(message)!=set(MESSAGE['required']):
            raise ValueError('invalid message fields')
        bounded_text(message['text'],'message text',6000)
        if type(message['delay_seconds']) is not int or not 0<=message['delay_seconds']<=30:
            raise ValueError('message delay must be 0..30 seconds')
    activity=value.get('activity')
    if activity is not None:
        if not isinstance(activity,dict) or set(activity)!=set(ACTIVITY['required']):
            raise ValueError('invalid activity fields')
        for key,limit in [('key',80),('title',160),('next_step',600),('reason',300)]:
            bounded_text(activity[key],'activity '+key,limit,allow_empty=key=='next_step')
        if activity['kind'] not in ACTIVITY['properties']['kind']['enum'] or activity['status'] not in ACTIVITY['properties']['status']['enum']:
            raise ValueError('invalid activity kind/status')
        if type(activity['due_after_minutes']) is not int or not 5<=activity['due_after_minutes']<=10080:
            raise ValueError('invalid activity deadline')
    if value.get('speech_preference') not in (None,'clean','contextual') or (value.get('outreach') is not None and type(value['outreach']) is not bool):
        raise ValueError('invalid audience preference')
    sources=value.get('web_sources',[])
    if not isinstance(sources,list) or len(sources)>8:
        raise ValueError('invalid web sources')
    from urllib.parse import urlsplit
    for source in sources:
        if not isinstance(source,dict) or set(source)!=set(SOURCE['required']):
            raise ValueError('invalid source fields')
        for key,limit in [('url',2000),('title',300),('claim',800)]:
            bounded_text(source[key],'source '+key,limit)
        parsed=urlsplit(source['url'])
        if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('source must be a plain HTTP(S) URL without credentials')
    if value.get('web_query') is not None:
        bounded_text(value['web_query'],'web query',400)
    if value.get('memory_query') and value.get('web_query'):
        raise ValueError('request only one intermediate query at a time')
    query=value.get('memory_query') or value.get('web_query')
    if query is not None:
        bounded_text(query,'memory query',400)
        if value['reply'] is not None or messages or value['memories'] or value.get('relations') or value.get('journal') or activity or sources or value.get('speech_preference') is not None or value.get('outreach') is not None:
            raise ValueError('memory query must not also speak or write actions')
    return value


def decode(text):
    return validate(json.loads(text))
