#!/usr/bin/env python3
"""Real process restart plus actual future timer, on isolated live test data."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.evaluate_character import artifact_path
from persona_runtime import proxy
from persona_runtime.store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--source-db',type=Path,required=True,help='completed isolated own-interest test database')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    source=args.source_db.resolve()
    if not args.run_live or not (ROOT/'.local/validation').resolve() in source.parents:
        parser.error('explicit live check and an isolated validation source required')
    directory=artifact_path(args.out)
    if directory.exists():parser.error('use a fresh evidence directory')
    if not proxy.status()['enabled']:parser.error('enabled proxy required; no direct test')
    directory.mkdir(parents=True,mode=0o700)
    resumed=directory/'resumed';resumed.mkdir(mode=0o700)
    with sqlite3.connect('file:'+str(source/'runtime.sqlite3')+'?mode=ro',uri=True) as old:
        with sqlite3.connect(resumed/'runtime.sqlite3') as new:old.backup(new)
    os.chmod(resumed/'runtime.sqlite3',0o600)
    started=time.monotonic();deadline=started+8*60;stages=[]
    def run(label,data,arguments):
        command=[sys.executable,'-m','persona_runtime','--data-dir',str(data),'--timeout','90',*arguments]
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        pid=process.pid
        try:out,err=process.communicate(timeout=max(1,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            # Ctrl+C runs the adapter's finally block and stops its Codex group.
            process.send_signal(signal.SIGINT)
            try:out,err=process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();out,err=process.communicate()
            raise RuntimeError('bounded restart check stopped at its deadline')
        (directory/(label+'.txt')).write_text(out+'\n'+err,encoding='utf-8')
        record={'stage':label,'pid':pid,'returncode':process.returncode,
                'elapsed_seconds':round(time.monotonic()-started,3)}
        stages.append(record);print(json.dumps(record),flush=True)
        if process.returncode:raise RuntimeError('restart check stage failed; no successful continuation claimed')
    results={}
    try:
        run('enqueue-memory-check',resumed,['send','Ты чем своим занимался, пока я был занят? Если что-то проверял, напомни вывод и источник.','--register','auto','--surface','private','--event-id','after-process-restart'])
        # Entirely new CLI/Engine/backend process, one bounded model call.
        run('new-process-memory-answer',resumed,['step','--count','1'])
        with sqlite3.connect(resumed/'runtime.sqlite3') as db:
            db.row_factory=sqlite3.Row
            results['resumed_turn']=dict(db.execute("SELECT * FROM turns WHERE event_id='after-process-restart'").fetchone())
            results['source_count_after_restart']=db.execute('SELECT count(*) FROM web_sources').fetchone()[0]
        timer=Store(directory/'timer')
        try:
            # Brand-new synthetic fixture: no previously chosen model timer is changed.
            with timer.db:
                timer.put('character','garik')
                timer.put('state','Own pending curiosity about pumice. No source accessed yet, no human question or participant. Want to investigate the primary explanation when the next check-in arrives.')
                timer.put('goals','Personal intent: verify gas trapping in pumice through a primary research source, privately.')
                timer.put('next_wake',time.time()+300)
                timer.db.execute('INSERT INTO audience_preferences VALUES (?,?,?,?)',(timer.scope({'person':'operator','conversation':'local'}),'contextual',0,time.time()))
            results['initial_future_timer']=timer.get('next_wake')
        finally:timer.close()
        run('timer-first-process',directory/'timer',['watch','--register','auto','--max-turns','1','--max-minutes','1'])
        with sqlite3.connect(directory/'timer/runtime.sqlite3') as db:
            results['turns_before_process_restart']=db.execute('SELECT count(*) FROM turns').fetchone()[0]
            results['timer_after_first_process']=db.execute("SELECT value FROM metadata WHERE key='next_wake'").fetchone()[0]
        # First application has fully exited; next process respects the SAME future timestamp.
        run('timer-second-process',directory/'timer',['watch','--register','auto','--max-turns','3','--max-minutes','6'])
        with sqlite3.connect(directory/'timer/runtime.sqlite3') as db:
            db.row_factory=sqlite3.Row
            results['scheduled_turns']=[dict(row) for row in db.execute('SELECT * FROM turns ORDER BY completed')]
            results['scheduled_events']=[dict(row) for row in db.execute('SELECT * FROM events ORDER BY created')]
            results['scheduled_sources']=[dict(row) for row in db.execute('SELECT * FROM web_sources')]
    finally:
        results.update(stages=stages,actual_elapsed_seconds=round(time.monotonic()-started,3),
            note='Distinct CLI processes fully exit between phases. First future deadline and starting curiosity are synthetic fixtures; never shorten a model-selected timer. At most 4 model calls, total 8-minute watchdog.')
        (directory/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':sys.exit(main())
