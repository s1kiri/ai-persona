"""Optional photo briefs preserve knowledge and never fabricate delivery."""
import copy
import json
import unittest

from persona_runtime.world_photo import photo_context, render_request


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.entities = [
            {'id': 'map', 'kind': 'thing', 'name': 'План 1988 года',
             'description': 'Складной план, магазин не отмечен.',
             'condition': 'Развёрнут на столе, сгиб надорван.',
             'known_to': ['garik'], 'world_note': 'PRIVATE-SUBJECT'},
            {'id': 'hidden', 'kind': 'thing', 'name': 'SECRET-OBJECT',
             'description': 'SECRET', 'condition': 'SECRET',
             'known_to': ['other'], 'world_note': 'SECRET'}]
        self.events = [
            {'id': 'outcome', 'stage': 'outcome', 'actor_id': 'garik', 'minute': 42,
             'value': {'observation': 'План уже лежит на столе.', 'world_note': 'PRIVATE-WORLD'}},
            {'id': 'diary', 'stage': 'experience', 'actor_id': 'other', 'minute': 43,
             'value': {'note': 'FOREIGN-DIARY'}},
            {'id': 'unseen', 'stage': 'world', 'actor_id': 'other', 'minute': 44,
             'value': {'observation': 'SECRET-EVENT', 'visible_to': ['other']}}]
        self.context = photo_context('garik', self.entities, self.events, 'MY-WRITTEN-REACTION')
        self.choice = {'decision': 'show', 'reason': 'Хочу показать разницу.',
                       'subject_ids': ['map'], 'source_ids': ['outcome'],
                       'framing': 'Крупно на столе, сверху чуть наискосок.', 'caption': 'вот эта штука'}

    def test_context_has_only_observable_subjects_and_own_written_reaction(self):
        text = json.dumps(self.context)
        for secret in ('PRIVATE-SUBJECT', 'SECRET', 'PRIVATE-WORLD', 'FOREIGN-DIARY'):
            self.assertNotIn(secret, text)
        self.assertEqual(self.context['personal_voice'], 'MY-WRITTEN-REACTION')
        self.assertEqual([s['id'] for s in self.context['sources']], ['outcome'])

    def test_skip_produces_no_render_request(self):
        choice = {'decision': 'skip', 'reason': 'Не хочется.', 'subject_ids': [],
                  'source_ids': [], 'framing': '', 'caption': ''}
        self.assertIsNone(render_request(choice, self.context))

    def test_request_preserves_subject_condition_and_is_neither_generated_nor_published(self):
        before = copy.deepcopy(self.context)
        job = render_request(self.choice, self.context)
        self.assertIn('сгиб надорван', job['prompt'])
        self.assertIn('1988', job['prompt'])
        self.assertIn('магазин не отмечен', job['prompt'])
        self.assertNotIn('MY-WRITTEN-REACTION', job['prompt'])
        self.assertNotIn(self.choice['caption'], job['prompt'])
        self.assertEqual(job['caption'], self.choice['caption'])
        self.assertEqual(job['status'], 'requested')
        self.assertFalse(job['generated'])
        self.assertFalse(job['published'])
        self.assertEqual(self.context, before)
        self.assertIn('several-years-old ordinary smartphone',job['prompt'])
        self.assertIn('supported by the recorded observation',job['prompt'])
        self.assertNotIn('keep faces,',job['prompt'])

    def test_invented_foreign_or_private_sources_cannot_render(self):
        for field, value in (('subject_ids', ['hidden']), ('source_ids', ['diary']),
                             ('source_ids', ['unseen']), ('source_ids', ['invented'])):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    render_request({**self.choice, field: value}, self.context)

    def test_future_or_missing_subject_has_no_receipt(self):
        empty = photo_context('garik', [], [], '')
        with self.assertRaises(ValueError): render_request(self.choice, empty)
        with self.assertRaises(ValueError): render_request({**self.choice, 'source_ids': []}, self.context)

    def test_skip_cannot_smuggle_a_request(self):
        with self.assertRaises(ValueError): render_request({**self.choice, 'decision': 'skip'}, self.context)
