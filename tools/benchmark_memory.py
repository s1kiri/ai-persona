#!/usr/bin/env python3
"""Small synthetic retrieval ablation. No Codex, no audience data, no downloads."""
import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from persona_runtime.store import Store
from persona_runtime.embedding import get_encoder
from persona_runtime.memory import Memory, Budget


def populate(store):
    def save(key,text,owner='operator',visibility='self',entities=None):
        event_id=store.enqueue('Synthetic fixture '+key,person=owner)
        event=store.claim()
        store.complete(event,{'reply':None,'state':'Synthetic retrieval evaluation only.',
                       'goals':'No actual character activity.','wake_after_minutes':60,
                       'memories':[{'key':key,'kind':'event','basis':'fiction','text':text,
                                    'visibility':visibility,'entities':entities or []}]},{})
        return event_id
    save('bass-colour','Garik wants a burgundy bass guitar.',entities=['bass'])
    targets={}
    targets['colour']=save('bass-colour','Garik now prefers a cream-white bass with a black pickguard.',entities=['bass'])
    targets['dog']=save('dog','On a cold night a ginger dog with a white chest warmed Garik outside a shawarma shop. His lost keys were in his coat lining.',entities=['dog','shawarma'])
    targets['shop']=save('shop','A music-shop seller accused Garik of damaging a bass guitar and threatened to call police. Fault is disputed; no purchase was completed.',entities=['bass','music-shop'])
    targets['sauce']=save('sauce','Garik intends to compare two hot sauces in his own notebook; no tasting has happened.',entities=['hot-sauce'])
    targets['music']=save('music','Garik likes McCartney melodic bass playing and the song Dear Prudence.',entities=['bass','music'])
    save('secret','Private passport and travel details, forbidden to another person.',owner='alice',visibility='person',entities=['bass'])
    for i in range(45):
        save('distractor-'+str(i),'Synthetic unrelated note about '+['curtains','laundry','a kettle','an umbrella','wall paint'][i%5]+', number '+str(i))
    # More people enlarge the archive without enlarging this person's eligible corpus.
    for i in range(1000):
        save('passing-comment','Synthetic passing comment '+str(i),owner='visitor-'+str(i),visibility='person')
    return targets


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    started=time.monotonic()
    encoder=get_encoder()
    load=time.monotonic()-started
    queries=[('colour','Какого цвета он теперь хочет инструмент?'),
             ('dog','Помнишь пса, который согрел тебя, когда ты ключи потерял?'),
             ('shop','Чем закончилась история с продавцом и полицией?'),
             ('sauce','Что ты собирался записать про острые соусы?'),
             ('music','McCartney Dear Prudence bass')]
    with tempfile.TemporaryDirectory() as directory:
        store=Store(directory)
        store.memory=Memory(store,encoder=encoder,budget=4000)
        targets=populate(store)
        event={'person':'operator','conversation':'fresh','kind':'message','text':''}
        result={'synthetic':True,'model':encoder.model_id,'encoder_load_seconds':round(load,3),
                'archive_people':1002,'budget':4000,'counter':store.memory.budget.name,'queries':len(queries),'modes':{}}
        for mode in ('recent','bm25','dense','hybrid'):
            hits=0; context_hits=0; measurements=[]
            for key,query in queries:
                event['text']=query
                start=time.monotonic()
                found,report=store.memory.search(event,query,mode=mode,limit=5)
                recall=any(v.get('source_event')==targets[key] for _,v in found[:5])
                hits+=recall
                context,context_report=store.memory.context(event,query=query,mode=mode)
                packed=any(v.get('source_event')==targets[key] for v in context['memories'])
                context_hits+=packed
                joined=json.dumps(context,ensure_ascii=False)
                assert 'Private passport' not in joined
                assert store.memory.budget.cost(context)<=4000
                measurements.append({'case':key,'hit_at_5':recall,'in_context':packed,
                                     'elapsed_ms':round((time.monotonic()-start)*1000,2),
                                     'context_cost':store.memory.budget.cost(context),
                                     'eligible_documents':report['authorized_documents']})
            result['modes'][mode]={'recall_at_5':hits/len(queries),'context_recall':context_hits/len(queries),'cases':measurements}
        result['peak_process_memory_mib']=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,1)
        result['total_seconds']=round(time.monotonic()-started,2)
        store.close()
    payload=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(payload+'\n',encoding='utf-8')
    print(payload)


if __name__=='__main__':
    main()
