"""The shipped Arkady package in disposable worlds, with scripted model answers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.world import WorldSession, WorldStore
from test_world import Backend, choice, outcome
from test_world_personal import experience, quiet


MANIFEST = Path(__file__).resolve().parents[1] / 'characters/arkady/character.json'


class ArkadyProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = CharacterProfile.load(MANIFEST)
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()

    def world(self, name, **kwargs):
        store = WorldStore(Path(self.temp.name) / name, **kwargs)
        self.stores.append(store)
        return store

    def test_package_imports_into_empty_world_without_garik_or_external_calls(self):
        store = self.world('empty', seed_garik=False)
        self.profile.install(store)
        view = store.actor_view('arkady')
        self.assertEqual(view['basis'], {'arkady': self.profile.canon})
        self.assertEqual([a['id'] for a in view['actors']], ['arkady'])
        self.assertEqual({e['id'] for e in view['entities']},
                         {e['id'] for e in self.profile.environment} | {'arkady'})
        self.assertTrue(all(e['known_to'] == ['arkady'] for e in view['entities']))
        self.assertNotIn('world_note', json.dumps(view))
        self.assertFalse(store.monitor()['calls'])
        self.assertFalse(store.monitor()['posts'])

    def test_install_beside_garik_preserves_canon_and_keeps_places_private(self):
        store = self.world('shared')
        before = store.actor_view('garik')
        self.profile.install(store)
        after = store.actor_view('garik')
        self.assertEqual(after['basis'], before['basis'])
        self.assertEqual(after['entities'], before['entities'])
        own = store.actor_view('arkady')
        self.assertEqual(own['basis'], {'arkady': self.profile.canon})
        self.assertEqual({e['id'] for e in own['entities']},
                         {e['id'] for e in self.profile.environment} | {'arkady'})
        self.assertEqual({a['id'] for a in store.state()['actors']}, {'garik', 'arkady'})
        # Importing friendship never writes a diary or starts a conversation.
        self.assertFalse(after['history'])
        self.assertFalse(own['history'])

    def test_own_turn_persists_result_and_resumes_unfinished_work_after_restart(self):
        store = self.world('life', seed_garik=False)
        self.profile.install(store)
        with store.lock, store.db:
            store.register_actor('observer', 'Другой управляемый герой')
        store.set_settings({**store.get('settings'), 'events_per_hour': 0,
                            'post_cooldown_minutes': 0}, 0)
        store.pause(False)
        observed = quiet()
        observed.update(entity_ids=['arkady_garage'], visible_to=['arkady'],
                        observation='В мастерской спокойно.')
        inspected = copy.deepcopy(next(e for e in self.profile.environment
                                      if e['id'] == 'arkady_transmitter'))
        inspected['condition'] = 'Осмотрен: крепление разъёма требует отдельного ремонта.'
        result = outcome(observation='Аркадий осмотрел передатчик; разъём ещё не починен.',
                         entities=[inspected])
        intent = 'Осмотрю крепление разъёма передатчика без запуска.'
        backend = Backend(observed, intent,
                          choice(action=intent,
                                 entity_ids=['arkady_transmitter']), result)

        def session(backend):
            return WorldSession(store, backend, {'arkady': self.profile.persona},
                                self.profile.speech_contract, personal_life=True,
                                active_world=True, autonomous_posts=True, direct_channel=True)

        run = session(backend)
        cycle = run.prepare('arkady', minutes=10, draw=1)
        source = cycle['id'] + '-outcome'
        concern = {'id': 'connector_repair', 'text': 'Крепление разъёма ещё нужно починить.',
                   'stake': 'Не хочу повредить передатчик при следующем запуске.',
                   'next_hint': 'Подобрать крепление.', 'status': 'active', 'source_ids': [source]}
        post = 'Расчёт потом. Сначала разъём закреплю.'
        backend.answers += [experience(source, note='Осмотрел. Ремонт оставил на следующий заход.',
                                       concerns=[concern]),
                            {'decision': 'post', 'text': post, 'source_ids': [source]}]
        for _ in range(6):
            run.step(cycle['id'])
        self.assertEqual(store.cycle(cycle['id'])['status'], 'complete')
        self.assertEqual([x[0] for x in backend.inputs],
                         ['world', 'impulse', 'choice', 'outcome', 'experience', 'channel'])
        self.assertIn(self.profile.persona, backend.inputs[1][1])
        self.assertIn(self.profile.speech_contract, backend.inputs[-1][1])
        self.assertNotIn(self.profile.persona, backend.inputs[0][1])
        self.assertEqual(store.monitor()['posts'][0]['value']['text'], post)
        self.assertNotIn(concern['text'], json.dumps(store.actor_view('observer'), ensure_ascii=False))

        # Reopening and reimporting must keep both the physical result and the pending matter.
        event_count = len(store.state()['history'])
        store.close()
        self.stores.remove(store)
        store = self.world('life', seed_garik=False)
        store.pause(True)
        revision = store.get('revision')
        self.profile.install(store)
        self.assertEqual(store.get('revision'), revision)
        self.assertEqual(len(store.state()['history']), event_count)
        self.assertIn(concern, store.actor_view('arkady')['concerns'])
        self.assertEqual(len(store.actor_view('arkady')['memories']), len(self.profile.memories))
        actual = next(e for e in store.actor_view('arkady')['entities']
                      if e['id'] == 'arkady_transmitter')
        self.assertEqual(actual['condition'], inspected['condition'])
        store.pause(False)
        following = Backend(observed, 'Вернусь к креплению разъёма.',
                            choice(action='Вернусь к креплению разъёма.',
                                   entity_ids=['arkady_transmitter']),
                            outcome(observation='Подобрано крепление; установка ещё впереди.'))
        run = session(following)
        second = run.prepare('arkady', minutes=10, draw=1)
        following.answers += [experience(second['id'] + '-outcome', concerns=[concern]),
                              {'decision': 'keep', 'text': '', 'source_ids': []}]
        for _ in range(6):
            run.step(second['id'])
        self.assertIn(concern, following.inputs[1][2]['life']['concerns'])
        self.assertEqual(store.cycle(second['id'])['status'], 'complete')
        self.assertEqual(len(store.monitor()['posts']), 1)
        self.assertEqual(store.actor_view('observer')['concerns'], [])


if __name__ == '__main__':
    unittest.main()
