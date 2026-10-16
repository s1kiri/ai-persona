"""Review and copy two stopped standalone lives into a NEW shared directory. No model/network."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from .shared_host import initialize_world, load_config
from .shared_world import SharedWorldStore
from .world import WorldConflict

GLOBAL_META={'version','revision','minute','settings','settings_revision','paused','basis',
             'host_profile','character_packages','night','review_notice','profile_revisions','profile_overrides','awake_status'}
SOURCE_TABLES={'world_cycles':'id','world_events':'id','world_calls':'id','world_entities':'id',
               'world_threads':'id','world_hooks':'id','world_notes':'id','world_memories':'id',
               'channel_reaction_jobs':'id','channel_jobs':'post_id','channel_read_batches':'id'}
ID_FIELDS={'id','parent','child','causes','sources','known_to','visible_to','focus_ids','read_ids','defer_ids',
           'prior_cycle','next_cycle','last_cycle','last_post','fulfilled_by'}


def inspect_source(world, telegram, actor, profile, stack, *, allow_legacy=False):
    world,telegram=Path(world).resolve(),Path(telegram).resolve()
    for folder,filename in [(world,'world.lock'),(telegram,'telegram.lock')]:
        if not folder.is_dir():raise ValueError('source directory missing: '+str(folder))
        lock=stack.enter_context((folder/filename).open('a+'))
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise WorldConflict('stop the source owner gracefully first: '+str(folder)) from None
    db=sqlite3.connect((world/'world.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    stack.callback(db.close)
    tg=sqlite3.connect((telegram/'telegram.sqlite3').as_uri()+'?mode=ro',uri=True);tg.row_factory=sqlite3.Row
    stack.callback(tg.close)
    meta={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM world_meta')}
    if not meta.get('paused') or [r[0] for r in db.execute('SELECT id FROM world_actors')]!=[actor]:
        raise WorldConflict('source must be a paused standalone life for '+actor)
    pending=list(db.execute("SELECT id,phase,status FROM world_cycles WHERE status IN ('ready','running','blocked','waiting','held')"))
    if pending:raise WorldConflict('finish or explicitly review source stages before migration: '+json.dumps([dict(r) for r in pending]))
    if db.execute("SELECT 1 FROM world_calls x JOIN world_cycles c ON c.id=x.cycle_id WHERE x.status='started' OR (x.status='interrupted' AND c.status NOT IN ('abandoned','complete'))").fetchone():
        raise WorldConflict('source contains an unresolved model call')
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'channel_jobs' in tables and db.execute("SELECT 1 FROM channel_jobs WHERE status IN ('sending','unknown')").fetchone():
        raise WorldConflict('review uncertain source channel deliveries before migration')
    if 'channel_reaction_jobs' in tables and db.execute("SELECT 1 FROM channel_reaction_jobs WHERE status IN ('sending','unknown')").fetchone():
        raise WorldConflict('review uncertain source reactions before migration')
    if tg.execute("SELECT 1 FROM outbox WHERE status IN ('sending','unknown')").fetchone():
        # Old explicitly reviewed attempts remain as unknown in the transport ledger.
        repairs=meta.get('delivery_repairs' if actor=='garik' else 'actor:'+actor+':delivery_repairs') or []
        if not repairs:raise WorldConflict('source Telegram has an uncertain delivery without a repair journal')
        for row in tg.execute("SELECT key FROM outbox WHERE status IN ('sending','unknown')"):
            if not any(r['post_id'] in row['key'] for r in repairs):
                raise WorldConflict('unreviewed source transport attempt')
    installed=(meta.get('character_packages') or {}).get(actor)
    if installed is None:
        if not allow_legacy or meta.get('basis',{}).get(actor)!=profile.canon:
            raise WorldConflict('legacy life needs its exact private profile and explicit --adopt-legacy '+actor)
    elif installed!=profile.digest:
        raise WorldConflict('source profile fingerprint differs from the configured profile')
    tgmeta={r['key']:json.loads(r['value']) for r in tg.execute('SELECT * FROM meta')}
    host=tgmeta.get('host_profile')
    if host and host.get('actor_id')!=actor:raise WorldConflict('source bot belongs to another actor')
    if not tgmeta.get('binding'):raise WorldConflict('source Telegram must have a verified channel binding')
    return {'actor':actor,'world':world,'telegram':telegram,'db':db,'tg':tg,'meta':meta,'tgmeta':tgmeta,
            'tables':tables,'legacy':installed is None}


def backup(db,path):
    with sqlite3.connect(path) as target:
        db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise WorldConflict('backup integrity check failed')
    os.chmod(path,0o600)


VIRTUAL_CLOCK_FIELDS={'minute','due_minute','opens_minute','closes_minute','exposed_minute','started_minute','service_minute','at_minute'}

def shift_clock(value, offset, field=None):
    if isinstance(value,dict):return {k:shift_clock(v,offset,k) for k,v in value.items()}
    if isinstance(value,list):return [shift_clock(v,offset,field) for v in value]
    if field in VIRTUAL_CLOCK_FIELDS and type(value) is int:return value+offset
    return value

def transform(value, aliases, prefixes=(), media=None, field=None):
    if isinstance(value,dict):return {k:transform(v,aliases,prefixes,media,k) for k,v in value.items()}
    if isinstance(value,list):return [transform(v,aliases,prefixes,media,field) for v in value]
    if isinstance(value,str):
        if field in ID_FIELDS or field and (field.endswith('_id') or field.endswith('_ids')):
            return aliases.get(value,value)
        if media and field in ('image_path','path','output_image'):
            for prefix in prefixes:
                try:return str(media/Path(value).relative_to(prefix))
                except ValueError:pass
    return value


def copy_media(source,destination,aliases,original_prefixes):
    for name in ('photos','visual-memory'):
        origin=source/name
        if origin.exists():
            if any(p.is_symlink() for p in origin.rglob('*')):raise WorldConflict('resolve media symlinks before migration')
            shutil.copytree(origin,destination/name)
    library=destination/'visual-memory/references.sqlite3'
    if library.exists():
        with sqlite3.connect(library) as db:
            for row in db.execute('SELECT rowid,image_path,subject_ids FROM references_by_entity').fetchall():
                image=transform(row[1],aliases,original_prefixes,destination,'image_path')
                subjects=transform(json.loads(row[2]),aliases,field='subject_ids')
                db.execute('UPDATE references_by_entity SET image_path=?,subject_ids=? WHERE rowid=?',(image,json.dumps(subjects),row[0]))


def merge_rows(target,source,offset,aliases,prefixes,media):
    skipped={'world_meta','world_actors','world_snapshots','sqlite_sequence'}
    for table in sorted(source['tables']-skipped):
        if not table.replace('_','').isalnum():raise ValueError('unexpected source table')
        definition=source['db'].execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        if not target.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            target.db.execute(definition)
        columns=[r[1] for r in source['db'].execute('PRAGMA table_info("'+table+'")')]
        for record in source['db'].execute('SELECT * FROM "'+table+'"'):
            data={}
            for key,value in dict(record).items():
                if table=='reader_memory_changes' and key=='id' and isinstance(value,int):continue
                if key in ('value','sources','usage','receipt','receipts','photo') and isinstance(value,str):
                    try:value=json.dumps(shift_clock(transform(json.loads(value),aliases,prefixes,media,'sources' if key=='sources' else None),offset),ensure_ascii=False)
                    except json.JSONDecodeError:pass
                elif isinstance(value,str) and (key in ID_FIELDS or key.endswith('_id') or key.endswith('_cycle')):
                    value=aliases.get(value,value)
                if key=='minute' and type(value) is int:value+=offset
                data[key]=value
            names=list(data)
            target.db.execute('INSERT INTO "'+table+'" ('+','.join('"'+n+'"' for n in names)+') VALUES('+','.join('?' for _ in names)+')',tuple(data[n] for n in names))


def migrate(config, source_worlds, source_telegrams, *, apply=False, adopt_legacy=()):
    config={**config,'data_dir':config['data_dir'].resolve(),
            'actors':[{**a,'telegram_dir':a['telegram_dir'].resolve()} for a in config['actors']]}
    expected={a['profile'].actor_id for a in config['actors']}
    if set(source_worlds)!=expected or set(source_telegrams)!=expected:
        raise ValueError('provide one source world and Telegram directory for EACH configured actor')
    destination=config['data_dir']
    targets=[destination,*(a['telegram_dir'] for a in config['actors'])]
    if any(p.exists() for p in targets):raise WorldConflict('all destination directories must be new; originals are never overwritten')
    with ExitStack() as stack:
        sources=[inspect_source(source_worlds[a['profile'].actor_id],source_telegrams[a['profile'].actor_id],
                    a['profile'].actor_id,a['profile'],stack,allow_legacy=a['profile'].actor_id in adopt_legacy) for a in config['actors']]
        if len({s['tgmeta'].get('bot',{}).get('id') for s in sources})!=2:
            raise WorldConflict('source connections must use distinct bots')
        minute=max(s['meta']['minute'] for s in sources)
        report={'ready':True,'applied':False,'destination':str(destination),'clock_minute':minute,
            'actors':[{'id':s['actor'],'cycles':s['db'].execute('SELECT COUNT(*) FROM world_cycles').fetchone()[0],
                       'events':s['db'].execute('SELECT COUNT(*) FROM world_events').fetchone()[0],
                       'clock_offset':minute-s['meta']['minute'],'adopt_legacy':s['legacy']} for s in sources]}
        if not apply:return report
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        stage=Path(tempfile.mkdtemp(prefix='shared-import-',dir=destination.parent));os.chmod(stage,0o700)
        stack.callback(lambda:shutil.rmtree(stage,ignore_errors=True))
        target=SharedWorldStore(stage/'world');target_open=[True]
        def close_target():
            if target_open[0]:target.close();target_open[0]=False
        stack.callback(close_target)
        common_time=time.time();imports=[]
        with target.lock,target.db:
            target.db.execute('BEGIN IMMEDIATE')
            target.db.execute('PRAGMA defer_foreign_keys=ON')
            for item,source in zip(config['actors'],sources):
                actor=source['actor'];archive=stage/'world'/'imports'/actor;archive.mkdir(parents=True,mode=0o700)
                backup(source['db'],archive/'original-world.sqlite3');backup(source['tg'],archive/'original-telegram.sqlite3')
                occupied={r[0] for t,col in SOURCE_TABLES.items() if target.db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(t,)).fetchone()
                          for r in target.db.execute('SELECT "'+col+'" FROM "'+t+'"')}
                aliases={}
                for table,column in SOURCE_TABLES.items():
                    if table not in source['tables']:continue
                    for row in source['db'].execute('SELECT "'+column+'" FROM "'+table+'"'):
                        eid=row[0]
                        if eid in occupied:
                            if eid in expected:raise WorldConflict('an old NPC collides with a managed actor; reconcile explicitly before migration')
                            aliases[eid]='import-'+actor+'-'+hashlib.sha256(str(eid).encode()).hexdigest()[:24]
                target.register_actor(actor,item['profile'].name)
                # Register creates an actor entity; the source's authoritative actor row replaces just that seed.
                target.db.execute('DELETE FROM world_entities WHERE id=?',(actor,))
                original=(source['tgmeta'].get('host_profile') or {}).get('world')
                prefixes=[source['world'],*( [Path(original)] if original else [])]
                final_media=destination/'media'/actor
                merge_rows(target,source,minute-source['meta']['minute'],aliases,prefixes,final_media)
                target.put('basis',{**target.get('basis'),actor:item['profile'].canon})
                target.put('character_packages',{**(target.get('character_packages') or {}),actor:item['profile'].digest})
                for key,value in source['meta'].items():
                    if key in GLOBAL_META or key.startswith('shared_'):continue
                    if key.startswith('actor:') and not key.startswith('actor:'+actor+':'):continue
                    target.put(key,shift_clock(transform(value,aliases,prefixes,final_media),minute-source['meta']['minute']))
                target.actor_put(actor,'life_status',{**(target.actor_get(actor,'life_status') or {}),'status':'paused'})
                target.actor_put(actor,'channel_epoch',{'wall':common_time,'minute':minute})
                if source==sources[0]:
                    target.put('settings',source['meta']['settings']);target.put('settings_revision',source['meta']['settings_revision'])
                override=(source['meta'].get('profile_overrides') or {}).get(actor)
                if override:
                    from .character_profile import CharacterProfile
                    manifest=(source['world']/override['manifest']).resolve()
                    if (not manifest.is_relative_to(source['world']/'profiles'/actor) or
                            override['digest']!=item['profile'].digest):
                        raise WorldConflict('private profile override does not match imported actor')
                    origin=manifest.parent
                    if any(p.is_symlink() for p in origin.rglob('*')):
                        raise WorldConflict('resolve profile symlinks before migration')
                    folder=stage/'world'/Path(override['manifest']).parent
                    shutil.copytree(origin,folder,ignore=shutil.ignore_patterns('backup'))
                    if CharacterProfile.load(folder/manifest.name).digest!=override['digest']:
                        raise WorldConflict('copied private profile digest differs')
                    for path in [folder,*folder.rglob('*')]:os.chmod(path,0o700 if path.is_dir() else 0o600)
                    target.put('profile_overrides',{**(target.get('profile_overrides') or {}),actor:override})
                revisions=source['meta'].get('profile_revisions') or []
                target.put('profile_revisions',[*(target.get('profile_revisions') or []),
                    *shift_clock(transform(revisions,aliases,prefixes,final_media),minute-source['meta']['minute'])])
                copy_media(source['world'],stage/'world'/'media'/actor,aliases,prefixes)
                library=stage/'world'/'media'/actor/'visual-memory/references.sqlite3'
                if library.exists():
                    with sqlite3.connect(library) as db:
                        for r in db.execute('SELECT rowid,image_path FROM references_by_entity').fetchall():
                            staged=str(stage/'world'/'media'/actor)
                            db.execute('UPDATE references_by_entity SET image_path=? WHERE rowid=?',
                                (r[1].replace(staged,str(final_media),1),r[0]))
                tgdir=stage/('telegram-'+actor);tgdir.mkdir(mode=0o700);backup(source['tg'],tgdir/'telegram.sqlite3')
                token=source['telegram']/'bot-token.json'
                if token.exists():
                    if token.is_symlink():raise WorldConflict('resolve token symlink locally before migration')
                    shutil.copyfile(token,tgdir/'bot-token.json');os.chmod(tgdir/'bot-token.json',0o600)
                with sqlite3.connect(tgdir/'telegram.sqlite3') as db:
                    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('host_profile',json.dumps({'actor_id':actor,'world':str(destination)})))
                    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('setup_actor',json.dumps(actor)))
                imports.append({'actor_id':actor,'clock_offset':minute-source['meta']['minute'],'aliases':aliases,
                    'original_world':str(source['world']),'original_telegram':str(source['telegram']),
                    'legacy_profile_adopted':source['legacy'],'original_meta':source['meta']})
            target.put('minute',minute);target.put('shared_imports',imports);target.put('paused',True)
        initialize_world(target,config,now=common_time,imported=True)
        if target.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or target.db.execute('PRAGMA foreign_key_check').fetchone():
            raise WorldConflict('merged database integrity failed')
        close_target()
        for item in config['actors']:item['telegram_dir'].parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Each rename is reversible; originals remain locked, unchanged and available for rollback.
        published=[]
        try:
            os.rename(stage/'world',destination);published.append((destination,stage/'world'))
            for item in config['actors']:
                source=stage/('telegram-'+item['profile'].actor_id);os.rename(source,item['telegram_dir']);published.append((item['telegram_dir'],source))
        except Exception:
            for final,original in reversed(published):os.rename(final,original)
            raise
        return {**report,'applied':True,'archives':str(destination/'imports')}


def parse_sources(values):
    result={}
    for value in values:
        actor,separator,path=value.partition('=')
        if not separator or not actor or not path or actor in result:raise ValueError('source syntax: actor_id=/absolute/path')
        result[actor]=Path(path).expanduser().resolve()
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--source-world',action='append',default=[])
    parser.add_argument('--source-telegram',action='append',default=[])
    parser.add_argument('--adopt-legacy',action='append',default=[])
    parser.add_argument('--apply',action='store_true',help='apply reviewed plan to NEW directories, preserving original archives')
    args=parser.parse_args(argv)
    report=migrate(load_config(args.config),parse_sources(args.source_world),parse_sources(args.source_telegram),apply=args.apply,adopt_legacy=args.adopt_legacy)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
