"""Rejected spatial events stay outside history; world receives explicit canon/location."""
import copy
import json
import unittest
import test_shared_life as fixtures
from test_world_personal import quiet
from persona_runtime.world import WorldConflict


class PlaceEventTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def reject(self):
        with self.store.db:self.store.db.execute("UPDATE peer_positions SET place_id='shawarma',source_id='confirmed-outcome' WHERE actor='garik'")
        session=self.f.lives['garik'].lab.session;cycle=session.prepare('garik',minutes=0,draw=1)
        place=copy.deepcopy(next(e for e in self.store.state()['entities'] if e['id']=='shawarma'))
        place['description']+=' An unapproved clarification.'
        self.f.backends['garik'].answers=[{**quiet(),'observation':'REJECTED-OBSERVATION from an inconsistent location.','entities':[place]}]
        with self.assertRaisesRegex(ValueError,'author-established place'):session.step(cycle['id'])
        return cycle['id']

    def pause(self):
        for life in self.f.lives.values():life.action(False)

    def test_contract_makes_confirmed_location_and_fixed_identity_explicit(self):
        self.reject();_,instruction,payload=self.f.backends['garik'].inputs[-1]
        contract=payload['world_contract']
        self.assertEqual(contract['observer_actor_id'],'garik')
        self.assertEqual(contract['location'],{'place_id':'shawarma','source_id':'confirmed-outcome'})
        locked=next(e for e in contract['fixed_place_fields'] if e['id']=='shawarma')
        original=next(e for e in payload['world']['entities'] if e['id']=='shawarma')
        self.assertEqual(locked,{k:original[k] for k in ('id','name','kind','description')})
        self.assertNotIn('condition',locked)
        self.assertIn('world не перемещает героя',instruction)
        self.assertNotIn('world_contract',self.store.actor_view('garik'))

    def test_entire_event_review_preserves_position_answer_usage_and_history(self):
        cid=self.reject();before=copy.deepcopy(self.store.cycle(cid));state=copy.deepcopy(self.store.state())
        calls=[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls ORDER BY rowid')]
        with self.assertRaises(WorldConflict):self.store.release_rejected_place_event(cid,'explicit review')
        self.pause();self.store.release_rejected_place_event(cid,'No canonical event: wrong location and changed immutable description.')
        self.store.release_rejected_place_event(cid,'idempotent')
        after=self.store.cycle(cid)
        self.assertEqual(after['status'],'abandoned');self.assertEqual(after['value'],before['value'])
        for k in ('minute','entities','threads','positions','history'):
            self.assertEqual(self.store.state()[k],state[k])
        self.assertEqual(calls,[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls ORDER BY rowid')])
        self.assertNotIn('REJECTED-OBSERVATION',json.dumps(self.store.actor_view('garik')))
        self.assertFalse(self.store.db.execute('SELECT 1 FROM world_events WHERE cycle_id=?',(cid,)).fetchone())
        self.assertEqual(len(self.f.backends['garik'].inputs),1)
        self.assertEqual(len(self.store.get('rejected_place_event_reviews')),1)

    def test_completed_or_unrelated_failure_cannot_be_discarded(self):
        cid=self.reject();self.pause();before=copy.deepcopy(self.store.cycle(cid))
        changed=copy.deepcopy(before['value'])
        changed['rejected']['answer']['entities']=[]
        with self.store.db:self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(changed),cid))
        with self.assertRaises(WorldConflict):self.store.release_rejected_place_event(cid,'not a place identity violation')
        self.assertFalse(self.store.get('rejected_place_event_reviews'))
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='complete',value=? WHERE id=?",(json.dumps(before['value']),cid))
        with self.assertRaises(WorldConflict):self.store.release_rejected_place_event(cid,'already committed')
