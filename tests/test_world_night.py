"""A finite overnight driver tested with synthetic roles and a manual clock."""
import tempfile
import unittest
import uuid

from persona_runtime.world import WorldStore, WorldSession
from persona_runtime.world_lab import Laboratory
from test_world import Backend, event, choice, outcome


class NightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.backend = Backend(event(), choice(), outcome(), 'Synthetic review reply.')
        self.session = WorldSession(self.store, self.backend, {'garik': 'Synthetic'}, 'Synthetic', 64)
        self.lab = Laboratory(self.store, self.session)
        self.request = {'id': str(uuid.uuid4()), 'hours': 1, 'interval_minutes': 5,
                        'virtual_minutes': 15, 'max_calls': 12, 'speech_every': 0}
        # Make the first synthetic opportunity available deterministically.
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 6}, 0)
        self.original_prepare = self.store.prepare
        self.store.prepare = lambda actor_id='garik', minutes=30: self.original_prepare(actor_id, minutes, draw=0)

    def tearDown(self):
        if self.lab.worker:
            self.lab.worker.join(2)
        self.store.close()
        self.temp.cleanup()

    def tick(self, now=1000):
        self.lab.night.tick(now)
        if self.lab.worker:
            self.lab.worker.join(2)

    def test_full_cycle_waits_without_model_calls_between_episodes(self):
        self.lab.night.start(self.request, 1000)
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertEqual(self.store.get('night')['cycles_completed'], 1)
        for _ in range(20):
            self.tick(1200)
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertEqual(len(self.store.monitor()['cycles']), 1)
        self.tick(1300)
        self.assertEqual(len(self.store.monitor()['cycles']), 2)

    def test_deadline_stops_and_no_restart_on_later_ticks(self):
        self.lab.night.start(self.request, 1000)
        self.tick(4600)
        self.assertEqual(self.store.get('night')['status'], 'finished')
        self.assertTrue(self.store.get('paused'))
        self.tick(5000)
        self.assertFalse(self.backend.inputs)

    def test_call_cap_stops_after_one_cycle_with_no_duplicate_or_partial_new_cycle(self):
        self.lab.night.start({**self.request, 'max_calls': 3}, 1000)
        for _ in range(8):
            self.tick()
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertEqual(self.store.get('night')['status'], 'budget')
        self.assertEqual(self.store.get('night')['cycles_completed'], 1)
        self.assertEqual(len(self.store.monitor()['cycles']), 1)

    def test_model_error_stops_entire_night_without_automatic_retry(self):
        self.backend.answers = [RuntimeError('Synthetic private failure')]
        self.lab.night.start(self.request, 1000)
        for _ in range(6):
            self.tick()
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertEqual(self.store.get('night')['status'], 'error')
        self.assertEqual(self.store.monitor()['calls'][0]['usage']['input_tokens'], 20)

    def test_periodic_review_is_local_and_does_not_prevent_next_pause(self):
        self.lab.night.start({**self.request, 'speech_every': 1}, 1000)
        for _ in range(8):
            self.tick()
        self.assertEqual(len(self.backend.inputs), 4)
        self.assertEqual(self.backend.inputs[-1][0], 'speech')
        self.assertEqual(self.store.state()['history'][-1]['value']['draft'], True)
        self.assertEqual(self.store.get('night')['cycles_completed'], 1)

    def test_start_is_idempotent_and_stop_never_discards_history(self):
        first = self.lab.night.start(self.request, 1000)
        self.assertEqual(self.lab.night.start(self.request, 1100)['deadline'], first['deadline'])
        for _ in range(5):
            self.tick()
        self.lab.night.stop(now=1100)
        self.assertEqual(len(self.store.state()['history']), 3)
        self.tick(5000)
        self.assertEqual(len(self.backend.inputs), 3)

    def test_acceleration_respects_revisit_and_an_earlier_world_deadline(self):
        plan = {**self.request, 'interval_minutes': 1, 'virtual_minutes': 60}
        latest = {'value': {'results': {'choice': {'revisit_minutes': 15}}}}
        state = {'minute': 200, 'threads': []}
        self.assertEqual(self.lab.night.interval(plan, latest, state), (15, 15))
        state['threads'] = [{'status': 'waiting', 'due_minute': 203}]
        self.assertEqual(self.lab.night.interval(plan, latest, state), (3, 5))
        state['threads'][0]['status'] = 'closed'
        self.assertEqual(self.lab.night.interval(plan, latest, state), (15, 15))

    def test_selected_virtual_wait_is_persisted_and_used_for_next_episode(self):
        self.backend.answers = [event(), choice(revisit_minutes=10), outcome()]
        self.lab.night.start({**self.request, 'interval_minutes': 1, 'virtual_minutes': 60}, 1000)
        for _ in range(5):
            self.tick(1000)
        plan = self.store.get('night')
        self.assertEqual(plan['next_virtual_minutes'], 10)
        self.assertEqual(plan['next_due'], 1010)
        before = self.store.get('minute')
        self.tick(1009)
        self.assertEqual(len(self.store.monitor()['cycles']), 1)
        self.tick(1010)
        self.assertEqual(self.store.get('minute'), before+10)
        self.assertEqual(len(self.backend.inputs), 3)


if __name__ == '__main__':
    unittest.main()
