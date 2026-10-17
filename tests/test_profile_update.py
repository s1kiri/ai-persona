"""Author revisions preserve life and accepted outcomes, with explicit provenance."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.profile_host import open_world
from persona_runtime.profile_update import revise_profile, revise_visual_reference
from persona_runtime.world import WorldConflict
import test_character_profile as fixtures
import test_comments_during_action as waiting


class ProfileSectionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.path = self.fixture.package()

    def save(self, **changes):
        value = json.loads(self.path.read_text())
        value.update(changes)
        self.path.write_text(json.dumps(value))

    def test_sections_reach_model_persona_and_fingerprint(self):
        voice = self.path.parent / 'voice.md'
        voice.write_text('Own voice and preferences.')
        self.save(persona_sections=['voice.md'], runtime={'model': 'gpt-6-luna', 'reasoning_effort': 'none'})
        first = CharacterProfile.load(self.path)
        self.assertIn('Own voice and preferences.', first.persona)
        self.assertEqual(first.runtime, {'model': 'gpt-6-luna', 'reasoning_effort': 'none'})
        voice.write_text('Revised voice.')
        self.assertNotEqual(first.digest, CharacterProfile.load(self.path).digest)
        self.save(runtime={'model': 'gpt-6-luna', 'reasoning_effort': 'low'})
        second = CharacterProfile.load(self.path)
        self.save(runtime={'model': 'gpt-6.1-sol', 'reasoning_effort': 'low'})
        self.assertNotEqual(second.digest, CharacterProfile.load(self.path).digest)

    def test_sections_cannot_escape_package_or_exceed_total_limit(self):
        outside = self.path.parent.parent / 'outside.md'
        outside.write_text('Outside')
        for sections in (['../outside.md'], ['persona.md'], ['voice.md', 'voice.md'], 'voice.md', []):
            self.save(persona_sections=sections)
            with self.assertRaises(ValueError):
                CharacterProfile.load(self.path)
        (self.path.parent / 'voice.md').symlink_to(outside)
        self.save(persona_sections=['voice.md'])
        with self.assertRaises(ValueError):
            CharacterProfile.load(self.path)
        (self.path.parent / 'voice.md').unlink()
        (self.path.parent / 'voice.md').write_text('x' * 100_000)
        with self.assertRaises(ValueError):
            CharacterProfile.load(self.path)

    def test_incomplete_or_invalid_model_settings_fail_before_startup(self):
        for value in ({'model': 'gpt-6-luna'}, {'model': '', 'reasoning_effort': 'none'},
                      {'model': 'test', 'reasoning_effort': 'unbounded'}, 'test'):
            self.save(runtime=value)
            with self.assertRaises(ValueError):
                CharacterProfile.load(self.path)


class ProfileRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = CharacterProfile.load(Path(__file__).parents[1] / 'characters/arkady/character.json')
        self.store = open_world(self.root / 'world', self.previous)
        self.addCleanup(lambda: self.store.close())
        record = {'id': 'arkady_memory_new_author_interest', 'text': 'A new authored preference.',
                  'entity_ids': ['arkady'], 'provenance': 'author_backstory'}
        self.updated = replace(self.previous, name='Аркадий Кедров (редакция)',
                               persona=self.previous.persona + '\nAuthor revision.',
                               canon=self.previous.canon + '\nNew author fact.',
                               digest='revision-fixture', memories=(*self.previous.memories, record))

    def test_approved_portrait_migration_preserves_life_and_rejects_other_changes(self):
        portrait=self.root/'approved.jpg';portrait.write_bytes(b'approved-reference-fixture')
        updated=replace(self.previous,references={**self.previous.references,'arkady':str(portrait)},
                        photo_style='Old phone, identity from reference only.',digest='portrait-fixture')
        before={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)]
                for t in ('world_events','world_calls','world_memories','world_concerns','world_undertakings')}
        with self.assertRaises(WorldConflict):revise_profile(self.store,self.previous,updated,'Not a generic author edit')
        with self.assertRaises(WorldConflict):revise_visual_reference(self.store,self.previous,replace(updated,persona='Other voice'),'Portrait only')
        with self.assertRaises(WorldConflict):revise_visual_reference(self.store,self.previous,replace(updated,references={**updated.references,'arkady_garage':str(portrait)}),'Own appearance only')
        revise_visual_reference(self.store,self.previous,updated,'Owner supplied and approved portrait')
        self.assertEqual(self.store.get('character_packages')['arkady'],updated.digest)
        self.assertEqual(self.store.get('profile_revisions')[-1]['kind'],'visual_reference')
        for t,rows in before.items():self.assertEqual(rows,[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)],t)

    def test_portrait_migration_requires_paused_world(self):
        portrait=self.root/'approved.jpg';portrait.write_bytes(b'approved-reference-fixture')
        updated=replace(self.previous,references={**self.previous.references,'arkady':str(portrait)},digest='portrait-fixture')
        self.store.pause(False)
        with self.assertRaises(WorldConflict):revise_visual_reference(self.store,self.previous,updated,'Owner approval still needs safe boundary')

    def test_revision_preserves_concerns_events_controls_deliveries_and_entity_condition(self):
        with self.store.lock, self.store.db:
            self.store.actor_put('arkady', 'life_controls', {'sharing_appetite': 90})
            row = self.store.db.execute("SELECT value FROM world_entities WHERE id='arkady_garage'").fetchone()
            garage = json.loads(row[0]); garage['condition'] = 'Lived condition'
            self.store.db.execute("UPDATE world_entities SET value=? WHERE id='arkady_garage'", (json.dumps(garage),))
            self.store.db.execute('CREATE TABLE channel_jobs(post_id TEXT,status TEXT)')
            self.store.db.execute("INSERT INTO channel_jobs VALUES('post','unknown')")
        concerns = copy.deepcopy(self.store.actor_view('arkady')['concerns'])
        state = copy.deepcopy(self.store.state())
        revised = revise_profile(self.store, self.previous, self.updated, 'Author request')
        self.assertEqual(revised['previous_digest'], self.previous.digest)
        self.assertEqual(self.store.actor_view('arkady')['concerns'], concerns)
        self.assertEqual(self.store.state()['history'], state['history'])
        self.assertEqual(self.store.get('minute'), state['minute'])
        self.assertEqual(self.store.actor_get('arkady', 'life_controls'), {'sharing_appetite': 90})
        self.assertEqual(self.store.db.execute('SELECT status FROM channel_jobs').fetchone()[0], 'unknown')
        garage_after = next(e for e in self.store.state()['entities'] if e['id'] == garage['id'])
        self.assertEqual(garage_after, garage)
        self.assertEqual(len(self.store.memories('arkady')), len(self.previous.memories) + 1)
        self.store.close()
        self.store = open_world(self.root / 'world', self.updated)
        self.assertEqual(self.store.get('host_profile')['name'], self.updated.name)
        with self.assertRaises(WorldConflict):
            revise_profile(self.store, self.previous, self.updated, 'Do not repeat')

    def test_rejects_live_world_wrong_source_and_changed_environment_atomically(self):
        original = copy.deepcopy(self.store.state())
        for previous, updated in ((replace(self.previous, digest='wrong'), self.updated),
                                  (self.previous, replace(self.updated, environment=()))):
            with self.assertRaises(WorldConflict):
                revise_profile(self.store, previous, updated, 'Request')
            self.assertEqual(self.store.state(), original)
        self.store.pause(False)
        with self.assertRaises(WorldConflict):
            revise_profile(self.store, self.previous, self.updated, 'Request')

    def test_rejects_memory_collision_without_partial_writes(self):
        with self.store.lock, self.store.db:
            record = self.updated.memories[-1]
            self.store.db.execute('INSERT INTO world_memories VALUES(?,?,?)',
                                  ('arkady', record['id'], json.dumps({**record, 'text': 'Lived fact'})))
        before = copy.deepcopy(self.store.state())
        with self.assertRaises(WorldConflict):
            revise_profile(self.store, self.previous, self.updated, 'Request')
        self.assertEqual(self.store.state(), before)
        self.assertIsNone(self.store.get('profile_revisions'))


class WaitingRevisionTests(unittest.TestCase):
    def test_revision_preserves_outcome_and_deadline_without_repeating_action(self):
        fixture = waiting.WaitingCommentsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.fixture
        cycle = f.store.cycle(fixture.cycle['id'])
        deadline = f.life.get('life_status')['next_wake']
        calls = copy.deepcopy(f.store.monitor()['calls'])
        f.store.pause(True)
        updated = replace(f.profile, digest='reviewed-waiting-revision', persona=f.profile.persona + '\nNew voice')
        revise_profile(f.store, f.profile, updated, 'User changes voice while awaiting accepted outcome')
        revised = f.store.cycle(cycle['id'])
        self.assertEqual(revised['value'], cycle['value'])
        self.assertEqual(revised['phase'], 'experience')
        self.assertEqual(revised['revision'], f.store.get('revision'))
        self.assertEqual(f.life.get('life_status')['next_wake'], deadline)
        self.assertEqual(f.store.monitor()['calls'], calls)
        self.assertEqual(f.store.get('profile_revisions')[-1]['pending_cycle_ids'], [cycle['id']])
        with f.store.lock, f.store.db:
            f.store.db.execute("UPDATE world_cycles SET status='blocked' WHERE id=?", (cycle['id'],))
        with self.assertRaises(WorldConflict):
            revise_profile(f.store, updated, replace(updated, digest='next'), 'No bypass')


if __name__ == '__main__':
    unittest.main()
