#!/usr/bin/env python3
"""Audit tracked source and optional outgoing history; never print matched secrets."""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT=Path(__file__).resolve().parent.parent
BINARY={'.png','.jpg','.jpeg','.webp','.gif','.pdf','.woff','.woff2'}
SECRET_PATTERNS=(re.compile(rb'\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b'),
                 re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b'),
                 re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'))


def forbidden(path):
    p=PurePosixPath(path)
    if any(part in {'.local','.runtime','.venv','secrets','tickets','__pycache__'} for part in p.parts):return True
    if p.name in {'.DS_Store','.env','auth.json','bot-token.json'} or p.name.startswith('.env.') and p.name!='.env.example':return True
    if p.suffix in {'.sqlite','.sqlite3','.db','.log','.pem','.p12','.pyc'}:return True
    if p.suffix=='.jsonl' and path not in {'agent/vesper/inbox.jsonl','agent/vesper/outbox.jsonl'} and not path.startswith('tests/'):return True
    return False


def secret_like(data):
    return any(pattern.search(data) for pattern in SECRET_PATTERNS)


def git(*args,input=None):
    return subprocess.run(['git',*args],cwd=ROOT,input=input,check=True,capture_output=True).stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since',help='also inspect new objects in this ref..HEAD; run after committing')
    args=parser.parse_args();failures=set();checked=0
    for raw in git('ls-files','-z').split(b'\0'):
        if not raw:continue
        name=raw.decode();path=ROOT/name
        if forbidden(name):failures.add(name+' — local/runtime file')
        if path.is_file() and path.suffix.lower() not in BINARY:
            data=path.read_bytes();checked+=1
            if secret_like(data):failures.add(name+' — possible credential')
    objects={}
    if args.since:
        for line in git('rev-list','--objects',args.since+'..HEAD').decode().splitlines():
            oid,_,name=line.partition(' ')
            if name:objects[oid]=name
        listing=git('cat-file','--batch-check',input=('\n'.join(objects)+'\n').encode()).decode()
        for line in listing.splitlines():
            oid,kind,size=line.split();name=objects[oid]
            if forbidden(name):failures.add(name+' — local/runtime file in outgoing history')
            if kind!='blob' or PurePosixPath(name).suffix.lower() in BINARY:continue
            data=git('cat-file','blob',oid);checked+=1
            if secret_like(data):failures.add(name+' — possible credential in outgoing history')
    for message in sorted(failures):print(message)
    print(f'Publication audit: {checked} text files/versions checked; {len(failures)} findings. No matched values displayed.')
    return bool(failures)


if __name__=='__main__':raise SystemExit(main())
