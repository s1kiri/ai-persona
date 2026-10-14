#!/usr/bin/env python3
"""Verified local reset. No model, credentials, global threads or network."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from persona_runtime.daemon import control,private_json
from persona_runtime.store import Store,worker_lock
from persona_runtime.telemetry import Ledger


def counts(db):
    names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name:db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in names}


def reset(directory,backup_root):
    directory,backup_root=Path(directory).resolve(),Path(backup_root).resolve()
    if control(directory):raise RuntimeError('stop the responding Garik daemon before reset')
    if not (directory/'runtime.sqlite3').is_file():raise ValueError('existing Garik database required')
    if any(path.is_symlink() for path in directory.rglob('*')):
        raise ValueError('review runtime symlinks before reset; no external target copied')
    backup_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+os.urandom(4).hex()
    destination=backup_root/('garik-reset-'+stamp)
    destination.mkdir(mode=0o700)
    snapshot=destination/'snapshot';archive=destination/'original'
    staging=destination/'fresh'
    with worker_lock(directory):
        # Preserve every local file, including queued decisions, thread mapping,
        # exports and receipts. SQLite backup replaces the copied database.
        shutil.copytree(directory,snapshot)
        with sqlite3.connect('file:'+str(directory/'runtime.sqlite3')+'?mode=ro',uri=True) as source:
            character=source.execute("SELECT value FROM metadata WHERE key='character'").fetchone()
            if not character or character[0]!='garik':raise ValueError('database is not Garik')
            expected=counts(source)
            with sqlite3.connect(snapshot/'runtime.sqlite3') as copied:
                source.backup(copied)
                if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or counts(copied)!=expected:
                    raise RuntimeError('backup verification failed; active state untouched')
            last=source.execute("SELECT MAX(created) FROM events WHERE kind='message' AND person='operator' AND conversation='local'").fetchone()[0]
            heartbeat=source.execute("SELECT value FROM metadata WHERE key='runtime_heartbeat'").fetchone()
        hashes={str(path.relative_to(snapshot)):hashlib.sha256(path.read_bytes()).hexdigest() for path in snapshot.rglob('*') if path.is_file()}
        manifest={'version':1,'created':time.time(),'active':str(directory),'snapshot':str(snapshot),
            'original':str(archive),'table_counts':expected,'sha256':hashes,'verified':True,'applied':False,
            'preserved':'Entire original directory. Proxy/Keychain, persona, model configuration, other threads and repository files untouched.'}
        private_json(destination/'manifest.json',manifest)
        fresh=Store(staging)
        try:
            Ledger(fresh)
            # Usage is accounting, not character memory. Preserve budgets across
            # reset, without carrying context provenance or transcripts forward.
            with sqlite3.connect(snapshot/'runtime.sqlite3') as copied:
                if copied.execute("SELECT 1 FROM sqlite_master WHERE name='call_ledger'").fetchone():
                    for receipt in copied.execute('SELECT * FROM call_ledger'):
                        row=list(receipt);row[0]='archive:'+stamp+':'+row[0];row[1]=None;row[10]='{}'
                        if row[8]=='running':row[8]='interrupted'
                        fresh.db.execute('INSERT INTO call_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
                    fresh.db.commit()
            fresh.seed('garik',(ROOT/'agent/garik/memory/state.md').read_text(),(ROOT/'agent/garik/GOALS.md').read_text())
            with fresh.db:
                if last is not None:fresh.put('previous_user_message_at',last)
                if heartbeat:fresh.put('runtime_heartbeat',heartbeat[0])
                fresh.put('reset_manifest',str(destination/'manifest.json'))
            if fresh.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('fresh database invalid')
        finally:fresh.close()
        directory.rename(archive)
        try:staging.rename(directory)
        except Exception:
            archive.rename(directory)
            raise
        manifest['applied']=True
        private_json(destination/'manifest.json',manifest)
    return {'reset':True,'backup':str(destination),'verified':True,'model_called':False,
        'old_thread_mapping_preserved':(archive/'codex-session/sessions.json').exists(),
        'active_thread_mapping_exists':(directory/'codex-session/sessions.json').exists()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',required=True)
    args=parser.parse_args()
    print(json.dumps(reset(ROOT/'.runtime/garik',ROOT/'.runtime/backups'),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
