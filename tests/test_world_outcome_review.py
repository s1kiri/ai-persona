"""Explicit review can omit an avatar patch, never rewrite or retry an outcome."""
import copy
import json
import tempfile
import unittest
from persona_runtime.world import WorldStore,WorldSession
from test_world import Backend,event,choice,outcome


class OutcomeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=WorldStore(self.temp.name);self.store.pause(False)
        self.backend=Backend(event(),choice())
        self.session=WorldSession(self.store,self.backend,{'garik':'Synthetic'},'Speech')
        self.cid=self.session.prepare(draw=0)['id']
        self.session.step(self.cid);self.session.step(self.cid)
        self.actor=copy.deepcopy(next(e for e in self.store.state()['entities'] if e['id']=='garik'))

    def tearDown(self):self.store.close();self.temp.cleanup()

    def reject(self,**changes):
        answer=outcome(entities=[{**self.actor,'condition':'Returned home.'}],**changes)
        self.backend.answers=[answer]
        with self.assertRaises(ValueError):self.session.step(self.cid)
        return answer

    def test_review_retains_words_time_error_usage_and_actor_without_new_call(self):
        original=self.reject();calls=len(self.backend.inputs);minute=self.store.get('minute')
        self.store.recover_outcome_without_actor_patch(self.cid)
        self.store.recover_outcome_without_actor_patch(self.cid)
        cycle=self.store.cycle(self.cid)
        self.assertEqual(cycle['phase'],'experience')
        self.assertEqual(cycle['value']['rejected']['answer'],original)
        self.assertEqual(cycle['value']['results']['outcome'],{**original,'entities':[]})
        self.assertEqual(self.store.get('minute'),minute+original['elapsed_minutes'])
        self.assertEqual(next(e for e in self.store.state()['entities'] if e['id']=='garik'),self.actor)
        self.assertEqual(len(self.backend.inputs),calls)
        receipt=self.store.db.execute("SELECT status,usage FROM world_calls WHERE cycle_id=? AND stage='outcome'",(self.cid,)).fetchone()
        self.assertEqual(receipt['status'],'failed');self.assertEqual(json.loads(receipt['usage']),self.backend.last_usage)

    def test_other_invalid_fields_still_block_atomic_review(self):
        self.reject(elapsed_minutes=0,time_costs=[{'activity':'invalid time','minutes':1}])
        minute=self.store.get('minute')
        with self.assertRaises(ValueError):self.store.recover_outcome_without_actor_patch(self.cid)
        self.assertEqual(self.store.get('minute'),minute)
        self.assertEqual(self.store.cycle(self.cid)['status'],'blocked')
        self.assertFalse(self.store.db.execute('SELECT 1 FROM world_repairs').fetchone())
