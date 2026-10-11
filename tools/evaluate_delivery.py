#!/usr/bin/env python3
"""Explicit real schema/delivery check on an isolated synthetic conversation."""
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
from persona_runtime.session import Session
from persona_runtime.store import Store,worker_lock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if not args.run_live:parser.error('explicit --run-live required')
    directory=artifact_path(args.out)
    if directory.exists():parser.error('use a new evidence directory')
    if not proxy.status()['enabled']:parser.error('enabled proxy required; no direct test')
    directory.mkdir(parents=True,mode=0o700)
    backend=Recorder(CodexBackend(timeout=90),directory,3)
    store=Store(directory/'db');store.memory.dense=False
    engine=Engine(store,backend,web_enabled=False)
    deliveries=[];status={}
    def deliver(item):
        row={'ordinal':item['ordinal'],'text':item['text'],'at':time.time()}
        deliveries.append(row);print(json.dumps({'delivery':row},ensure_ascii=False),flush=True)
        store.acknowledge(item['id'])
    try:
        with worker_lock(store.directory):
            event_id=store.enqueue('Пришли три отдельных коротких сообщения: сначала привет, через две секунды пожелай спокойной ночи, а через десять секунд добавь что-нибудь от себя. Это только проверка доставки в местном тесте.',register='auto',surface='private',event_id='multipart')
            engine.step(max_calls=1)
            value=json.loads(store.db.execute('SELECT decision FROM turns WHERE event_id=?',(event_id,)).fetchone()['decision'])
            if len(value.get('messages',[]))!=3:
                raise RuntimeError('real model did not produce three message parts; not claimed as a delivery pass')
            deliver(store.pending_output()[0])
            store.close();store=Store(directory/'db');store.memory.dense=False
            engine=Engine(store,backend,web_enabled=False)
            status['queued_after_restart']=store.status()['queued_replies']
            deadline=time.monotonic()+35
            while not store.pending_output() and time.monotonic()<deadline:time.sleep(.02)
            if not store.pending_output():raise RuntimeError('second part did not become due')
            deliver(store.pending_output()[0])
            session=Session(store,engine,autonomy=False,max_calls=1,output=lambda _:None)
            session.line('/cancel');session.close()
            status['queued_after_cancel']=store.status()['queued_replies']
            status['first_decision']=value
            # The next real message tests silence, without scheduling any wake.
            quiet=store.enqueue('Теперь давай молча. Вообще ничего мне не отвечай, даже «ладно».',register='auto',surface='private',event_id='silence')
            engine.step(max_calls=1)
            status['silence_decision']=json.loads(store.db.execute('SELECT decision FROM turns WHERE event_id=?',(quiet,)).fetchone()['decision'])
            status['outbox']=[dict(row) for row in store.db.execute('SELECT ordinal,status,text FROM outbox ORDER BY created,ordinal')]
            status['final']=store.status()
    finally:
        (directory/'results.json').write_text(json.dumps({'model':backend.backend.model,'model_calls':backend.calls,
            'deliveries':deliveries,'status':status,'note':'Actual Codex decisions and actual relative delay across restart; synthetic audience and isolated database.'},ensure_ascii=False,indent=2),encoding='utf-8')
        store.close()
    return 0


if __name__=='__main__':sys.exit(main())
