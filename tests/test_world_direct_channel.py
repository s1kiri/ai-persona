"""A voluntary expression is delivered verbatim, independently of world facts."""
import json
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from test_world import Backend, choice, outcome, event
from test_world_personal import experience, quiet


class DirectChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)
        self.store.set_settings({**self.store.get('settings'), 'post_cooldown_minutes': 0}, 0)
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'garik': 'PERSONALITY'}, 'Speech',
                                    personal_life=True, autonomous_posts=True, direct_channel=True,
                                    simulated_audience=True, max_calls=100)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def before_channel(self):
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers += ['MY-RAW-IMPULSE', choice(action='MY-RAW-IMPULSE'), event(pattern=cid),
                                 outcome(), experience(cid+'-outcome', note='MY-RAW-AFTER')]
        for _ in range(5): self.session.step(cid)
        return cid

    def test_single_word_needs_no_event_and_is_not_rewritten(self):
        cid = self.before_channel()
        self.backend.answers = [{'decision': 'post', 'text': 'блять', 'source_ids': []}]
        self.session.step(cid)
        self.assertEqual(self.store.cycle(cid)['status'], 'complete')
        self.assertEqual([v[0] for v in self.backend.inputs],
                         ['impulse', 'choice', 'world', 'outcome', 'experience', 'channel'])
        post = self.store.monitor()['posts'][0]
        self.assertEqual(post['value']['text'], 'блять')
        self.assertEqual(post['value']['source_ids'], [])
        self.assertTrue(post['value']['direct'])
        self.assertEqual(len(self.store.monitor()['calls']), 6)
        call = self.store.monitor()['calls'][0]
        self.assertFalse(self.store.complete(call['id'], {'decision': 'post', 'text': 'другой текст', 'source_ids': []}, {}))
        self.assertEqual(len(self.store.monitor()['posts']), 1)
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'], 'блять')
        voice = self.backend.inputs[-1][2]['personal_voice']
        self.assertEqual(voice, {'before_act': 'MY-RAW-IMPULSE', 'after_act': 'MY-RAW-AFTER'})
        self.assertNotIn('Synthetic backstage', json.dumps(self.backend.inputs[-1][2]))
        with self.store.lock, self.store.db: self.store.register_actor('other', 'Other')
        self.assertNotIn('MY-RAW-AFTER', json.dumps(self.store.actor_view('other')))
        self.assertNotIn(post['id'], self.store.public_evidence_ids('other'))
        self.assertIn(post['id'], self.store.public_evidence_ids('garik'))

    def test_free_expression_does_not_patch_world_and_keeps_format(self):
        cid = self.before_channel()
        before = self.store.state()
        text = 'мечта: купил себе Луну\n\nблять\n  такую маленькую'
        self.backend.answers = [{'decision': 'post', 'text': text, 'source_ids': []}]
        self.session.step(cid)
        after = self.store.state()
        for field in ('entities', 'threads', 'minute'):
            self.assertEqual(before[field], after[field])
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'], text)
        self.assertEqual(self.store.channel_context('garik')[-1]['value']['text'], text)

    def test_keep_does_not_create_a_post(self):
        cid = self.before_channel()
        self.backend.answers = [{'decision': 'keep', 'text': '', 'source_ids': []}]
        self.session.step(cid)
        self.assertEqual(self.store.monitor()['posts'], [])
        self.assertEqual(self.store.cycle(cid)['status'], 'complete')

    def test_private_or_invented_source_is_still_rejected(self):
        for source in ('-experience', '-invented'):
            with self.subTest(source=source):
                cid = self.before_channel()
                self.backend.answers = [{'decision': 'post', 'text': 'мысль', 'source_ids': [cid+source]}]
                with self.assertRaises(ValueError): self.session.step(cid)
                self.assertEqual(self.store.monitor()['posts'], [])
                self.assertEqual(self.store.cycle(cid)['status'], 'blocked')
                self.store.abandon(cid)

    def test_direct_reply_targets_existing_comments(self):
        cid = self.before_channel()
        self.backend.answers = [{'decision': 'post', 'text': 'первая мысль', 'source_ids': []}]
        self.session.step(cid)
        root = cid+'-post'
        cid = self.session.prepare(draw=0)['id']
        self.backend.answers = [
            {'post_id': root, 'comments': [{'speaker': 'Reader', 'text': 'чего?'}]},
            'MY-RAW-IMPULSE', choice(action='MY-RAW-IMPULSE'), quiet(), outcome(),
            experience(cid+'-outcome'),
            {'decision': 'reply', 'text': 'да ниче)))', 'source_ids': [cid+'-audience']}]
        for _ in range(7): self.session.step(cid)
        reply = self.store.monitor()['posts'][0]['value']
        self.assertEqual(reply['text'], 'да ниче)))')
        self.assertEqual(reply['reply_to'], root)
        self.assertEqual(reply['surface'], 'comment_reply')
        self.assertEqual(self.store.cycle(cid)['status'], 'complete')

    def test_reply_without_comments_stops_instead_of_inventing_them(self):
        cid = self.before_channel()
        self.backend.answers = [{'decision': 'reply', 'text': 'кому?', 'source_ids': []}]
        with self.assertRaises(ValueError): self.session.step(cid)
        self.assertEqual(self.store.monitor()['posts'], [])
