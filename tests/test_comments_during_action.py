"""Reader turns during a real-duration wait must not expose or replay its future outcome."""
import copy
import json
import unittest

from test_channel_social import rich
import test_profile_runtime as fixtures
from test_world import choice, outcome
from test_world_personal import quiet, experience


class WaitingCommentsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArkadyLifeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        self.post = f.initial_post()
        f.store.set_settings({**f.store.get('settings'), 'events_per_hour': 0}, 0)
        observation = quiet()
        observation.update(entity_ids=['arkady_garage'], visible_to=['arkady'])
        intent = 'Полчаса разбирать передатчик.'
        changed = copy.deepcopy(next(e for e in f.profile.environment if e['id'] == 'arkady_transmitter'))
        changed['condition'] = 'FUTURE-DEVICE-RESULT'
        f.backend.answers = [observation, intent, choice(action=intent, entity_ids=['arkady_transmitter']),
                             outcome(observation='FUTURE-ACTION-RESULT', elapsed_minutes=30, entities=[changed])]
        self.cycle = f.session.prepare('arkady', minutes=0, draw=1)
        self.start_minute = f.life.minute_now()
        for _ in range(4):
            f.session.step(self.cycle['id'])
        self.future_minute = f.store.get('minute')
        self.deadline = f.life.get('life_status')['next_wake']
        self.original_outcome = copy.deepcopy(f.store.cycle(self.cycle['id'])['value']['results']['outcome'])

    def incoming(self):
        return self.fixture.receive_comment(self.post)

    def test_comment_is_answered_during_wait_without_future_knowledge_or_replay(self):
        f = self.fixture
        source = self.incoming()
        visiting = next(c for c in f.store.monitor()['cycles'] if c['value'].get('suspended_cycle_id'))
        f.backend.answers = [rich(decision='reply', text='Сейчас разбираю. Скажи, что непонятно.',
                                  source_ids=[source], read_ids=[source])]
        f.life.tick()
        payload = f.backend.inputs[-1][2]
        self.assertNotIn('FUTURE-', json.dumps(payload))
        self.assertNotIn(self.cycle['id'] + '-outcome', payload['_source_ids'])
        self.assertEqual(payload['clock']['minute'], self.start_minute)
        self.assertEqual(payload['ongoing_action'], 'Полчаса разбирать передатчик.')
        f.life.tick()
        sent = f.connection.state()['outbox']
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]['status'], 'sent')
        self.assertEqual(f.store.get('minute'), self.future_minute)
        self.assertEqual(f.store.cycle(self.cycle['id'])['phase'], 'experience')
        self.assertEqual(f.store.cycle(self.cycle['id'])['revision'], f.store.get('revision'))
        self.assertEqual(f.life.get('life_status')['next_wake'], self.deadline)
        self.assertEqual(f.store.cycle(self.cycle['id'])['value']['results']['outcome'], self.original_outcome)
        reply = f.store.db.execute("SELECT minute FROM world_events WHERE cycle_id=? AND stage='post'", (visiting['id'],)).fetchone()
        self.assertEqual(reply[0], self.start_minute)
        # At the actual end, complete only the remaining stages of the accepted action.
        f.now += 30 * 60
        f.backend.answers = [{**experience(self.cycle['id'] + '-outcome'), 'intent_updates': []}, rich()]
        f.life.tick()
        f.life.tick()
        self.assertEqual(f.store.cycle(self.cycle['id'])['status'], 'complete')
        self.assertEqual(sum(stage == 'outcome' for stage, _, _ in f.backend.inputs), 1)

    def test_future_outcome_cannot_be_cited_by_waiting_reply(self):
        f = self.fixture
        source = self.incoming()
        f.backend.answers = [rich(decision='reply', text='Уже готово.',
                                  source_ids=[source, self.cycle['id'] + '-outcome'])]
        f.life.tick()
        self.assertEqual(f.life.state()['status'], 'error')
        self.assertEqual(len(f.connection.state()['outbox']), 1)
        self.assertEqual(f.store.cycle(self.cycle['id'])['phase'], 'experience')

    def test_restart_preserves_a_prepared_comment_and_the_waiting_action(self):
        f = self.fixture
        source = self.incoming()
        visiting = next(c for c in f.store.monitor()['cycles'] if c['value'].get('suspended_cycle_id'))
        f.restart()
        f.backend.answers = [rich(decision='reply', text='Да, я здесь.', source_ids=[source], read_ids=[source])]
        f.life.tick()
        f.life.tick()
        self.assertEqual(f.store.cycle(visiting['id'])['status'], 'complete')
        self.assertEqual(f.store.cycle(self.cycle['id'])['phase'], 'experience')
        self.assertEqual(len(f.backend.inputs), 1)
        self.assertEqual(f.backend.inputs[0][0], 'live_channel')
        self.assertNotIn('FUTURE-', json.dumps(f.backend.inputs[0][2]))

    def test_unrelated_world_revision_cannot_be_silently_rebased(self):
        f = self.fixture
        self.incoming()
        with f.store.lock, f.store.db:
            f.store.put('revision', f.store.get('revision') + 1)
        f.backend.answers = [rich()]
        f.life.tick()
        self.assertEqual(f.life.state()['status'], 'error')
        self.assertEqual(f.store.cycle(self.cycle['id'])['phase'], 'experience')

    def test_own_post_during_wait_is_bounded_and_preserves_the_action(self):
        f = self.fixture
        f.life.configure({'wake_minutes': 5})
        with f.store.lock, f.store.db:
            f.life.put('life_status', {**f.life.get('life_status'), 'next_wake': f.now+300})
        self.deadline = f.now+300
        calls = len(f.backend.inputs)
        f.now += 5 * 60
        f.life.tick()  # Own channel opportunity even without incoming comments.
        visiting = next(c for c in f.store.monitor()['cycles'] if c['value'].get('suspended_cycle_id'))
        f.backend.answers = [rich(decision='post', text='Разбираю передатчик. Пока без выводов.')]
        f.life.tick()
        payload = f.backend.inputs[-1][2]
        self.assertEqual(payload['attention']['purpose'], 'own_channel')
        self.assertNotIn('FUTURE-', json.dumps(payload))
        self.assertEqual(payload['ongoing_action'], 'Полчаса разбирать передатчик.')
        f.life.tick()  # Deliver the chosen post.
        f.life.tick()  # No second opportunity on every polling tick.
        self.assertEqual(len(f.connection.state()['outbox']), 2)
        self.assertEqual(len(f.backend.inputs), calls+1)
        self.assertEqual(f.store.cycle(self.cycle['id'])['value']['results']['outcome'], self.original_outcome)
        self.assertEqual(f.store.get('minute'), self.future_minute)
        self.assertEqual(f.life.get('life_status')['next_wake'], self.deadline)
        self.assertEqual(f.store.cycle(visiting['id'])['status'], 'complete')
        check = f.life.get('waiting_channel_next_check')
        f.restart()
        f.life.tick()
        self.assertEqual(f.life.get('waiting_channel_next_check'), check)
        self.assertEqual(len(f.backend.inputs), 0)
        f.now += 5 * 60
        f.life.tick()
        self.assertTrue(any(c['value'].get('suspended_cycle_id') and c['status']=='ready'
                            for c in f.store.monitor()['cycles']))

    def test_wait_display_uses_accepted_attempt_not_future_outcome(self):
        f = self.fixture
        activity = f.life.state()['activity']
        self.assertEqual(activity['action'], 'Полчаса разбирать передатчик.')
        self.assertEqual(activity['remaining_minutes'], 30)
        self.assertNotIn('FUTURE-', json.dumps(activity))
        f.now += 61
        self.assertEqual(f.life.state()['activity']['remaining_minutes'], 29)
        f.now += 29 * 60
        self.assertIsNone(f.life.state()['activity'])

    def test_due_post_intention_during_wait_can_be_kept_without_a_busy_loop(self):
        f = self.fixture
        with f.store.lock, f.store.db:
            f.store.db.execute('INSERT INTO personal_intentions VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('arkady', 'intro', 'post', 'Рассказать о передатчике', 'pending',
                 '["arkady_memory_contact"]', '', f.now, None, f.now))
        f.life.tick()
        f.backend.answers = [rich()]
        f.life.tick()
        payload = f.backend.inputs[-1][2]
        self.assertEqual(payload['attention']['purpose'], 'own_channel')
        self.assertEqual(payload['attention']['fulfill_intent'], 'intro')
        calls = len(f.backend.inputs)
        for _ in range(5):
            f.life.tick()
        self.assertEqual(len(f.backend.inputs), calls)
        self.assertEqual(f.life.state()['status'], 'running')
        self.assertEqual(f.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()[0], 'pending')
        self.assertFalse(any(c['status']=='ready' and c['value'].get('suspended_cycle_id')
                             for c in f.store.monitor()['cycles']))

    def test_own_post_during_wait_cannot_cite_future_result(self):
        f = self.fixture
        with f.store.lock, f.store.db:
            f.life.put('own_channel_check_requested', True)
        f.life.tick()
        f.backend.answers = [rich(decision='post', text='Уже готово.',
                                  source_ids=[self.cycle['id']+'-outcome'])]
        f.life.tick()
        self.assertEqual(f.life.state()['status'], 'error')
        self.assertEqual(len(f.connection.state()['outbox']), 1)
        self.assertEqual(f.store.cycle(self.cycle['id'])['value']['results']['outcome'], self.original_outcome)


if __name__ == '__main__':
    unittest.main()
