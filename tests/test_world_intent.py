"""Coverage, conditional tails and recovery links; no real model or secrets."""
import copy
import json
import tempfile
import unittest

from persona_runtime.world import WorldStore, WorldSession
from persona_runtime.world_intent import adapt
from test_world import Backend, choice, outcome
from test_world_personal import quiet, experience


def routed(parts, **changes):
    value = {k: v for k, v in choice(note='').items() if k != 'action'}
    value.update(parts=[{'role': role, 'text': text} for role, text in parts], recovery=[])
    value.update(changes)
    return value


class IntentTests(unittest.TestCase):
    def test_native_units_preserve_every_character_without_retyping(self):
        from persona_runtime.world_intent import unit_io, decode_units, INTENT_SCHEMA
        original='  Хочу\nчай.  Пойду к другу — если\tпозовёт.\n'
        schema,payload,units=unit_io(INTENT_SCHEMA,{'written_intent':original})
        self.assertEqual(''.join(units.values()),original)
        roles={key:'attempt' for key in reversed(units)}
        metadata={k:v for k,v in routed([]).items() if k!='parts'}
        decoded=decode_units({**metadata,'unit_roles':roles},units)
        self.assertEqual(''.join(p['text'] for p in adapt(decoded,original)['intent_parts']),original)
        self.assertNotIn('parts',schema['properties'])
        self.assertEqual(payload['written_intent'],original)
        roles.pop(next(iter(roles)))
        with self.assertRaises(ValueError):decode_units({**metadata,'unit_roles':roles},units)

    def test_full_conditional_tail_reaches_outcome_but_private_feeling_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(directory); store.pause(False)
            try:
                private = 'PRIVATE-FEELING. '
                start = 'Ask for meat. '
                tail = 'If sold, take it home and try it with apples.'
                intent = private + start + tail
                backend = Backend(intent, routed([('private', private), ('attempt', start), ('attempt', tail)]), quiet(), outcome())
                session = WorldSession(store, backend, {'garik': 'Persona'}, 'Speech', personal_life=True, lossless_intentions=True)
                cid = session.prepare(draw=0)['id']
                for _ in range(4): session.step(cid)
                self.assertEqual([r[0] for r in backend.inputs], ['impulse', 'intent', 'world', 'outcome'])
                sent = backend.inputs[-1][2]['chosen_action']['action']
                self.assertIn(start.strip(), sent)
                self.assertIn(tail, sent)
                self.assertNotIn('PRIVATE-FEELING', json.dumps(backend.inputs[-1][2]))
                self.assertNotIn(tail, json.dumps(backend.inputs[-2][2]))
                self.assertEqual(''.join(p['text'] for p in store.cycle(cid)['value']['results']['choice']['intent_parts']), intent)
            finally: store.close()

    def test_any_omission_rewrite_or_reordering_is_rejected(self):
        full = 'Ask. If yes, eat.'
        for parts in ([('attempt', 'Ask.')], [('attempt', 'Ask. If yes, buy.')],
                      [('attempt', 'If yes, eat.'), ('attempt', 'Ask. ')],
                      [('attempt', full), ('private', '.')]):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                adapt(routed(parts), full)

    def test_explicit_later_is_preserved_without_becoming_current_action(self):
        value = adapt(routed([('attempt', 'Drink tea. '), ('later', 'Tomorrow visit the shop.')]),
                      'Drink tea. Tomorrow visit the shop.')
        self.assertEqual(value['action'], 'Drink tea.')
        self.assertEqual(value['intent_parts'][1]['text'], 'Tomorrow visit the shop.')

    def test_lossy_adapter_output_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(directory); store.pause(False)
            try:
                backend = Backend('Ask. If yes, eat.', routed([('attempt', 'Ask.')]))
                session = WorldSession(store, backend, {'garik': 'Persona'}, 'Speech', personal_life=True, lossless_intentions=True)
                cid = session.prepare(draw=0)['id'];session.step(cid)
                with self.assertRaises(ValueError): session.step(cid)
                self.assertNotIn('choice', store.cycle(cid)['value']['results'])
                self.assertEqual(store.cycle(cid)['status'], 'blocked')
                self.assertEqual(len(backend.inputs), 2)
            finally: store.close()

    def test_recovery_is_explicit_and_scoped_to_own_unfinished_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(directory);store.pause(False)
            try:
                backend = Backend('Ask.', routed([('attempt', 'Ask.')]), RuntimeError('Synthetic failure'))
                session = WorldSession(store, backend, {'garik': 'Persona'}, 'Speech', personal_life=True, lossless_intentions=True)
                first = session.prepare(draw=0)['id']
                for _ in range(2): session.step(first)
                with self.assertRaises(RuntimeError): session.step(first)
                store.abandon(first)
                recovery = {'cycle_id': first, 'decision': 'resume', 'note': 'Try the unfinished attempt.'}
                backend.answers = ['Try again.', routed([('attempt', 'Try again.')], recovery=[recovery]), quiet(), outcome()]
                second = session.prepare(draw=0)['id']
                for _ in range(4): session.step(second)
                self.assertEqual(backend.inputs[-4][2]['life']['unfinished_attempts'][0]['cycle_id'], first)
                self.assertEqual(store.actor_view('garik')['unfinished_attempts'], [])
                row = store.db.execute('SELECT * FROM world_recovery').fetchone()
                self.assertEqual(row['next_cycle'], second)
                self.assertEqual(store.cycle(first)['status'], 'abandoned')
            finally: store.close()

    def test_current_unfinished_cycle_cannot_be_its_own_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(directory); store.pause(False)
            try:
                with store.lock, store.db: store.register_actor('other', 'Other')
                backend = Backend('Try.', routed([('attempt', 'Try.')]))
                session = WorldSession(store, backend, {'garik': 'Persona'}, 'Speech', personal_life=True, lossless_intentions=True)
                cid = session.prepare(draw=0)['id']
                backend.answers[1]['recovery'] = [{'cycle_id': cid, 'decision': 'drop', 'note': ''}]
                session.step(cid)
                with self.assertRaises(ValueError): session.step(cid)
                self.assertEqual(store.db.execute('SELECT count(*) FROM world_recovery').fetchone()[0], 0)
            finally: store.close()

    def test_explicit_drop_and_defer_do_not_invent_an_outcome(self):
        for decision in ('drop', 'defer'):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                store = WorldStore(directory);store.pause(False)
                try:
                    backend = Backend('Ask.', routed([('attempt', 'Ask.')]), RuntimeError('Synthetic failure'))
                    session = WorldSession(store,backend,{'garik':'Persona'},'Speech',personal_life=True,lossless_intentions=True)
                    first=session.prepare(draw=0)['id']
                    for _ in range(2): session.step(first)
                    with self.assertRaises(RuntimeError): session.step(first)
                    store.abandon(first)
                    recovery={'cycle_id':first,'decision':decision,'note':'Explicit written decision.'}
                    backend.answers=['Drink tea.',routed([('attempt','Drink tea.')],recovery=[recovery])]
                    second=session.prepare(draw=0)['id']
                    for _ in range(2):session.step(second)
                    attempts=store.actor_view('garik')['unfinished_attempts']
                    self.assertEqual(len(attempts), 0 if decision=='drop' else 1)
                    if attempts:self.assertEqual(attempts[0]['disposition'],'defer')
                    self.assertNotIn('outcome',store.cycle(first)['value']['results'])
                finally:store.close()

    def test_another_actors_interrupted_intent_cannot_be_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            store=WorldStore(directory);store.pause(False)
            try:
                with store.lock,store.db:store.register_actor('other','Other')
                backend=Backend('PRIVATE-OTHER.',routed([('attempt','PRIVATE-OTHER.')],entity_ids=[]),RuntimeError('Synthetic failure'))
                other=WorldSession(store,backend,{'other':'Other'},'Speech',personal_life=True,lossless_intentions=True)
                first=other.prepare(actor_id='other',draw=0)['id']
                for _ in range(2):other.step(first)
                with self.assertRaises(RuntimeError):other.step(first)
                store.abandon(first)
                session=WorldSession(store,backend,{'garik':'Persona'},'Speech',personal_life=True,lossless_intentions=True)
                backend.answers=['Drink tea.',routed([('attempt','Drink tea.')],recovery=[{'cycle_id':first,'decision':'drop','note':''}])]
                second=session.prepare(draw=0)['id'];session.step(second)
                self.assertNotIn('PRIVATE-OTHER',json.dumps(backend.inputs[-1][2]))
                with self.assertRaises(ValueError):session.step(second)
                self.assertEqual(store.db.execute('SELECT count(*) FROM world_recovery').fetchone()[0],0)
            finally:store.close()
