"""Make a PRIVATE profile from a stopped legacy world and its actual authored prompt."""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .character_profile import CharacterProfile
from .world import WorldConflict


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--world',type=Path,required=True)
    parser.add_argument('--persona',type=Path,required=True)
    parser.add_argument('--speech-contract',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model')
    parser.add_argument('--reasoning-effort',choices=['none','minimal','low','medium','high','xhigh','max'])
    parser.add_argument('--portrait',type=Path)
    args=parser.parse_args(argv)
    if bool(args.model)!=bool(args.reasoning_effort):parser.error('supply both model and reasoning effort, or neither')
    root=Path(__file__).resolve().parent.parent;output=args.output.resolve()
    if root in output.parents and root/'.local' not in output.parents:parser.error('private profiles belong under .local/')
    if output.exists():parser.error('output must be a new private directory')
    with ExitStack() as resources:
        lock=resources.enter_context((args.world.resolve()/'world.lock').open('a+'))
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise WorldConflict('stop the source owner before exporting its profile') from None
        db=sqlite3.connect((args.world.resolve()/'world.sqlite3').as_uri()+'?mode=ro',uri=True)
        resources.callback(db.close)
        actors=db.execute('SELECT id,name FROM world_actors').fetchall()
        if len(actors)!=1:raise WorldConflict('export requires one standalone actor')
        actor,name=actors[0]
        meta={r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM world_meta')}
        if not meta.get('paused'):raise WorldConflict('pause the source before shutdown and export')
        if (meta.get('character_packages') or {}).get(actor):
            raise WorldConflict('this world already has a profile; copy its EXACT existing package instead')
        canon=meta['basis'][actor]
        environment=[json.loads(r[0]) for r in db.execute('SELECT value FROM world_entities WHERE id!=?',(actor,))]
        has_memories=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_memories'").fetchone()
        memories=[json.loads(r[0]) for r in db.execute('SELECT value FROM world_memories WHERE actor_id=?',(actor,))] if has_memories else []
    output.mkdir(parents=True,mode=0o700)
    for source,target in [(args.persona,'persona.md'),(args.speech_contract,'speech-contract.md')]:
        shutil.copyfile(source,output/target);os.chmod(output/target,0o600)
    files={'canon.md':canon,'environment.json':environment,'memory.json':{'version':1,'records':memories,'concerns':[]}}
    for filename,value in files.items():
        (output/filename).write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        os.chmod(output/filename,0o600)
    manifest={'version':1,'id':actor,'name':name,'persona':'persona.md','speech_contract':'speech-contract.md',
              'canon':'canon.md','environment':'environment.json','memory':'memory.json'}
    if args.model:manifest['runtime']={'model':args.model,'reasoning_effort':args.reasoning_effort}
    if args.portrait:
        filename='portrait'+args.portrait.suffix.lower();shutil.copyfile(args.portrait,output/filename)
        (output/'references.json').write_text(json.dumps({actor:filename}));manifest['visual_references']='references.json'
    (output/'character.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');os.chmod(output/'character.json',0o600)
    profile=CharacterProfile.load(output/'character.json')
    print(json.dumps({'actor_id':actor,'private_profile':str(output/'character.json'),'sha256':profile.digest,
        'note':'Use --adopt-legacy for explicit import. Lived memory is copied from SQLite, never re-seeded.'},ensure_ascii=False))


if __name__=='__main__':main()
