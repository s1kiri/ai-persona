"""Temporary macOS idle-sleep assertion scoped to one finite night plan."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .app_server import save_private


def night(directory):
    with sqlite3.connect((directory/'world.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM world_meta WHERE key='night'").fetchone()
        return json.loads(row[0]) if row else None


def run(directory):
    if sys.platform != 'darwin':
        raise RuntimeError('temporary awake guard is macOS-only')
    fd = os.open(directory/'awake.lock', os.O_RDWR | os.O_CREAT, 0o600)
    child = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = night(directory)
        server = json.loads((directory/'lab.json').read_text())
        if not plan or plan['status'] != 'running' or not time.time() < plan['deadline'] <= time.time()+12*3600:
            raise RuntimeError('no active finite night plan')
        os.kill(server['pid'], 0)
        child = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-t', str(math.ceil(plan['deadline']-time.time())),
                                  '-w', str(server['pid'])], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record = {'pid': os.getpid(), 'caffeinate_pid': child.pid, 'server_pid': server['pid'],
                  'night_id': plan['id'], 'deadline': plan['deadline'], 'active': True}
        save_private(directory/'awake.json', record)
        try:
            while time.time() < plan['deadline'] and child.poll() is None:
                current = night(directory)
                if not current or current['id'] != plan['id'] or current['status'] != 'running':
                    break
                if json.loads((directory/'lab.json').read_text())['pid'] != server['pid']:
                    break
                try:
                    os.kill(server['pid'], 0)
                except ProcessLookupError:
                    break
                time.sleep(5)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            save_private(directory/'awake.json', {**record, 'active': False, 'ended': time.time()})
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    if args.background:
        fd = os.open(directory/'awake.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'persona_runtime.world_awake', '--data-dir', str(directory)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        print(json.dumps({'awake_guard_pid': process.pid}), flush=True)
    else:
        run(directory)


if __name__ == '__main__':
    main()
