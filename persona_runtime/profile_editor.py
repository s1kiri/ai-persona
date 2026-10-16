"""Local author revision: private package, checked backups, audited state, no inference."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from .character_profile import CharacterProfile
from .profile_update import revise_profile
from .world import WorldConflict


def editor_state(profile, *, paused, busy=False):
    return None if profile is None else {'persona': profile.persona, 'digest': profile.digest,
        'model': profile.runtime.get('model'), 'reasoning_effort': profile.runtime.get('reasoning_effort'),
        'can_save': paused and not busy,
        'hint': 'Сначала пауза и завершение текущего вызова. В общем мире — пауза обоих персонажей. '
                'Изменения сохраняются локально с резервной копией; жизнь остаётся на паузе.'}


def private_package(directory, previous, persona, model, effort):
    if (not isinstance(persona,str) or not persona.strip() or len(persona.encode())>100_000 or
            not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}',model) or
            effort not in ('none','minimal','low','medium','high','xhigh','max')):
        raise ValueError('invalid persona, model or reasoning effort')
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest={'version':1,'id':previous.actor_id,'name':previous.name,'persona':'persona.md',
        'speech_contract':'speech-contract.md','canon':'canon.md','environment':'environment.json',
        'memory':'memory.json','runtime':{'model':model,'reasoning_effort':effort}}
    files={'persona.md':persona,'speech-contract.md':previous.speech_contract,'canon.md':previous.canon,
        'environment.json':json.dumps(previous.environment,ensure_ascii=False),
        'memory.json':json.dumps({'version':1,'records':previous.memories,'concerns':previous.concerns},ensure_ascii=False)}
    if previous.references:
        refs={}
        for key,source in previous.references.items():
            name=key+Path(source).suffix;shutil.copyfile(source,directory/name);os.chmod(directory/name,0o600);refs[key]=name
        manifest['visual_references']='references.json';files['references.json']=json.dumps(refs)
    files['character.json']=json.dumps(manifest,ensure_ascii=False,indent=2)
    for name,text in files.items():
        target=directory/name;target.write_text(text);os.chmod(target,0o600)
    return CharacterProfile.load(directory/'character.json')


def edit_profile(store, previous, value, connections, *, lives=(), scheduler=None):
    if not isinstance(value,dict) or set(value)!={'digest','persona','model','reasoning_effort'}:
        raise ValueError('exact author fields required')
    if value['digest']!=previous.digest:
        raise WorldConflict('Авторская версия изменилась в другой вкладке. Сначала обновите редактор.')
    connections,lives=tuple(connections),tuple(lives)
    lock=scheduler.lock if scheduler else lives[0].active_lock if lives else None
    @contextmanager
    def guard():
        if lock and not lock.acquire(blocking=False):
            raise WorldConflict('Идёт текущий вызов. Дождитесь окончания этапа.')
        try:yield
        finally:
            if lock:lock.release()
    with guard(), store.lock:
        if not store.get('paused'):
            raise WorldConflict('Сначала поставьте жизнь на паузу.')
        if any(life.lab.busy or life.get('life_status')['status']=='running' for life in lives):
            raise WorldConflict('Дождитесь окончания текущего вызова и поставьте всех на паузу.')
        if store.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone():
            raise WorldConflict('Модель ещё отвечает. Дождитесь окончания текущего вызова.')
        for connection in connections:
            if connection.db.execute("SELECT 1 FROM outbox WHERE status IN ('sending','unknown')").fetchone():
                # A reviewed unknown is retained in transport history. Only the
                # live host job decides whether that old receipt is unresolved.
                if store.db.execute("SELECT 1 FROM channel_jobs WHERE status IN ('sending','unknown')").fetchone():
                    raise WorldConflict('Сначала разберите неизвестную доставку.')
        folder=store.directory/'profiles'/previous.actor_id/uuid.uuid4().hex
        updated=private_package(folder,previous,value['persona'],value['model'],value['reasoning_effort'])
        backup=folder/'backup';backup.mkdir(mode=0o700)
        for name,source in [('world',store), *((f'telegram-{i}',c) for i,c in enumerate(connections))]:
            with source.lock, sqlite3.connect(backup/(name+'.sqlite3')) as target:
                source.db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise WorldConflict('Проверка резервной копии не пройдена.')
            os.chmod(backup/(name+'.sqlite3'),0o600)
        # Profile binding and private startup pointer commit in the same transaction.
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            revise_profile(store,previous,updated,'Explicit owner edit in local settings panel',shared_owner=bool(store.get('shared_host')))
            overrides=store.get('profile_overrides') or {};old=overrides.get(previous.actor_id,{})
            store.put('profile_overrides',{**overrides,previous.actor_id:{
                'base_digest':old.get('base_digest',previous.digest),'digest':updated.digest,
                'manifest':str((folder/'character.json').relative_to(store.directory))}})
        for life in lives:
            if life.actor_id!=updated.actor_id:continue
            session=life.lab.session
            for backend in {id(b):b for b in (session.backend,session.research_backend,life.renderer) if b is not None}.values():
                backend.close();backend.model=value['model'];backend.reasoning_effort=value['reasoning_effort']
            session.actor_prompts[updated.actor_id]=updated.persona
            life.persona=updated.persona;life.references=updated.references;life.lab.profile=updated
        return updated
