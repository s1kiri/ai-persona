"""Renewed stories preserve the original ending and ordinary world limits."""
import copy
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from test_world import Backend, event, choice, outcome


class ThreadRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)
        self.backend = Backend(event(), choice(), outcome(threads=[{
            **event()['threads'][0], 'status': 'closed', 'due_minute': None,
            'situation': 'The original visit ended.', 'open_question': ''}]))
        self.session = WorldSession(self.store, self.backend, {'garik': 'Synthetic'}, 'Speech')
        c = self.session.prepare(draw=0)
        for _ in range(3): self.session.step(c['id'])

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_reused_closed_identity_becomes_linked_child_and_preserves_ending(self):
        previous = copy.deepcopy(self.store.state()['threads'][0])
        c = self.session.prepare(draw=0)
        self.backend.answers = [event(pattern='a-new-matter', threads=[{
            **event()['threads'][0], 'due_minute': None, 'situation': 'A later matter is open.'}])]
        self.session.step(c['id'])
        state = self.store.state()
        self.assertEqual(next(t for t in state['threads'] if t['id']=='workshop'), previous)
        child = next(t for t in state['threads'] if t['id']!='workshop')
        self.assertEqual(child['status'], 'open')
        self.assertEqual(state['thread_links'][0]['parent'], 'workshop')
        self.assertEqual(self.store.cycle(c['id'])['value']['results']['world']['thread_id'], child['id'])
        self.assertEqual(self.store.cycle(c['id'])['value']['thread_routing']['world']['original']['thread_id'], 'workshop')
        with self.store.lock, self.store.db: self.store.register_actor('other', 'Other')
        self.assertEqual(self.store.actor_view('other')['thread_links'], [])

    def test_branching_cannot_evade_new_event_gate_or_overwrite_on_failure(self):
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        c = self.session.prepare(draw=1)
        self.backend.answers = [event(beat='progress', pattern='a-new-matter', threads=[{
            **event()['threads'][0], 'due_minute': None}])]
        with self.assertRaises(ValueError): self.session.step(c['id'])
        self.assertEqual(len(self.store.state()['threads']), 1)
        self.assertEqual(self.store.state()['threads'][0]['status'], 'closed')
        self.assertEqual(self.store.state()['thread_links'], [])

    def test_duplicate_closed_identity_is_rejected_before_routing(self):
        c = self.session.prepare(draw=0)
        thread = {**event()['threads'][0], 'due_minute': None}
        self.backend.answers = [event(pattern='duplicate', threads=[thread, thread])]
        with self.assertRaises(ValueError): self.session.step(c['id'])
        self.assertEqual(len(self.store.state()['threads']), 1)
        self.assertEqual(self.store.state()['thread_links'], [])

