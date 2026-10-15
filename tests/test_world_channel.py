"""Local channel interaction is evidence of utterances, never real publication."""
import json
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from test_world import Backend, choice, outcome, event
from test_world_personal import quiet, experience


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'garik': 'SECRET-PERSONA'}, 'Speech',
                                    personal_life=True, autonomous_posts=True, simulated_audience=True,
                                    max_calls=100)
        self.store.set_settings({**self.store.get('settings'), 'post_cooldown_minutes': 0}, 0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def root(self):
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = ['Try it.', choice(action='Try it.', note='SECRET-DIARY'),
                                event(), outcome(status='pending'), experience(cid+'-outcome'),
                                {'decision': 'post', 'focus': 'Not finished yet.', 'source_ids': [cid+'-outcome']},
                                'PUBLIC-FIRST-FRAGMENT']
        for _ in range(7): self.session.step(cid)
        return cid+'-post'

    def test_unfinished_story_can_be_posted_then_comments_precede_next_choice(self):
        post = self.root()
        self.assertEqual(self.store.state()['threads'][0]['status'], 'open')
        cid = self.session.prepare(draw=0)['id']
        self.assertEqual(self.store.cycle(cid)['phase'], 'audience')
        self.backend.answers = [{'post_id': post, 'comments': [{'speaker': 'Reader', 'text': 'Try another idea.'}]},
                                'I will try that idea.']
        self.session.step(cid)
        audience = self.backend.inputs[-1]
        self.assertEqual(audience[0], 'audience')
        text = json.dumps(audience)
        for secret in ('SECRET-PERSONA', 'SECRET-DIARY', 'MY-PRIVATE-CONCERN', 'Synthetic backstage'):
            self.assertNotIn(secret, text)
        self.session.step(cid)
        wake = self.backend.inputs[-1][2]['life']
        self.assertIn('Try another idea.', json.dumps(wake['channel_context']))
        self.assertTrue(self.store.monitor()['audience'][0]['value']['simulated'])

    def test_reply_is_attached_to_original_post_and_not_delivered_as_new_root(self):
        post = self.root()
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'post_id': post, 'comments': [{'speaker': 'Reader', 'text': 'An unverified claim.'}]},
                                'Do another thing.', choice(action='Do another thing.'), quiet(), outcome(),
                                experience(cid+'-audience'),
                                {'decision': 'reply', 'focus': 'Disagree with Reader.', 'source_ids': [cid+'-audience']},
                                'PUBLIC-REPLY']
        for _ in range(8): self.session.step(cid)
        reply = self.store.monitor()['posts'][0]
        self.assertEqual(reply['value']['reply_to'], post)
        self.assertEqual(self.backend.inputs[-1][2]['surface'], 'comment_reply')
        self.assertIsNone(self.store.pending_audience_post('garik'))
        self.assertEqual(self.session.prepare(draw=0)['phase'], 'impulse')

    def test_foreign_or_invented_target_stops_delivery_without_retry(self):
        self.root()
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'post_id': 'invented-post', 'comments': []}]
        with self.assertRaises(ValueError): self.session.step(cid)
        self.assertEqual(self.store.cycle(cid)['status'], 'blocked')
        self.assertEqual(self.store.monitor()['audience'], [])

    def test_empty_comments_are_delivered_once_and_cannot_be_replied_to(self):
        post = self.root()
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [{'post_id': post, 'comments': []},
                                'Do another thing.', choice(action='Do another thing.'), quiet(), outcome(),
                                experience(cid+'-outcome'),
                                {'decision': 'reply', 'focus': 'Imagined reply.', 'source_ids': [cid+'-audience']}]
        for _ in range(6): self.session.step(cid)
        with self.assertRaises(ValueError): self.session.step(cid)
        self.assertIsNone(self.store.pending_audience_post('garik'))

    def test_two_threads_can_alternate_posts_and_old_evidence_survives_window(self):
        self.store.set_settings({**self.store.get('settings'), 'quiet': 100}, 1)
        post = self.root()
        first_source = post.removesuffix('-post')+'-outcome'
        for index in range(7):
            cid = self.session.prepare(draw=0)['id']
            self.backend.answers = ([{'post_id': post, 'comments': []}]
                                    if self.store.cycle(cid)['phase'] == 'audience' else [])
            world = event(pattern='another-story', thread_id='other-story', threads=[{
                'id': 'other-story', 'title': 'Other', 'domain': 'people', 'status': 'waiting',
                'situation': 'Waiting while another thing happens.', 'open_question': 'What next?',
                'due_minute': None, 'entity_ids': ['home'], 'visible_to': ['garik']}]) if index == 0 else quiet()
            share = {'decision': 'post', 'focus': 'Return to the first unfinished story.', 'source_ids': [first_source]} if index == 6 else {'decision': 'keep', 'focus': '', 'source_ids': []}
            self.backend.answers += ['Try it.', choice(action='Try it.'), world, outcome(),
                                     experience(cid+'-outcome', concerns=[]), share]
            if index == 6: self.backend.answers += ['PUBLIC-NEXT-FRAGMENT']
            while self.store.cycle(cid)['status'] == 'ready': self.session.step(cid)
        self.assertEqual(len(self.store.state()['threads']), 2)
        self.assertTrue(all(t['status'] != 'closed' for t in self.store.state()['threads']))
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'], 'PUBLIC-NEXT-FRAGMENT')
        with self.store.lock, self.store.db: self.store.register_actor('other', 'Other')
        self.assertEqual(self.store.actor_view('other')['channel_context'], [])
        self.assertNotIn(first_source, self.store.public_evidence_ids('other'))

