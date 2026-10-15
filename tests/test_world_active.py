"""A latent environment reaches perception before a free character choice."""
import copy
import json
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from persona_runtime.world_protocol import validate_result
from test_world import Backend, event, choice, outcome
from test_world_personal import experience, quiet


def hook(identity='market-opening', anchor='market', minute=30):
    return {'id': identity, 'anchor_ids': [anchor], 'actor_ids': ['garik'], 'domain': 'city',
            'opens_minute': minute, 'closes_minute': minute+120,
            'route': 'A public fictional announcement visible from home.',
            'npc_want': 'SELLER-WANTS-TO-SELL', 'friction': 'SELLER-REFUSES-TO-ADMIT-A-MISTAKE',
            'temptation': 'An optional visit; no actor agreement yet.'}


class ActiveWorldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)
        self.market = {'id': 'market', 'kind': 'place', 'name': 'Market', 'description': 'A fictional place.',
                       'condition': 'Open.', 'known_to': [], 'world_note': 'SECRET-MARKET-PLAN'}
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO world_entities VALUES(?,?,?)', ('market', json.dumps(self.market), 'fixture'))
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'garik': 'SECRET-PERSONA'}, 'Speech',
                                    personal_life=True, active_world=True)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def exposed(self):
        return event(domain='city', entity_ids=['market'], entities=[{**self.market, 'known_to': ['garik']}])

    def test_planned_opening_reaches_actor_before_choosing_without_revealing_backstage(self):
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'hooks': [hook()]}, self.exposed(), 'I choose to visit.',
                                choice(action='I choose to visit.', entity_ids=['market']), outcome(),
                                experience(cid+'-outcome')]
        for _ in range(6): self.session.step(cid)
        self.assertEqual([i[0] for i in self.backend.inputs], ['horizon','world','impulse','choice','outcome','experience'])
        wake = self.backend.inputs[2][2]
        self.assertIn('market', [e['id'] for e in wake['life']['entities']])
        self.assertEqual(wake['moment'], self.exposed()['observation'])
        for secret in ('SECRET-MARKET-PLAN', 'SELLER-WANTS-TO-SELL', 'SELLER-REFUSES-TO-ADMIT-A-MISTAKE'):
            self.assertNotIn(secret, json.dumps(wake))
        self.assertNotIn('SECRET-PERSONA', json.dumps(self.backend.inputs[0]))
        self.assertEqual(self.store.state()['hooks'][0]['exposed_to'], ['garik'])

    def test_ignored_hook_is_not_marked_seen_and_cannot_masquerade_as_quiet(self):
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'hooks': [hook()]}, quiet()]
        self.session.step(cid)
        with self.assertRaises(ValueError): self.session.step(cid)
        self.assertEqual(self.store.state()['hooks'][0]['exposed_to'], [])
        self.assertNotIn('market', [e['id'] for e in self.store.actor_view('garik')['entities']])

    def test_frequency_zero_prevents_planning_and_exposure(self):
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        c = self.session.prepare(draw=0)
        self.assertEqual(c['phase'], 'world')
        self.backend.answers = [quiet(), 'Rest.', choice(action='Rest.'), outcome(), experience(c['id']+'-outcome')]
        for _ in range(5): self.session.step(c['id'])
        self.assertEqual(self.backend.inputs[0][0], 'world')
        self.assertNotIn('exposure', self.backend.inputs[0][2]['director'])

    def test_unseen_anchors_are_prioritized_and_plans_wait_between_event_slots(self):
        first = hook('known-opening', 'home')
        second = hook('unknown-opening')
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'hooks': [first, second]}, self.exposed(), 'Rest.', choice(action='Rest.'),
                                outcome(), experience(cid+'-outcome')]
        for _ in range(6): self.session.step(cid)
        self.assertEqual(self.backend.inputs[1][2]['director']['exposure']['id'], 'unknown-opening')
        next_cycle = self.session.prepare(draw=1)
        self.assertEqual(next_cycle['phase'], 'world')  # No regeneration while a plan remains.
        self.backend.answers = [quiet()]
        self.session.step(next_cycle['id'])
        self.assertNotIn('exposure', self.backend.inputs[-1][2]['director'])
        self.assertEqual(self.store.state()['hooks'][0]['exposed_to'], [])

    def test_unknown_plan_anchors_do_not_become_world_facts(self):
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'hooks': [hook(anchor='invented-entity')]}]
        with self.assertRaises(ValueError): self.session.step(cid)
        self.assertEqual(self.store.state()['hooks'], [])


class PersonalRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name); self.store.pause(False)
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'garik': 'Synthetic'}, 'Speech', personal_life=True)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def episode(self, previous=None):
        cid = self.session.prepare(draw=1)['id']
        note = experience(cid+'-outcome')
        if previous:
            note['concerns'][0].update(status='closed', source_ids=[previous+'-experience', cid+'-outcome'])
        self.backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(), note]
        return cid

    def test_review_saved_note_once_without_repeating_model_action_or_rewriting_failed_usage(self):
        first = self.episode()
        for _ in range(5): self.session.step(first)
        second = self.episode(first)
        for _ in range(4): self.session.step(second)
        evidence = self.store.personal_evidence_ids
        self.store.personal_evidence_ids = lambda actor: [i for i in evidence(actor) if i != first+'-experience']
        with self.assertRaises(ValueError): self.session.step(second)
        self.store.personal_evidence_ids = evidence
        before = self.store.monitor(); minute = before['minute']; count = len(self.backend.inputs)
        self.store.recover_experience(second)
        self.store.recover_experience(second)
        after = self.store.monitor()
        self.assertEqual(after['minute'], minute)
        self.assertEqual(len(self.backend.inputs), count)
        self.assertEqual(after['call_count'], before['call_count'])
        self.assertEqual(after['calls'][0], before['calls'][0])
        self.assertEqual(after['calls'][0]['status'], 'failed')
        self.assertEqual(len(after['repairs']), 1)
        self.assertEqual(self.store.actor_view('garik')['concerns'][0]['status'], 'closed')
        self.assertIn('rejected', self.store.cycle(second)['value'])

    def test_own_journal_is_only_private_evidence_and_cannot_source_a_public_post(self):
        cid = self.episode()
        for _ in range(5): self.session.step(cid)
        self.assertIn(cid+'-experience', self.store.personal_evidence_ids('garik'))
        with self.store.lock, self.store.db: self.store.register_actor('other', 'Other')
        self.assertNotIn(cid+'-experience', self.store.personal_evidence_ids('other'))
        self.assertNotIn(cid+'-experience', self.store.public_evidence_ids('garik'))
        proposal = {'decision': 'post', 'focus': 'Private note is not a world fact.', 'source_ids': [cid+'-experience']}
        with self.assertRaises(ValueError):
            validate_result('share', proposal, self.store.state(), self.store.cycle(cid)['value']['brief'])

    def test_review_does_not_accept_an_unknown_source(self):
        cid = self.episode('invented-cycle')
        for _ in range(4): self.session.step(cid)
        with self.assertRaises(ValueError): self.session.step(cid)
        with self.assertRaises(ValueError): self.store.recover_experience(cid)
        self.assertEqual(self.store.cycle(cid)['status'], 'blocked')
        self.assertEqual(self.store.monitor()['repairs'], [])
