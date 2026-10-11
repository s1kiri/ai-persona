#!/usr/bin/env python3
"""Explicit native web test on an isolated synthetic database and enabled proxy."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.evaluate_character import Recorder,artifact_path
from persona_runtime import proxy
from persona_runtime.codex import CodexBackend
from persona_runtime.engine import Engine
from persona_runtime.store import Store,worker_lock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--max-calls',type=int,default=8)
    parser.add_argument('--wait-for-wake',action='store_true')
    parser.add_argument('--own-interest',action='store_true',help='documented synthetic fixture: own pending curiosity, not a human search request')
    parser.add_argument('--max-minutes',type=int,default=12)
    args=parser.parse_args()
    if not args.run_live or not 1<=args.max_calls<=12 or not 1<=args.max_minutes<=30:
        parser.error('explicit --run-live and bounded limits required')
    directory=artifact_path(args.out)
    if directory.exists():parser.error('preserve evidence; use a new directory')
    if not proxy.status()['enabled']:parser.error('user proxy must be enabled; no direct test')
    directory.mkdir(parents=True,mode=0o700)
    backend=Recorder(CodexBackend(timeout=90),directory,args.max_calls)
    store=Store(directory/'db');store.memory.dense=False
    engine=Engine(store,backend,web_enabled=True,max_web_calls=2)
    engine.runtime_limits={'autonomy_enabled':True,'autonomous_minutes_remaining':args.max_minutes,
                           'scheduled_wakes_remaining':2,'model_calls_remaining':args.max_calls}
    records=[];deadline=time.monotonic()+args.max_minutes*60
    try:
        with worker_lock(store.directory):
            if args.own_interest:
                with store.db:
                    store.put('state','Own pending curiosity about pumice buoyancy. I want to check a primary source now, without involving the operator. No sources accessed yet.')
                    store.put('goals','Personal intent: investigate how gas remains trapped in pumice; use actual native search if available and keep a source-linked finding for myself.')
                    store.db.execute("INSERT INTO audience_preferences VALUES (?,?,?,?)",(store.scope({'person':'operator','conversation':'local'}),'contextual',0,time.time()))
                event_id=store.enqueue('Scheduled local check-in; decide the next step in your own pending interest. No human task or participant.',
                    person='self',display_name='Гарик',conversation='local',register='auto',surface='private',kind='wake',event_id='own-interest-web')
            else:
                event_id=store.enqueue('Найди в интернете, почему пемза плавает. Дай ссылку на первичный источник, а не байку.',
                    person='operator',display_name='Тестовый собеседник',conversation='local',register='auto',surface='private',event_id='explicit-web')
            engine.step(max_calls=min(3,args.max_calls-backend.calls))
            for output in store.pending_output():store.acknowledge(output['id'])
            records.append(dict(store.db.execute('SELECT * FROM turns WHERE event_id=?',(event_id,)).fetchone()))
            print(json.dumps({'scene':'own-interest-web' if args.own_interest else 'explicit-web','calls':backend.calls,'decision':json.loads(records[-1]['decision']),
                'web_runs':[json.loads(row['receipt']) for row in store.db.execute('SELECT receipt FROM web_runs')]},ensure_ascii=False),flush=True)
            if args.wait_for_wake:
                # Wait through the real stored timer, without /wake or shortening its deadline.
                while time.monotonic()<deadline and backend.calls<args.max_calls:
                    event_id=engine.wake_if_due('auto')
                    if event_id:
                        engine.runtime_limits['autonomous_minutes_remaining']=max(0,int((deadline-time.monotonic())/60))
                        engine.step(max_calls=min(3,args.max_calls-backend.calls))
                        for output in store.pending_output():store.acknowledge(output['id'])
                        records.append(dict(store.db.execute('SELECT * FROM turns WHERE event_id=?',(event_id,)).fetchone()))
                        print(json.dumps({'scene':'scheduled-wake','calls':backend.calls,'decision':json.loads(records[-1]['decision'])},ensure_ascii=False),flush=True)
                        break
                    time.sleep(1)
    finally:
        result={'model':backend.backend.model,'model_calls':backend.calls,'turns':records,
                'sources':[dict(row) for row in store.db.execute('SELECT * FROM web_sources')],
                'status':store.status(),'own_interest_fixture':args.own_interest,
                'note':'Actual native web tools; synthetic audience/initial interest in an isolated database. Immediate fixture wake does not prove elapsed waiting; --wait-for-wake respects stored deadline.'}
        (directory/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        store.close()
    return 0


if __name__=='__main__':sys.exit(main())
