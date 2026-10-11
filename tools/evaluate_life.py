#!/usr/bin/env python3
"""Bounded live life/restart/cancellation check, with real stored deadlines.

Synthetic audience; no working conversation database, forced wake or shortened
timer. A character choosing a long wait is recorded as such, not as a success
of a scheduled continuation. Explicit --run-live and enabled proxy required.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
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
    parser.add_argument('--max-calls',type=int,default=16)
    parser.add_argument('--max-minutes',type=int,default=20)
    parser.add_argument('--pending-interest',action='store_true',help='explicit synthetic fixture: own unfinished mineral curiosity, first deadline 5 real minutes from boot')
    parser.add_argument('--interrupt-first-wake',action='store_true',help='cancel an actual in-flight first wake with audience input, then verify admitted continuation')
    args=parser.parse_args()
    if not args.run_live or not 1<=args.max_calls<=24 or not 1<=args.max_minutes<=30:
        parser.error('explicit --run-live and bounded limits required')
    directory=artifact_path(args.out)
    if directory.exists():parser.error('use a new evidence directory')
    if not proxy.status()['enabled']:parser.error('enabled user proxy required; no direct test')
    directory.mkdir(parents=True,mode=0o700)
    backend=Recorder(CodexBackend(timeout=90),directory,args.max_calls)
    started=time.monotonic();deadline=started+args.max_minutes*60
    records=[];store=None;session=None
    def record(kind,**details):
        row={'kind':kind,'elapsed_seconds':round(time.monotonic()-started,3),
             'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**details}
        records.append(row)
        with (directory/'timeline.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    def delivery():
        for item in store.pending_output('operator','local'):
            record('delivery',event_id=item['event_id'],text=item['text'],ordinal=item['ordinal'])
            store.acknowledge(item['id'])
    def open_session(calls=0,wakes=0):
        nonlocal store,session
        store=Store(directory/'db');store.memory.dense=False
        engine=Engine(store,backend,web_enabled=True,max_web_calls=2)
        session=Session(store,engine,output=lambda text:record('session',text=text),
                        max_calls=args.max_calls,max_wakes=4,max_minutes=args.max_minutes,deliver=delivery)
        session.deadline=deadline;session.calls=calls;session.wakes=wakes
        session.async_mode=True
        session.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='life-test')
    def pump_until_idle():
        while session.future and time.monotonic()<deadline:
            session.poll();delivery();time.sleep(.05)
        if session.future:raise RuntimeError('bounded live evaluation deadline reached')
    try:
        open_session()
        with worker_lock(store.directory):
            store.recover()
            if args.pending_interest:
                # A documented fixture, not a claim that the live character chose
                # this starting interest. Never shorten a model-selected timer.
                with store.db:
                    store.put('state','My quiet local life. No human task, purchases, participants or external actions. Curious about soft chalk and harder limestone.')
                    store.put('goals','Own unfinished curiosity: understand why chalk is soft while other limestones are harder, and whether porosity explains it. No assigned task. Decide what to do next.')
                    store.put('next_wake',time.time()+300)
                record('synthetic-interest-fixture',first_deadline=store.get('next_wake'))
            # First boot wake is immediate because no previous deadline exists.
            record('first-boot',woke=session.tick())
            if args.interrupt_first_wake:
                time.sleep(.6)
                session.line('Я пока занят. Не присылай мне ничего первым, продолжай свои дела без меня. Потом загляну.')
            pump_until_idle()
            record('before-restart',calls=session.calls,wakes=session.wakes,status=store.status())
            calls,wakes=session.calls,session.wakes
            session.close();store.close();store=None
            open_session(calls,wakes)
            store.recover()
            record('after-restart',status=store.status())
            if not args.interrupt_first_wake:
                session.line('Гарыч, сейчас зайду, секунду.')
                time.sleep(.6)
                session.line('Предыдущую реплику пока отмени. Я ещё занят. Не присылай мне ничего первым, занимайся своими делами. Потом загляну.')
                pump_until_idle()
            record('after-interruption',status=store.status(),calls=session.calls)
            wait_started=time.monotonic();last_turns=store.db.execute('SELECT count(*) FROM turns').fetchone()[0]
            while time.monotonic()<deadline and not session.stopped and session.wakes<session.max_wakes:
                session.poll();delivery()
                session.tick()
                count=store.db.execute('SELECT count(*) FROM turns').fetchone()[0]
                if count!=last_turns:
                    record('scheduled-completion',waited_seconds=round(time.monotonic()-wait_started,3),
                           calls=session.calls,wakes=session.wakes,status=store.status())
                    last_turns=count
                    # Two naturally scheduled continuations are enough; bounded by the deadline.
                    if session.wakes>=3:break
                time.sleep(.1)
            pump_until_idle()
            record('finished',calls=session.calls,wakes=session.wakes,status=store.status())
    finally:
        if session:session.close()
        if store:
            result={'model':backend.backend.model,'model_calls':backend.calls,'timeline':records,
                    'turns':[dict(row) for row in store.db.execute('SELECT * FROM turns ORDER BY completed')],
                    'events':[dict(row) for row in store.db.execute('SELECT * FROM events ORDER BY rowid')],
                    'attempts':[dict(row) for row in store.db.execute('SELECT * FROM model_attempts ORDER BY id')],
                    'sources':[dict(row) for row in store.db.execute('SELECT * FROM web_sources')],
                    'activity_versions':[dict(row) for row in store.db.execute('SELECT * FROM activity_versions')],
                    'status':store.status(),'actual_elapsed_seconds':round(time.monotonic()-started,3),
                    'note':'Actual Codex, actual clock and stored timers; no /wake or shortened deadline. Synthetic audience, isolated database.'}
            (directory/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            store.close()
    return 0


if __name__=='__main__':sys.exit(main())
