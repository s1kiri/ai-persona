"""Portable, source-only character package. Loading never calls a model or Telegram."""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from .world import WorldConflict
from .world_protocol import ENTITY, CONCERN, identifier, validate_shape


@dataclass(frozen=True)
class CharacterProfile:
    actor_id: str
    name: str
    persona: str
    speech_contract: str
    canon: str
    environment: tuple
    digest: str
    memories: tuple = ()
    concerns: tuple = ()
    references: dict = field(default_factory=dict)

    @classmethod
    def load(cls, manifest):
        path = Path(manifest).resolve()
        value = json.loads(path.read_text(encoding='utf-8'))
        required = {'version', 'id', 'name', 'persona', 'speech_contract', 'canon', 'environment'}
        optional = {'memory', 'visual_references'}
        if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional or
                type(value['version']) is not int or value['version'] != 1):
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
        memories, concerns, references, reference_digests = [], [], {}, {}
        if 'memory' in value:
            memory = json.loads(read('memory', 100_000))
            if (not isinstance(memory, dict) or set(memory) != {'version', 'records', 'concerns'} or
                    type(memory['version']) is not int or memory['version'] != 1 or
                    not isinstance(memory['records'], list) or len(memory['records']) > 64 or
                    not isinstance(memory['concerns'], list) or len(memory['concerns']) > 8):
                raise ValueError('invalid initial memory')
            memory_ids = set()
            for record in memory['records']:
                if (not isinstance(record, dict) or set(record) != {'id', 'text', 'entity_ids', 'provenance'} or
                        record['provenance'] != 'author_backstory' or
                        not isinstance(record['text'], str) or not 0 < len(record['text'].strip()) <= 2000 or
                        not isinstance(record['entity_ids'], list) or
                        any(not isinstance(e, str) or e not in seen for e in record['entity_ids'])):
                    raise ValueError('initial memory must be sourced author backstory about package entities')
                identifier(record['id'])
                if not record['id'].startswith(value['id'] + '_memory_') or record['id'] in memory_ids:
                    raise ValueError('initial memory ids must be unique and actor-scoped')
                memory_ids.add(record['id'])
                memories.append(record)
            concern_ids = set()
            for concern in memory['concerns']:
                validate_shape(concern, CONCERN)
                identifier(concern['id'])
                if (concern['id'] in concern_ids or not concern['source_ids'] or
                        not set(concern['source_ids']) <= memory_ids):
                    raise ValueError('initial concerns need unique ids and own memory sources')
                concern_ids.add(concern['id'])
                concerns.append(concern)
        if 'visual_references' in value:
            refs = json.loads(read('visual_references', 16_000))
            if not isinstance(refs, dict) or len(refs) > 64:
                raise ValueError('invalid visual reference mapping')
            for subject, relative in refs.items():
                if subject not in seen or not isinstance(relative, str) or Path(relative).is_absolute():
                    raise ValueError('visual reference must belong to a package subject')
                target = (path.parent / relative).resolve()
                if (not target.is_relative_to(path.parent) or not target.is_file() or
                        target.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'} or
                        target.stat().st_size > 40_000_000):
                    raise ValueError('invalid visual reference file')
                references[subject] = str(target)
                reference_digests[subject] = hashlib.sha256(target.read_bytes()).hexdigest()
        normalized = {'id': value['id'], 'name': value['name'], 'persona': persona,
                      'speech_contract': contract, 'canon': canon, 'environment': environment}
        if 'memory' in value:
            normalized['memory'] = {'records': memories, 'concerns': concerns}
        if 'visual_references' in value:
            normalized['visual_references'] = reference_digests
        digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return cls(value['id'], value['name'], persona, contract, canon, tuple(environment), digest,
                   tuple(memories), tuple(concerns), references)

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
            for record in self.memories:
                store.db.execute('INSERT INTO world_memories VALUES(?,?,?)',
                                 (self.actor_id, record['id'], json.dumps(record, ensure_ascii=False)))
            for concern in self.concerns:
                store.db.execute('INSERT INTO world_concerns VALUES(?,?,?,?)',
                                 (self.actor_id, concern['id'], json.dumps(concern, ensure_ascii=False),
                                  'character-package:' + self.actor_id))
            store.put('character_packages', {**packages, self.actor_id: self.digest})
            store.put('revision', store.get('revision') + 1)
            store.snapshot()
