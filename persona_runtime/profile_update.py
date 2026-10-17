"""Explicit author revision of an installed standalone profile; never reset lived state."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import hashlib
from contextlib import nullcontext

from .character_profile import CharacterProfile
from .world import WorldConflict, WorldStore


def revise_profile(store, previous, updated, reason, *, shared_owner=False):
    """Change author material only. Accepted outcomes, deadlines and deliveries survive."""
    return _revise_profile(store,previous,updated,reason,shared_owner=shared_owner)


def revise_visual_reference(store, previous, updated, reason, *, shared_owner=False):
    """Explicit approval of this actor's portrait/camera, never a voice or life reset."""
    return _revise_profile(store,previous,updated,reason,shared_owner=shared_owner,visual=True)


def _revise_profile(store, previous, updated, reason, *, shared_owner=False, visual=False):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('an author revision needs a reason')
    def reference_bytes(profile):
        return {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key,path in profile.references.items()}
    old_refs,new_refs=reference_bytes(previous),reference_bytes(updated)
    if visual:
        fields=('actor_id','name','environment','persona','speech_contract','canon','memories','concerns','runtime')
        if any(getattr(previous,k)!=getattr(updated,k) for k in fields):
            raise WorldConflict('portrait migration cannot change voice, canon, memory or runtime')
        subject=previous.actor_id
        if subject not in new_refs or {k:v for k,v in old_refs.items() if k!=subject}!={k:v for k,v in new_refs.items() if k!=subject}:
            raise WorldConflict('portrait migration changes only the approved actor reference')
    if (previous.actor_id != updated.actor_id or previous.environment != updated.environment or
            not visual and old_refs != new_refs):
        raise WorldConflict('identity, environment or visual changes require a separate migration')
    old_memories = {r['id']: r for r in previous.memories}
    new_memories = {r['id']: r for r in updated.memories}
    if not old_memories.keys() <= new_memories.keys():
        raise WorldConflict('an author revision cannot remove memories')
    with store.lock, (nullcontext() if store.db.in_transaction else store.db):
        actor = previous.actor_id
        binding = store.get('host_profile')
        packages = store.get('character_packages') or {}
        shared = store.get('shared_host') if shared_owner else None
        binding_matches = (shared and shared['profiles'].get(actor) == previous.digest) if shared_owner else (
            binding == {'actor_id': actor, 'name': previous.name, 'digest': previous.digest})
        if (not binding_matches or packages.get(actor) != previous.digest or
                (store.get('basis') or {}).get(actor) != previous.canon):
            raise WorldConflict('previous profile does not match installed author material')
        if not store.get('paused'):
            raise WorldConflict('pause and stop the host before revising its profile')
        revision = store.get('revision')
        pending = [dict(r) for r in store.db.execute(
            "SELECT * FROM world_cycles WHERE status IN ('ready','running','blocked','waiting','held')")]
        # Only a fully committed physical result may cross this author boundary.
        # Do not rebase an unfinished call or a speech service with old evidence.
        if shared_owner:
            if (any((store.actor_get(a['id'], 'life_status') or {}).get('status') == 'running' for a in store.state()['actors']) or
                    any(c['status'] not in ('waiting','held') or c['phase'] != 'outcome' or
                        not json.loads(c['value']).get('shared_window') or
                        'outcome' in json.loads(c['value']).get('results', {}) for c in pending)):
                raise WorldConflict('pause both characters and finish pending stages before author revision')
        elif (len(pending) > 1 or any(c['actor_id'] != actor or c['status'] != 'ready' or
                c['phase'] != 'experience' or c['revision'] != revision or
                'outcome' not in json.loads(c['value']).get('results', {}) or
                json.loads(c['value']).get('suspended_cycle_id') for c in pending)):
            raise WorldConflict('finish or explicitly review pending stages before author revision')
        if store.db.execute("SELECT 1 FROM world_calls WHERE status='started'").fetchone():
            raise WorldConflict('a model call is still in flight')
        existing = {m['id']: m for m in store.memories(actor)}
        for key, record in old_memories.items():
            if existing.get(key) != record:
                raise WorldConflict('an authored memory changed since the previous profile')
        for key in new_memories.keys() - old_memories.keys():
            if key in existing:
                raise WorldConflict('new authored memory collides with lived memory')
        at = datetime.now(timezone.utc).isoformat()
        audit = {'actor_id': actor, 'at': at, 'reason': reason,
                 'kind':'visual_reference' if visual else 'author_material',
                 'previous_reference_digests':old_refs,'reference_digests':new_refs,
                 'previous_digest': previous.digest, 'digest': updated.digest,
                 'previous_name': previous.name, 'name': updated.name,
                 'previous_persona': previous.persona, 'persona': updated.persona,
                 'previous_contract': previous.speech_contract, 'contract': updated.speech_contract,
                 'previous_canon': previous.canon, 'canon': updated.canon,
                 'previous_runtime': previous.runtime, 'runtime': updated.runtime,
                 'previous_photo_style':previous.photo_style,'photo_style':updated.photo_style,
                 'previous_memories': list(previous.memories), 'memories': list(updated.memories),
                 'pending_cycle_ids': [c['id'] for c in pending],
                 'concerns_policy': 'preserve all lived concerns; do not re-seed'}
        store.put('profile_revisions', [*(store.get('profile_revisions') or []), audit])
        store.put('basis', {**store.get('basis'), actor: updated.canon})
        if not visual:
            store.db.execute('UPDATE world_actors SET name=? WHERE id=?', (updated.name, actor))
            entity = json.loads(store.db.execute('SELECT value FROM world_entities WHERE id=?', (actor,)).fetchone()[0])
            entity['name'] = updated.name
            store.db.execute('UPDATE world_entities SET value=? WHERE id=?', (json.dumps(entity, ensure_ascii=False), actor))
            for key, record in new_memories.items():
                store.db.execute('INSERT OR REPLACE INTO world_memories VALUES(?,?,?)',
                                 (actor, key, json.dumps(record, ensure_ascii=False)))
        store.put('character_packages', {**packages, actor: updated.digest})
        if shared_owner:
            store.put('shared_host', {**shared, 'profiles': {**shared['profiles'], actor: updated.digest}})
        else:
            store.put('host_profile', {'actor_id': actor, 'name': updated.name, 'digest': updated.digest})
        store.put('revision', revision + 1)
        for cycle in pending:
            if shared_owner:
                store.db.execute('UPDATE world_cycles SET revision=? WHERE id=?', (revision+1, cycle['id']))
            else:
                store.rebase_waiting_cycle(cycle['id'], actor, revision)
        store.snapshot()
        return {k: audit[k] for k in ('actor_id', 'at', 'reason', 'previous_digest', 'digest', 'pending_cycle_ids')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    if not (args.data_dir / 'world.sqlite3').is_file():
        parser.error('existing world required')
    previous, updated = CharacterProfile.load(args.previous), CharacterProfile.load(args.profile)
    store = WorldStore(args.data_dir, seed_garik=False)  # Exclusive process ownership.
    try:
        if not store.get('paused'):
            raise WorldConflict('pause the host before shutdown and migration')
        args.backup_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        path = args.backup_dir / 'world.sqlite3'
        with sqlite3.connect(path) as backup:
            store.db.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise WorldConflict('backup failed integrity check')
        os.chmod(path, 0o600)
        result = revise_profile(store, previous, updated, args.reason)
        print(json.dumps({**result, 'backup': str(path)}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == '__main__':
    main()
