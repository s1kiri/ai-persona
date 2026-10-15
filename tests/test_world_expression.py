"""Whole-episode time, private intentions and voluntary local channel drafts."""
import json
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from persona_runtime.world_protocol import clock_at, validate_result
from test_world import Backend, event, choice, outcome


class ExpressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def setup_cycle(self, *answers, enabled=True, pattern='test-invitation'):
        cycle = self.store.prepare(draw=0)
        backend = Backend(event(pattern=pattern, threads=[] ,thread_id=None), *answers)
        session = WorldSession(self.store, backend, {'garik': 'Synthetic personality'}, 'Synthetic speech',
                               autonomous_posts=enabled)
        return cycle, session, backend

    def test_hour_of_practice_and_travel_cannot_commit_as_one_minute(self):
        bad = outcome(elapsed_minutes=1, time_costs=[{'activity': 'Practice', 'minutes': 60},
                                                  {'activity': 'Travel', 'minutes': 75}])
        cycle, session, _ = self.setup_cycle(choice(), bad, enabled=False)
        session.step(cycle['id']); session.step(cycle['id'])
        before = self.store.get('minute')
        with self.assertRaises(ValueError):
            session.step(cycle['id'])
        self.assertEqual(self.store.get('minute'), before)
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'blocked')

    def test_long_episode_uses_one_clock_and_is_applied_once(self):
        result = outcome(elapsed_minutes=135, time_costs=[{'activity': 'Travel', 'minutes': 75},
                                                        {'activity': 'Practice', 'minutes': 60}],
                         observation='Synthetic activity. Сейчас 02:45.')
        cycle, session, backend = self.setup_cycle(choice(), result, enabled=False)
        for _ in range(3): session.step(cycle['id'])
        self.assertEqual(self.store.get('minute'), 165)
        self.assertEqual(backend.inputs[-1][2]['clock'], clock_at(30))
        call = self.store.monitor()['calls'][0]
        self.assertFalse(self.store.complete(call['id'], result, {}))
        self.assertEqual(self.store.get('minute'), 165)

    def test_narrated_now_must_match_clock_but_future_appointment_can_differ(self):
        cycle = self.store.prepare(draw=0)
        state, brief = self.store.state(), cycle['value']['brief']
        validate_result('world', event(observation='Встреча завтра в 19:00.'), state, brief)
        with self.assertRaises(ValueError):
            validate_result('outcome', outcome(observation='Сейчас около 12:30.'), state, brief)

    def test_intentions_persist_for_the_actor_and_do_not_enter_world_context(self):
        want = {'id': 'my-idea', 'want': 'PRIVATE-UNDERTAKING', 'status': 'active'}
        cycle, session, backend = self.setup_cycle(choice(undertakings=[want], reaction='PRIVATE-REACTION'),
                                                    outcome(), enabled=False)
        for _ in range(3): session.step(cycle['id'])
        self.assertEqual(self.store.actor_view('garik')['undertakings'], [want])
        self.assertNotIn('PRIVATE-UNDERTAKING', json.dumps(backend.inputs[-1][2]))
        self.assertNotIn('PRIVATE-REACTION', json.dumps(backend.inputs[-1][2]))
        with self.store.db: self.store.register_actor('other', 'Other')
        self.assertEqual(self.store.actor_view('other')['undertakings'], [])

    def test_character_can_keep_an_episode_private_without_generating_a_post(self):
        cycle, session, backend = self.setup_cycle(choice(), outcome(),
                                                   {'decision': 'keep', 'focus': '', 'source_ids': []})
        for _ in range(4): session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'complete')
        self.assertEqual([v[0] for v in backend.inputs], ['world', 'choice', 'outcome', 'share'])
        self.assertEqual(self.store.monitor()['posts'], [])

    def test_selected_post_is_plain_speech_with_observed_sources_only(self):
        cycle = self.store.prepare(draw=0)
        backend = Backend(event(threads=[], thread_id=None),
                          choice(note='PRIVATE-DIARY', reaction='PRIVATE-REACTION'), outcome(),
                          {'decision': 'post', 'focus': 'My impression.', 'source_ids': [cycle['id']+'-outcome']},
                          'Synthetic channel text.')
        session = WorldSession(self.store, backend, {'garik': 'Synthetic'}, 'Synthetic speech', autonomous_posts=True)
        for _ in range(5): session.step(cycle['id'])
        text = json.dumps(backend.inputs[-1][2])
        self.assertNotIn('PRIVATE-DIARY', text)
        self.assertNotIn('PRIVATE-REACTION', text)
        self.assertNotIn('Synthetic backstage', text)
        posts = self.store.monitor()['posts']
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]['value']['autonomous'])
        self.assertTrue(posts[0]['value']['draft'])
        self.assertFalse(self.store.posting_allowed('garik'))
        self.store.prepare(minutes=90, draw=0)
        self.assertTrue(self.store.posting_allowed('garik'))

    def test_private_choice_is_not_a_public_factual_source(self):
        cycle, session, _ = self.setup_cycle(choice(), outcome(), enabled=False)
        for _ in range(3): session.step(cycle['id'])
        proposal = {'decision': 'post', 'focus': 'Synthetic.', 'source_ids': [cycle['id']+'-choice']}
        with self.assertRaises(ValueError):
            validate_result('share', proposal, self.store.state(), cycle['value']['brief'])

    def test_cooldown_can_expire_during_the_episode(self):
        cycle, session, backend = self.setup_cycle(choice(), outcome())
        backend.answers += [{'decision': 'post', 'focus': 'Synthetic focus.',
                             'source_ids': [cycle['id']+'-outcome']}, 'Synthetic post.']
        for _ in range(5): session.step(cycle['id'])
        second, session, _ = self.setup_cycle(choice(), outcome(elapsed_minutes=60), pattern='another-invitation')
        self.assertFalse(self.store.posting_allowed('garik'))
        for _ in range(3): session.step(second['id'])
        self.assertEqual(self.store.cycle(second['id'])['phase'], 'share')

    def test_free_intention_is_private_and_adapter_cannot_rewrite_action(self):
        cycle = self.store.prepare(draw=0)
        raw = 'PRIVATE-WISH. Try an odd thing.'
        backend = Backend(event(), raw, choice(action='Try an odd thing.'), outcome())
        session = WorldSession(self.store, backend, {'garik': 'PRIVATE-PERSONALITY'}, 'Speech', free_intentions=True)
        for _ in range(4): session.step(cycle['id'])
        self.assertEqual([r[0] for r in backend.inputs], ['world', 'impulse', 'choice', 'outcome'])
        self.assertNotIn('PRIVATE-PERSONALITY', backend.inputs[2][1])
        self.assertNotIn('PRIVATE-WISH', json.dumps(backend.inputs[3][2]))
        self.assertEqual(backend.inputs[3][2]['chosen_action']['action'], 'Try an odd thing.')
        with self.store.db: self.store.register_actor('other', 'Other')
        self.assertNotIn('PRIVATE-WISH', json.dumps(self.store.actor_view('other')))

    def test_adapter_added_caution_is_rejected_without_retry(self):
        cycle = self.store.prepare(draw=0)
        backend = Backend(event(), 'Try an odd thing.', choice(action='First do a careful risk review.'))
        session = WorldSession(self.store, backend, {'garik': 'Synthetic'}, 'Speech', free_intentions=True)
        session.step(cycle['id']); session.step(cycle['id'])
        with self.assertRaises(ValueError): session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'blocked')
        self.assertEqual(len(backend.inputs), 3)

