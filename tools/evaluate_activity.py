#!/usr/bin/env python3
"""One explicit live continuation of completed work on a copy of test data."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

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
    parser.add_argument('--source-db',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    source=args.source_db.resolve();directory=artifact_path(args.out)
    if not args.run_live or not (ROOT/'.local/validation').resolve() in source.parents:
        parser.error('explicit --run-live and isolated validation source required')
    if directory.exists():parser.error('use a fresh evidence directory')
    if not proxy.status()['enabled']:parser.error('enabled proxy required; no direct test')
    directory.mkdir(parents=True,mode=0o700);data=directory/'db';data.mkdir(mode=0o700)
    with sqlite3.connect('file:'+str(source/'runtime.sqlite3')+'?mode=ro',uri=True) as old:
        with sqlite3.connect(data/'runtime.sqlite3') as new:old.backup(new)
    os.chmod(data/'runtime.sqlite3',0o600)
    store=Store(data);store.memory.dense=False
    backend=Recorder(CodexBackend(timeout=90),directory,1)
    engine=Engine(store,backend,web_enabled=False)
    before=[dict(row) for row in store.db.execute('SELECT * FROM activities ORDER BY updated DESC')]
    try:
        with worker_lock(store.directory):
            with store.db:
                # Explicit fixture, not a claim of a spontaneously chosen next wish.
                store.put('goals','Own test starting intent: add a short private reflection to the same completed pumice investigation, using the saved source and admitting its access limits. This continues existing work; no new search or external action.')
            event=store.enqueue('Immediate test invitation for your own check-in. No elapsed wait is asserted and no audience participant is added.',person='self',display_name='Гарик',conversation='local',register='auto',kind='wake',surface='private',event_id='continuation')
            engine.step(max_calls=1)
            result=json.loads(store.db.execute('SELECT decision FROM turns WHERE event_id=?',(event,)).fetchone()['decision'])
            print(json.dumps({'decision':result,'previous_keys':[row['key'] for row in before]},ensure_ascii=False),flush=True)
            (directory/'results.json').write_text(json.dumps({'model':backend.backend.model,'model_calls':backend.calls,
                'before':before,'decision':result,'after':[dict(row) for row in store.db.execute('SELECT * FROM activities')],
                'status':store.status(),'note':'Actual model continuation on copied test records. Starting intent and immediate invitation are explicit fixtures; not proof of elapsed autonomous waiting.'},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:store.close()
    return 0


if __name__=='__main__':sys.exit(main())
