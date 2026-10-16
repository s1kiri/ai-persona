#!/usr/bin/env python3
"""Finite isolated comparison of world direction with identical character speech.

No Telegram connection, production database, reader messages or image tool.
Failed stages are retained and never retried. Outputs are private by default.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from persona_runtime.app_server import save_private
from persona_runtime.channel_social import SocialMemory
from persona_runtime.proxy import SharedTransport
from persona_runtime.world import WorldStore,WorldSession
from persona_runtime.world_backend import WorldBackend


def seed(store):
    places=[('courtyard','Двор и улица у дома','Обычный жилой двор, тротуар, скамейки и проход к магазинам.'),
            ('corner_shop','Магазин у дома','Обычный небольшой магазин продуктов и хозяйственных мелочей.'),
            ('market','Развал у рынка','Прилавки с подержанными вещами и обычные торговцы. Нет реального адреса.')]
    with store.lock,store.db:
        for identity,name,description in places:
            value={'id':identity,'kind':'place','name':name,'description':description,
                   'condition':'Обычный вечер, место доступно.','known_to':['garik'],
                   'world_note':'Собственные дела людей не определены; сцены ещё не произошли.'}
            store.db.execute('INSERT INTO world_entities VALUES(?,?,?)',(identity,json.dumps(value,ensure_ascii=False),'synthetic-evaluation-fixture'))
        store.put('minute',1080)
        store.put('life_controls',{'photos':False,'episode_minutes':15,'sharing_appetite':80})
        store.put('channel_attention',{'purpose':'own_channel','focus_ids':[]})
        store.put('channel_connection',{'explanation':'Свой канал уже есть; можно написать или оставить происходящее себе. Внешние отправки этого теста отключены.'})
        store.put('settings',{**store.get('settings'),'events_per_hour':2.5,'surprise':65,'continuity':70,
                              'quiet':15,'max_threads':5,'post_cooldown_minutes':0,'director_empathy':50})
        store.pause(False)
        store.snapshot()


def capture_drafts(store):
    # A local recording sink, explicitly simulated; never a Telegram receipt.
    with store.lock,store.db:
        for row in store.db.execute("SELECT id,value FROM world_events WHERE stage='post'").fetchall():
            value=json.loads(row['value'])
            if value.get('delivery'):continue
            value.update(draft=False,delivery={'status':'sent','simulated':True,'transport':'isolated-recording-sink'})
            store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value,ensure_ascii=False),row['id']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--persona',required=True,type=Path)
    parser.add_argument('--speech-contract',required=True,type=Path)
    parser.add_argument('--episodes',type=int,default=3)
    parser.add_argument('--max-calls',type=int,default=48)
    parser.add_argument('--enable-model',action='store_true')
    args=parser.parse_args()
    out=args.output.resolve()
    if not args.enable_model:parser.error('real calls require --enable-model')
    if not 1<=args.episodes<=6 or not 1<=args.max_calls<=100:parser.error('invalid finite evaluation limits')
    if ROOT/'.local' not in out.parents:parser.error('private evaluation output must live under .local/')
    out.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker=out/'started.json'
    if marker.exists():parser.error('evaluation already started; no automatic replay')
    persona=args.persona.read_text();contract=args.speech_contract.read_text()
    if not persona.strip() or not contract.strip():parser.error('empty character instructions')
    manifest={'persona_sha256':hashlib.sha256(persona.encode()).hexdigest(),
              'fixture_version':2,
              'speech_contract_sha256':hashlib.sha256(contract.encode()).hexdigest(),
              'episodes_per_branch':args.episodes,'max_calls_total':args.max_calls,
              'external_publication':False,'image_generation':False,'reader_data':False,'initial_state_hashes':{}}
    save_private(marker,manifest)
    owner=SharedTransport();backend=WorldBackend(out/'transport',transport_factory=owner.borrow,max_known_tokens=90000)
    manifest.update(model=backend.model,reasoning_effort=backend.reasoning_effort)
    calls=0;deadline=time.monotonic()+2400;report={'manifest':manifest,'branches':{}}
    try:
        for style in ('current','lively'):
            store=WorldStore(out/style);seed(store)
            manifest['initial_state_hashes'][style]=hashlib.sha256(json.dumps(store.state(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            social=SocialMemory(store)
            session=WorldSession(store,backend,{'garik':persona},contract,max_calls=args.max_calls,
                personal_life=True,active_world=True,lively_world=style=='lively',lossless_intentions=True,
                autonomous_posts=True,direct_channel=True,external_channel=True,channel_extension=social)
            branch={'episodes':[],'status':'running'};report['branches'][style]=branch
            try:
                for index in range(args.episodes):
                    cid=session.prepare(minutes=5,draw=(.12,.65,.2,.8,.15,.5)[index])['id']
                    while store.cycle(cid)['status']=='ready':
                        if calls>=args.max_calls or time.monotonic()>deadline:raise RuntimeError('finite evaluation limit reached; no retry')
                        phase=store.cycle(cid)['phase'];calls+=1
                        progress={'branch':style,'episode':index+1,'phase':phase,'calls_total':calls,'status':'started'}
                        save_private(out/'progress.json',progress);print(json.dumps(progress),flush=True)
                        session.step(cid)
                        progress.update(status='completed',usage=backend.last_usage)
                        save_private(out/'progress.json',progress);print(json.dumps(progress),flush=True)
                    capture_drafts(store)
                    branch['episodes'].append(store.cycle(cid))
                    save_private(out/'results.json',report)
                branch['status']='complete'
            except Exception as error:
                branch.update(status='stopped',error_type=type(error).__name__,automatic_retry=False)
                # Keep all committed stages and the rejected answer for diagnosis.
                branch['state']=store.monitor()
                print(json.dumps({'branch':style,'status':'stopped','error_type':type(error).__name__}),flush=True)
            finally:
                store.pause(True);branch['state']=store.monitor();store.close()
                save_private(out/'results.json',report)
        report['calls_total']=calls
        report['identical_initial_state']=len(set(manifest['initial_state_hashes'].values()))==1
        save_private(out/'results.json',report)
        print(json.dumps({'status':'finished','calls_total':calls,'identical_initial_state':report['identical_initial_state']}),flush=True)
    finally:
        backend.close();owner.close()


if __name__=='__main__':main()
