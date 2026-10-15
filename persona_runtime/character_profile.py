"""Portable, source-only character package. Loading never calls a model or Telegram."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .world import WorldConflict
from .world_protocol import ENTITY, identifier, validate_shape


@dataclass(frozen=True)
class CharacterProfile:
    actor_id: str
    name: str
    persona: str
    speech_contract: str
    canon: str
    environment: tuple
    digest: str

    @classmethod
    def load(cls, manifest):
        path = Path(manifest).resolve()
        value = json.loads(path.read_text(encoding='utf-8'))
        required = {'version', 'id', 'name', 'persona', 'speech_contract', 'canon', 'environment'}
        if not isinstance(value, dict) or set(value) != required or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('character manifest needs the exact version 1 fields')
        identifier(value['id'])
        if not isinstance(value['name'], str) or not value['name'].strip() or len(value['name']) > 100:
            raise ValueError('character name is empty or too long')

        def read(field, limit):
            relative = value[field]
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError('character files must use relative package paths')
            target = (path.parent / relative).resolve()
            if not target.is_relative_to(path.parent) or target.stat().st_size > limit:
                raise ValueError('character file escapes package or exceeds size limit')
            text = target.read_text(encoding='utf-8')
            if not text.strip():
                raise ValueError('character file is empty: ' + field)
            return text

        persona = read('persona', 100_000)
        contract = read('speech_contract', 16_000)
        canon = read('canon', 16_000)
        environment = json.loads(read('environment', 256_000))
        if not isinstance(environment, list) or len(environment) > 64:
            raise ValueError('environment must contain at most 64 initial entities')
        seen = {value['id']}
        for entity in environment:
            validate_shape(entity, ENTITY)
            identifier(entity['id'])
            if entity['id'] in seen:
                raise ValueError('duplicate entity or collision with actor id')
            seen.add(entity['id'])
        normalized = {'id': value['id'], 'name': value['name'], 'persona': persona,
                      'speech_contract': contract, 'canon': canon, 'environment': environment}
        digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return cls(value['id'], value['name'], persona, contract, canon, tuple(environment), digest)

    def install(self, store):
        """Host operation on a paused world; atomic, idempotent, never resets lived state."""
        with store.lock, store.db:
            if not store.get('paused') or store.db.execute(
                    "SELECT 1 FROM world_cycles WHERE status IN ('ready','running','blocked')").fetchone():
                raise WorldConflict('install a character only into a paused world without pending cycles')
            packages = store.get('character_packages') or {}
            if self.actor_id in packages:
                if packages[self.actor_id] != self.digest:
                    raise WorldConflict('installed character changed; use an explicit reviewed migration')
                return
            actors = {a['id'] for a in store.state()['actors']} | {self.actor_id}
            if self.actor_id in (store.get('basis') or {}):
                raise WorldConflict('existing actor canon must not be replaced by an imported package')
            for entity in self.environment:
                if not set(entity['known_to']) <= actors:
                    raise ValueError('entity knowledge refers to an unregistered actor')
                if store.db.execute('SELECT 1 FROM world_entities WHERE id=?', (entity['id'],)).fetchone():
                    raise WorldConflict('initial entity already exists; merge shared places explicitly')
            store.register_actor(self.actor_id, self.name)
            store.put('basis', {**(store.get('basis') or {}), self.actor_id: self.canon})
            for entity in self.environment:
                store.db.execute('INSERT INTO world_entities VALUES(?,?,?)',
                                 (entity['id'], json.dumps(entity, ensure_ascii=False), 'character-package:' + self.actor_id))
            store.put('character_packages', {**packages, self.actor_id: self.digest})
            store.put('revision', store.get('revision') + 1)
            store.snapshot()
