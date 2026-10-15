"""Portable packages, isolated actor knowledge and imports without runtime side effects."""
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.world import WorldStore, WorldConflict, WorldSession
from test_world import Backend, choice, outcome
from test_world_personal import quiet, experience


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorldStore(self.root/'world', seed_garik=False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def package(self, actor='friend'):
        folder=self.root/actor;folder.mkdir(exist_ok=True)
        (folder/'persona.md').write_text('A different fictional personality.',encoding='utf-8')
        (folder/'speech.md').write_text('Speak in your own voice.',encoding='utf-8')
        (folder/'canon.md').write_text('A human who repairs bicycles.',encoding='utf-8')
        entity={'id':actor+'_home','kind':'place','name':'Home','description':'A room.',
                'condition':'Quiet.','known_to':[actor],'world_note':'Private NPC information.'}
        (folder/'world.json').write_text(json.dumps([entity]),encoding='utf-8')
        value={'version':1,'id':actor,'name':actor.title(),'persona':'persona.md',
               'speech_contract':'speech.md','canon':'canon.md','environment':'world.json'}
        manifest=folder/'character.json';manifest.write_text(json.dumps(value),encoding='utf-8')
        return manifest

    def test_fresh_world_can_be_empty_and_import_does_not_inherit_garik(self):
        self.assertFalse(self.store.state()['actors'])
        p=CharacterProfile.load(self.package());p.install(self.store)
        view=self.store.actor_view('friend')
        self.assertEqual(view['basis'],{'friend':p.canon})
        self.assertEqual([a['id'] for a in view['actors']],['friend'])
        self.assertNotIn('world_note',json.dumps(view))
        self.assertFalse(self.store.monitor()['calls'])
        self.store.close()
        self.store=WorldStore(self.root/'world')
        self.assertEqual([a['id'] for a in self.store.state()['actors']],['friend'])

    def test_two_packages_share_world_without_reading_each_others_private_places(self):
        a=CharacterProfile.load(self.package('first'));b=CharacterProfile.load(self.package('second'))
        a.install(self.store);b.install(self.store)
        self.assertEqual(len(self.store.state()['actors']),2)
        self.assertNotIn('second_home',[e['id'] for e in self.store.actor_view('first')['entities']])
        self.assertEqual(self.store.actor_view('second')['basis'],{'second':b.canon})

    def test_import_is_idempotent_and_never_resets_lived_state(self):
        p=CharacterProfile.load(self.package());p.install(self.store)
        with self.store.lock,self.store.db:
            row=self.store.db.execute("SELECT value FROM world_entities WHERE id='friend_home'").fetchone()
            entity=json.loads(row[0]);entity['condition']='Changed after an episode.'
            self.store.db.execute("UPDATE world_entities SET value=? WHERE id='friend_home'",(json.dumps(entity),))
        revision=self.store.get('revision');p.install(self.store)
        self.assertEqual(self.store.get('revision'),revision)
        self.assertEqual(self.store.actor_view('friend')['entities'][-1]['condition'],'Changed after an episode.')
        (self.root/'friend'/'canon.md').write_text('Changed canon.',encoding='utf-8')
        with self.assertRaises(WorldConflict):CharacterProfile.load(self.root/'friend'/'character.json').install(self.store)

    def test_failed_import_rolls_back_actor_and_canon(self):
        a=CharacterProfile.load(self.package('first'));a.install(self.store)
        manifest=self.package('second');path=manifest.parent/'world.json';value=json.loads(path.read_text())
        value[0]['id']='first_home';path.write_text(json.dumps(value))
        with self.assertRaises(WorldConflict):CharacterProfile.load(manifest).install(self.store)
        self.assertNotIn('second',[a['id'] for a in self.store.state()['actors']])
        self.assertNotIn('second',self.store.get('basis'))

    def test_paths_and_identity_are_validated(self):
        manifest=self.package();value=json.loads(manifest.read_text());value['persona']='../outside.md'
        (self.root/'outside.md').write_text('Outside package.')
        manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):CharacterProfile.load(manifest)
        value['persona']='persona.md';value['id']='../garik';manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):CharacterProfile.load(manifest)
        value['id']='friend';value['persona']='link.md';manifest.write_text(json.dumps(value))
        (manifest.parent/'link.md').symlink_to(self.root/'outside.md')
        with self.assertRaises(ValueError):CharacterProfile.load(manifest)

    def test_other_character_takes_world_turn_with_its_persona_and_own_sources(self):
        p=CharacterProfile.load(self.package());p.install(self.store);self.store.pause(False)
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':0},0)
        observation=quiet();observation.update(entity_ids=['friend_home'],visible_to=['friend'])
        backend=Backend(observation, 'Rest.',choice(action='Rest.',entity_ids=['friend_home']),outcome())
        session=WorldSession(self.store,backend,{'friend':p.persona},p.speech_contract,
                             personal_life=True,active_world=True,autonomous_posts=True,direct_channel=True)
        cycle=session.prepare('friend',minutes=10,draw=1)
        backend.answers += [experience(cycle['id']+'-outcome'),
                            {'decision':'post','text':'My character post','source_ids':[cycle['id']+'-outcome']}]
        for _ in range(6):session.step(cycle['id'])
        self.assertIn(p.persona,backend.inputs[1][1])
        self.assertNotIn('Ты Гарик',backend.inputs[1][1])
        self.assertEqual(self.store.cycle(cycle['id'])['actor_id'],'friend')
        self.assertEqual(self.store.state()['history'][-1]['actor_id'],'friend')
        self.assertEqual(self.store.cycle(cycle['id'])['status'],'complete')
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'],'My character post')


if __name__=='__main__':unittest.main()
