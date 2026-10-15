"""Personal continuity, independent wake and real-search boundaries, offline."""
import json
import tempfile
import threading
import unittest

from persona_runtime.world import WorldStore, WorldSession
from test_world import Backend, event, choice, outcome


def experience(source, **changes):
    value = {'note': 'MY-EXPERIENCE', 'director_signal': 'A brief current interest.',
             'concerns': [{'id': 'curiosity', 'text': 'MY-PRIVATE-CONCERN',
                           'stake': 'MY-PRIVATE-STAKE', 'next_hint': 'Learn more.',
                           'status': 'active', 'source_ids': [source]}]}
    value.update(changes)
    return value


def quiet():
    return event(beat='quiet', pattern='quiet', thread_id=None, threads=[], entities=[],
                 surprise=0, observation='A quiet interval.', world_note='')


def research(cid):
    return {'task_id': cid, 'facts': [{'url': 'https://example.org/evidence',
                                      'title': 'Synthetic source', 'claim': 'Synthetic fact.'}],
            'caveats': [], 'tool_events': [{'id': 'web1', 'status': 'completed', 'action': {}}]}


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.store.pause(False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, *answers, **flags):
        backend = Backend(*answers)
        session = WorldSession(self.store, backend, {'garik': 'MY-PERSONA'}, 'Speech',
                               personal_life=True, **flags)
        return session, backend

    def test_own_choice_precedes_event_and_experience_survives_restart(self):
        session, backend = self.session()
        cycle = session.prepare(draw=0)
        backend.answers = ['MY-WISH. Try it.', choice(action='Try it.'), quiet(), outcome(),
                           experience(cycle['id']+'-outcome')]
        for _ in range(5): session.step(cycle['id'])
        self.assertEqual([v[0] for v in backend.inputs], ['impulse', 'choice', 'world', 'outcome', 'experience'])
        self.assertIsNone(backend.inputs[0][2]['moment'])
        self.assertIsNone(backend.inputs[2][2]['chosen_action'])
        self.assertNotIn('Try it.', json.dumps(backend.inputs[2][2]))
        self.assertEqual(backend.inputs[3][2]['chosen_action']['action'], 'Try it.')
        self.store.close()
        self.store = WorldStore(self.temp.name)
        self.assertEqual(self.store.actor_view('garik')['concerns'][0]['text'], 'MY-PRIVATE-CONCERN')
        with self.store.db:
            self.store.register_actor('other', 'Other')
        self.assertEqual(self.store.actor_view('other')['concerns'], [])
        self.assertNotIn('MY-EXPERIENCE', json.dumps(self.store.actor_view('other')))

    def test_director_gets_only_brief_signal_and_outcome_gets_no_signal(self):
        session, backend = self.session()
        first = session.prepare(draw=0)
        backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(),
                           experience(first['id']+'-outcome')]
        for _ in range(5): session.step(first['id'])
        second = session.prepare(draw=0)
        backend.answers = ['Try more.', choice(action='Try more.'), quiet(), outcome()]
        for _ in range(4): session.step(second['id'])
        world, result = backend.inputs[-2][2], backend.inputs[-1][2]
        self.assertEqual(world['director_context']['signal']['signal'], 'A brief current interest.')
        self.assertNotIn('director_context', result)
        for payload in (world, result):
            text = json.dumps(payload)
            for secret in ('MY-PRIVATE-CONCERN', 'MY-PRIVATE-STAKE', 'MY-EXPERIENCE', 'MY-WISH'):
                self.assertNotIn(secret, text)

    def test_unknown_sources_block_without_committing_personal_state(self):
        session, backend = self.session()
        cycle = session.prepare(draw=0)
        backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(), experience('invented')]
        for _ in range(4): session.step(cycle['id'])
        with self.assertRaises(ValueError): session.step(cycle['id'])
        self.assertEqual(self.store.actor_view('garik')['concerns'], [])
        self.assertEqual(len(backend.inputs), 5)

    def test_abandoned_attempt_is_not_a_world_fact_or_causal_receipt(self):
        session, backend = self.session()
        first = session.prepare(draw=0)
        backend.answers = ['UNPERFORMED-ACTION', choice(action='UNPERFORMED-ACTION')]
        for _ in range(2): session.step(first['id'])
        self.store.abandon(first['id'])
        view = self.store.actor_view('garik')
        self.assertEqual(view['unfinished_attempts'][0]['status'], 'no_committed_outcome')
        second = session.prepare(draw=0)
        backend.answers = ['Do something else.', choice(action='Do something else.'), quiet()]
        for _ in range(3): session.step(second['id'])
        context = backend.inputs[-1][2]
        self.assertNotIn('UNPERFORMED-ACTION', json.dumps(context))
        self.assertNotIn(first['id']+'-choice', context['director']['cause_ids'])

    def test_interrupted_action_preserves_whole_intent_for_next_choice_and_ui(self):
        session, backend = self.session()
        first = session.prepare(draw=0)
        full = 'I want to try a meal. Go and ask for meat. If sold, bring it home and try it with apples.'
        backend.answers = [full, choice(action='Go and ask for meat.'), RuntimeError('Synthetic world failure')]
        for _ in range(2): session.step(first['id'])
        with self.assertRaises(RuntimeError): session.step(first['id'])
        history = self.store.monitor()['history']
        self.assertTrue(all(e['execution'] == 'blocked' for e in history))
        self.store.abandon(first['id'])
        next_cycle = session.prepare(draw=0)
        backend.answers = ['Do something else.']
        session.step(next_cycle['id'])
        recovery = backend.inputs[-1][2]['life']['unfinished_attempts'][0]
        self.assertEqual(recovery['written_intent'], full)
        self.assertEqual(recovery['status'], 'no_committed_outcome')
        monitor = self.store.monitor()
        self.assertEqual(monitor['unfinished_attempts'][0]['written_intent'], full)
        previous = [e for e in monitor['history'] if e['cycle_id'] == first['id']]
        self.assertTrue(all(e['execution'] == 'interrupted' for e in previous))
        self.assertEqual(len(backend.inputs), 4)  # No automatic replay.

    def test_search_worker_receives_only_question_and_real_result_returns_to_actor(self):
        session, backend = self.session(research_enabled=True, autonomous_posts=True)
        cycle = session.prepare(draw=0)
        backend.answers = ['MY-PRIVATE-WISH. Look up a topic.', choice(kind='research', action='Look up a topic.'),
                           quiet(), research(cycle['id']), outcome(), experience(cycle['id']+'-research'),
                           {'decision': 'post', 'focus': 'My discovery.', 'source_ids': [cycle['id']+'-research']},
                           'Synthetic post about the discovery.']
        for _ in range(8): session.step(cycle['id'])
        worker = backend.inputs[3][2]
        self.assertEqual(worker, {'task_id': cycle['id'], 'question': 'Look up a topic.', 'topic': 'Try something new.'})
        self.assertNotIn('MY-PERSONA', backend.inputs[3][1])
        self.assertIn('Synthetic fact.', json.dumps(backend.inputs[5][2]))
        self.assertIn('https://example.org/evidence', json.dumps(backend.inputs[-1][2]))
        self.assertNotIn('MY-PRIVATE-WISH', json.dumps(backend.inputs[-1][2]))
        self.assertEqual(len(self.store.monitor()['posts']), 1)

    def test_search_requires_enabled_capability_and_actual_receipt(self):
        session, backend = self.session()
        cycle = session.prepare(draw=0)
        backend.answers = ['Look up a topic.', choice(kind='research', action='Look up a topic.')]
        session.step(cycle['id'])
        with self.assertRaises(ValueError): session.step(cycle['id'])
        self.assertEqual(len(backend.inputs), 2)
        self.store.abandon(cycle['id'])
        session.research_enabled = True
        cycle = session.prepare(draw=0)
        bad = research(cycle['id']); bad['tool_events'] = []
        backend.answers = ['Look up a topic.', choice(kind='research', action='Look up a topic.'), quiet(), bad]
        for _ in range(3): session.step(cycle['id'])
        with self.assertRaises(ValueError): session.step(cycle['id'])
        self.assertNotIn('research', self.store.cycle(cycle['id'])['value']['results'])

    def test_separate_search_process_error_keeps_its_usage_and_stops_the_cycle(self):
        service = Backend(RuntimeError('Synthetic search failure'))
        session, backend = self.session(research_enabled=True, research_backend=service)
        cycle = session.prepare(draw=0)
        backend.answers = ['Look up a topic.', choice(kind='research', action='Look up a topic.'), quiet()]
        for _ in range(3): session.step(cycle['id'])
        with self.assertRaises(RuntimeError): session.step(cycle['id'])
        self.assertEqual(len(backend.inputs), 3)
        self.assertEqual(len(service.inputs), 1)
        self.assertEqual(self.store.monitor()['calls'][0]['usage'], service.last_usage)
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'blocked')

    def test_personal_topic_can_change_and_close_with_same_identity(self):
        session, backend = self.session()
        for status in ('active', 'waiting', 'closed'):
            cycle = session.prepare(draw=0)
            update = experience(cycle['id']+'-outcome')
            update['concerns'][0].update(status=status, text='Changed '+status)
            backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(), update]
            for _ in range(5): session.step(cycle['id'])
        matters = self.store.actor_view('garik')['concerns']
        self.assertEqual(len(matters), 1)
        self.assertEqual(matters[0]['status'], 'closed')

    def test_personal_matter_survives_the_recent_event_window(self):
        session, backend = self.session(max_calls=64)
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        original = None
        for index in range(8):
            cid = session.prepare(draw=0)['id']
            if index == 0:
                original = experience(cid+'-outcome')['concerns'][0]
            update = experience(cid+'-outcome', concerns=[original] if index == 0 else [])
            backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(), update]
            for _ in range(5): session.step(cid)
        view = self.store.actor_view('garik')
        self.assertEqual(view['concerns'], [original])
        self.assertNotIn(original['source_ids'][0], [e['id'] for e in view['history']])
        self.assertEqual(view['personal_sources'][0]['id'], original['source_ids'][0])
        self.assertNotIn('world_note', json.dumps(view['personal_sources']))
        with self.store.lock, self.store.db:
            self.store.register_actor('other', 'Other')
        self.assertEqual(self.store.actor_view('other')['personal_sources'], [])

    def test_zero_director_attention_excludes_personal_signal(self):
        self.store.set_settings({**self.store.get('settings'), 'director_empathy': 0}, 0)
        session, backend = self.session()
        cycle = session.prepare(draw=0)
        backend.answers = ['Try it.', choice(action='Try it.'), quiet()]
        for _ in range(3): session.step(cycle['id'])
        self.assertNotIn('director_context', backend.inputs[-1][2])

    def test_every_database_read_in_a_cycle_and_monitor_is_serialized(self):
        raw, lock = self.store.db, self.store.lock
        class Guard:
            def execute(self, *args):
                if not lock._is_owned():
                    raise AssertionError('SQLite access without store lock')
                return raw.execute(*args)
            def __enter__(self): return raw.__enter__()
            def __exit__(self, *args): return raw.__exit__(*args)
            def close(self): return raw.close()
        self.store.db = Guard()
        session, backend = self.session(autonomous_posts=True)
        cid = session.prepare(draw=0)['id']
        backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(),
                           experience(cid+'-outcome'), {'decision': 'post', 'focus': 'A thing.', 'source_ids': [cid+'-outcome']}, 'Post.']
        for _ in range(7): session.step(cid)
        self.store.monitor()
        self.store.request_speech(cid)
        backend.answers = ['Reply.']
        session.step(cid)

    def test_monitor_and_actor_reads_can_overlap_cycle_writes(self):
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        stop, errors = threading.Event(), []
        def read():
            try:
                while not stop.is_set():
                    self.store.monitor()
                    self.store.actor_view('garik')
            except Exception as error:
                errors.append(error)
        reader = threading.Thread(target=read)
        reader.start()
        try:
            session, backend = self.session(max_calls=64)
            for _ in range(10):
                cid = session.prepare(draw=0)['id']
                backend.answers = ['Try it.', choice(action='Try it.'), quiet(), outcome(),
                                   experience(cid+'-outcome')]
                for _ in range(5): session.step(cid)
        finally:
            stop.set(); reader.join(3)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])

