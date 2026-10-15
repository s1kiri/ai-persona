"""Synthetic world mechanics only; no model, credentials or working database."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession, WorldConflict, director, DEFAULT_SETTINGS
from persona_runtime.world_protocol import validate_result


def event(**changes):
    value = {'beat': 'opportunity', 'domain': 'leisure', 'pattern': 'shared-workshop',
             'thread_id': 'workshop', 'causes': [], 'entity_ids': ['home'],
             'observation': 'Synthetic invitation to a workshop.', 'world_note': 'Synthetic backstage detail.',
             'visible_to': ['garik'], 'surprise': 10, 'entities': [],
             'threads': [{'id': 'workshop', 'title': 'Workshop', 'domain': 'leisure',
                          'status': 'open', 'situation': 'An invitation is available.',
                          'open_question': 'Will he go?', 'due_minute': 100,
                          'entity_ids': ['home'], 'visible_to': ['garik']}]}
    value.update(changes)
    return value


def choice(**changes):
    value = {'desire': 'Try something new.', 'kind': 'act', 'action': 'Go to the workshop.',
             'entity_ids': ['home'], 'note': 'Synthetic personal diary note.', 'reaction': '', 'undertakings': [], 'revisit_minutes': 60}
    value.update(changes)
    return value


def outcome(**changes):
    value = {'status': 'succeeded', 'observation': 'He reached the workshop.',
             'world_note': '', 'elapsed_minutes': 15, 'entities': [], 'threads': []}
    value.update(changes)
    value.setdefault('time_costs', [{'activity': 'Synthetic activity', 'minutes': value['elapsed_minutes']}])
    return value


class Backend:
    model = 'synthetic'
    def __init__(self, *answers):
        self.answers = list(answers)
        self.inputs = []
        self.last_usage = {}

    def generate(self, stage, instructions, payload):
        self.inputs.append((stage, instructions, copy.deepcopy(payload)))
        self.last_usage = {'input_tokens': 20, 'output_tokens': 5}
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, self.last_usage


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = WorldStore(self.path)
        self.store.pause(False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, *answers, max_calls=24):
        backend = Backend(*answers)
        return WorldSession(self.store, backend, {'garik': 'Synthetic personality'}, 'Synthetic speech contract', max_calls), backend

    def prepare(self):
        return self.store.prepare(minutes=30, draw=0)

    def test_full_cycle_separate_roles_speech_and_persistent_receipts(self):
        cycle = self.prepare()
        session, backend = self.session(event(), choice(), outcome(), 'Synthetic spoken text.')
        for phase in ('world', 'choice', 'outcome'):
            self.assertEqual(self.store.cycle(cycle['id'])['phase'], phase)
            session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'complete')
        self.assertEqual(self.store.state()['minute'], 45)
        self.assertEqual(len(self.store.monitor()['notes']), 1)
        self.assertNotIn('Synthetic backstage detail.', json.dumps(backend.inputs[1][2]))
        self.assertNotIn('Synthetic personal diary note.', json.dumps(backend.inputs[2][2]))
        self.store.request_speech(cycle['id'], 'private', 'How are you?')
        session.step(cycle['id'])
        self.assertNotIn('Сейчас ты выбираешь', backend.inputs[-1][1])
        speech = self.store.state()['history'][-1]['value']
        self.assertEqual(speech, {'text': 'Synthetic spoken text.', 'draft': True})
        self.store.close()
        self.store = WorldStore(self.path)
        self.assertEqual(len(self.store.state()['history']), 4)
        self.assertEqual(len(self.store.monitor()['notes']), 1)
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'complete')

    def test_repeated_completion_is_idempotent(self):
        cycle = self.prepare()
        call, _ = self.store.claim(cycle['id'], 24)
        self.assertTrue(self.store.complete(call, event(), {'input_tokens': 20}))
        self.assertFalse(self.store.complete(call, event(), {'input_tokens': 20}))
        self.assertEqual(len(self.store.state()['history']), 1)

    def test_interruption_blocks_replay_and_preserves_prior_world(self):
        cycle = self.prepare()
        call, _ = self.store.claim(cycle['id'], 24)
        self.store.close()
        self.store = WorldStore(self.path)
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'blocked')
        self.assertEqual(self.store.monitor()['calls'][0]['status'], 'interrupted')
        with self.assertRaises(WorldConflict):
            self.store.claim(cycle['id'], 24)
        with self.assertRaises(WorldConflict):
            self.store.complete(call, event(), {})
        self.assertFalse(self.store.state()['history'])
        self.store.abandon(cycle['id'])
        self.store.prepare(draw=0)

    def test_second_owner_does_not_mark_first_call_interrupted(self):
        cycle = self.prepare()
        self.store.claim(cycle['id'], 24)
        with self.assertRaises(WorldConflict):
            WorldStore(self.path)
        self.assertEqual(self.store.monitor()['calls'][0]['status'], 'started')

    def test_invalid_patch_commits_nothing_and_failure_keeps_usage(self):
        cycle = self.prepare()
        invalid = event(threads=[{**event()['threads'][0], 'entity_ids': ['missing']}])
        session, backend = self.session(invalid)
        with self.assertRaises(ValueError):
            session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'blocked')
        self.assertFalse(self.store.state()['threads'])
        self.assertFalse(self.store.state()['history'])
        self.assertEqual(self.store.monitor()['calls'][0]['usage']['input_tokens'], 20)
        self.assertEqual(len(backend.inputs), 1)
        self.assertEqual(self.store.cycle(cycle['id'])['value']['rejected']['answer'], invalid)
        self.assertNotIn('rejected', json.dumps(self.store.actor_view('garik')))

    def test_recent_pattern_and_unknown_causal_receipt_rejected(self):
        cycle = self.prepare()
        session, _ = self.session(event(), choice(), outcome())
        for _ in range(3):
            session.step(cycle['id'])
        following = self.prepare()
        brief = following['value']['brief']
        with self.assertRaises(ValueError):
            validate_result('world', event(), self.store.state(), brief)
        with self.assertRaises(ValueError):
            validate_result('world', event(beat='return', causes=['invented']), self.store.state(), brief)
        validate_result('world', event(beat='return', causes=[cycle['id']+'-world']), self.store.state(), brief)

    def test_unknown_context_actor_does_not_learn_other_private_information(self):
        with self.store.db:
            self.store.register_actor('guest', 'Synthetic future actor')
        e = {'id': 'hidden', 'kind': 'thing', 'name': 'Guest thing', 'description': 'Guest private object',
             'condition': 'Secret condition', 'known_to': ['guest'], 'world_note': 'Never put into an actor view'}
        with self.store.db:
            self.store.db.execute('INSERT INTO world_entities VALUES(?,?,?)', ('hidden', json.dumps(e), 'synthetic'))
        guest = self.store.actor_view('guest')
        garik = self.store.actor_view('garik')
        self.assertNotIn('Secret condition', json.dumps(garik))
        self.assertNotIn('guest', [a['id'] for a in garik['actors']])
        self.assertIn('Secret condition', json.dumps(guest))
        self.assertNotIn('Never put into an actor view', json.dumps(guest))
        with self.assertRaises(WorldConflict):
            self.store.register_actor('hidden', 'Do not promote an NPC into a persona')
        cycle = self.prepare()
        with self.assertRaises(ValueError):
            validate_result('choice', choice(entity_ids=['hidden']), self.store.state(), cycle['value']['brief'])

    def test_two_actors_share_events_but_not_diary_or_private_choices(self):
        with self.store.db:
            self.store.register_actor('guest', 'Synthetic future actor')
        cycle = self.prepare()
        home = next(e for e in self.store.state()['entities'] if e['id'] == 'home')
        home['known_to'].append('guest')
        session, _ = self.session(event(visible_to=['garik', 'guest'], entities=[home]), choice(), outcome())
        for _ in range(3):
            session.step(cycle['id'])
        guest = json.dumps(self.store.actor_view('guest'))
        self.assertIn('Synthetic invitation', guest)
        self.assertNotIn('Synthetic personal diary note', guest)
        self.assertNotIn('Try something new', guest)
        self.assertNotIn('He reached the workshop', guest)

    def test_zero_event_rate_still_allows_due_consequences_and_own_action(self):
        settings = {**DEFAULT_SETTINGS, 'events_per_hour': 0}
        self.store.set_settings(settings, 0)
        cycle = self.store.prepare(minutes=30, draw=0)
        brief = cycle['value']['brief']
        self.assertFalse(brief['new_event_allowed'])
        self.assertNotIn('opportunity', brief['allowed_beats'])
        self.assertNotIn('consequence', brief['allowed_beats'])  # Nothing has happened yet.
        quiet = event(beat='quiet', pattern='quiet', thread_id=None, threads=[], surprise=0)
        session, _ = self.session(quiet, choice(), outcome())
        for _ in range(3):
            session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['value']['results']['choice']['kind'], 'act')
        following = self.store.prepare(draw=0)
        self.assertIn('consequence', following['value']['brief']['allowed_beats'])

    def test_pacing_does_not_allow_new_entities_disguised_as_progress(self):
        self.store.set_settings({**DEFAULT_SETTINGS, 'events_per_hour': 0}, 0)
        cycle = self.prepare()
        with self.assertRaises(ValueError):
            validate_result('world', event(beat='progress'), self.store.state(), cycle['value']['brief'])

    def test_controls_conflict_and_probability_depend_on_virtual_interval(self):
        self.store.set_settings({**DEFAULT_SETTINGS, 'events_per_hour': 1}, 0)
        with self.assertRaises(WorldConflict):
            self.store.set_settings(DEFAULT_SETTINGS, 0)
        with self.assertRaises(ValueError):
            self.store.set_settings({**DEFAULT_SETTINGS, 'events_per_hour': float('nan')}, 1)
        state = self.store.state()
        self.assertGreater(director(state, 'garik', 60, .5)['new_event_probability'], director(state, 'garik', 15, .5)['new_event_probability'])
        self.assertEqual(self.store.get('settings')['events_per_hour'], 1)

    def test_long_quiet_streak_yields_only_when_a_new_arrival_is_available(self):
        state = self.store.state()
        state['settings'] = {**DEFAULT_SETTINGS, 'quiet': 10}
        state['history'] = [{'id': 'quiet-'+str(i), 'stage': 'world',
                             'value': {'beat': 'quiet'}} for i in range(3)]
        self.assertNotIn('quiet', director(state, 'garik', 60, 0)['allowed_beats'])
        self.assertIn('quiet', director(state, 'garik', 60, 1)['allowed_beats'])
        state['settings']['events_per_hour'] = 0
        self.assertIn('quiet', director(state, 'garik', 60, 0)['allowed_beats'])
        state['settings'].update(events_per_hour=6, quiet=100)
        self.assertIn('quiet', director(state, 'garik', 60, 0)['allowed_beats'])

    def test_previous_speech_is_returned_only_to_its_original_surface(self):
        captured = []
        for index, (surface, text) in enumerate([
                ('private', 'Synthetic private detail.'),
                ('channel_draft', 'Synthetic public draft.'),
                ('private', 'Synthetic next private reply.')]):
            cycle = self.prepare()
            scene = event(pattern='test-pattern-'+str(index))
            scene['threads'][0]['due_minute'] = None
            session, backend = self.session(scene, choice(), outcome(), text)
            for _ in range(3):
                session.step(cycle['id'])
            self.store.request_speech(cycle['id'], surface, 'Synthetic question.')
            session.step(cycle['id'])
            captured.append(backend.inputs[-1][2]['recent_spoken'])
        self.assertEqual(captured, [[], [], ['Synthetic private detail.']])

    def test_open_story_cap_and_closed_story_are_not_overwritten(self):
        cycle = self.prepare()
        self.store.set_settings({**DEFAULT_SETTINGS, 'max_threads': 1}, 0)
        brief = {**cycle['value']['brief'], 'settings': self.store.get('settings')}
        duplicate = {**event()['threads'][0], 'id': 'another'}
        with self.assertRaises(ValueError):
            validate_result('world', event(threads=event()['threads']+[duplicate]), self.store.state(), brief)
        state = self.store.state()
        state['threads'] = [{**event()['threads'][0], 'status': 'closed', 'due_minute': None}]
        with self.assertRaises(ValueError):
            validate_result('world', event(), state, brief)

    def test_pause_budget_and_overlapping_cycles_cannot_dispatch(self):
        cycle = self.prepare()
        with self.assertRaises(WorldConflict):
            self.prepare()
        self.store.pause(True)
        with self.assertRaises(WorldConflict):
            self.store.claim(cycle['id'], 1)
        self.store.pause(False)
        session, backend = self.session(event(), choice(), max_calls=1)
        session.step(cycle['id'])
        with self.assertRaises(WorldConflict):
            session.step(cycle['id'])
        self.assertEqual(len(backend.inputs), 1)

    def test_refuses_production_directory_before_opening_world_database(self):
        directory = self.path / 'production'
        directory.mkdir()
        (directory / 'runtime.sqlite3').write_text('synthetic working marker')
        with self.assertRaises(ValueError):
            WorldStore(directory)
        self.assertFalse((directory / 'world.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
