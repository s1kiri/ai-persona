"""No planned arrivals or fabricated outcomes; explicit offline reviews only."""
import copy
import json
import unittest

import test_shared_life as fixtures
from test_shared_world_validation import thread
from test_world import outcome
from test_world_backend import Client
from persona_runtime.world_backend import WorldBackend
from persona_runtime.world_protocol import SCHEMAS
from persona_runtime.world import WorldConflict


class OutcomeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def due(self,cid):
        self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),cid))

    def pause(self):
        for life in self.f.lives.values():life.action(False)

    def test_successful_wait_at_home_does_not_confirm_planned_trip_or_meeting(self):
        with self.store.db:self.store.db.execute("UPDATE peer_positions SET place_id='arkady_garage_yard' WHERE actor='arkady'")
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.due(cid)
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5,observation='Stayed home; condition for departure never occurred.'),'final_place_id':'home'}]
        self.f.scheduler._run_cycle(self.store.cycle(cid))
        self.assertEqual(self.store.peer_context('garik')['place_id'],'home')
        self.assertEqual(self.store.nearby('garik'),[])
        self.assertEqual(self.store.cycle(cid)['value']['position_receipt']['final_place_id'],'home')
        self.assertFalse(self.store.db.execute("SELECT 1 FROM peer_deliveries").fetchone())

    def test_legacy_success_does_not_imply_arrival(self):
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.due(cid)
        self.f.backends['garik'].answers=[outcome(elapsed_minutes=5)]
        self.f.lives['garik'].lab.session.step(cid)
        self.assertIsNone(self.store.peer_context('garik')['place_id'])

    def test_partial_outcome_can_confirm_return_home(self):
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.due(cid)
        self.f.backends['garik'].answers=[{**outcome(status='partial',elapsed_minutes=5,observation='Tried to leave, then returned home.'),'final_place_id':'home'}]
        self.f.lives['garik'].lab.session.step(cid)
        self.assertEqual(self.store.peer_context('garik')['place_id'],'home')

    def test_model_cannot_confirm_an_unknown_location(self):
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.due(cid)
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5),'final_place_id':'arkady_garage'}]
        with self.assertRaisesRegex(ValueError,'observed location'):self.f.lives['garik'].lab.session.step(cid)
        self.assertIsNone(self.store.peer_context('garik')['place_id'])

    def test_delivered_peer_messages_are_world_receipts(self):
        from test_shared_life import peer_answer
        self.f.job('garik',peer_answer(decision='message',recipient_actor_id='arkady',text='Actual delivered words.'))
        cid=self.f.plan('arkady',5,'arkady_garage');self.due(cid)
        self.f.backends['arkady'].answers=[{**outcome(elapsed_minutes=5),'final_place_id':'arkady_garage'}]
        self.f.lives['arkady'].lab.session.step(cid)
        words=self.f.backends['arkady'].inputs[-1][2]['shared_scene']['delivered_words']
        self.assertTrue(any(e['value']['text']=='Actual delivered words.' for e in words))

    def rejected(self):
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.due(cid)
        t=thread('private_route',['arkady','garik']);t['entity_ids']=['arkady_transmitter']
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5,observation='Stayed home.',threads=[t]),'final_place_id':'home'}]
        with self.assertRaisesRegex(ValueError,'undiscovered entity'):self.f.lives['garik'].lab.session.step(cid)
        return cid

    def test_quarantine_preserves_original_failure_knowledge_and_window(self):
        cid=self.rejected();before=copy.deepcopy(self.store.cycle(cid));calls=list(self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,)))
        self.assertIsNone(self.store.peer_context('garik')['place_id'])
        with self.assertRaises(WorldConflict):self.store.review_saved_outcome(cid,['private_route'],'home','review')
        self.pause();self.store.review_saved_outcome(cid,['private_route'],'home','saved observation explicitly says stayed home')
        self.store.review_saved_outcome(cid,['private_route'],'home','idempotent')
        fixed=self.store.cycle(cid)
        self.assertEqual(fixed['status'],'held');self.assertEqual(fixed['phase'],'experience')
        for field in ('rejected','inputs','failure','shared_window'):
            self.assertEqual(fixed['value'][field],before['value'][field])
        self.assertEqual([tuple(r) for r in calls],[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,))])
        self.assertEqual(fixed['value']['results']['outcome']['threads'],[])
        self.assertEqual(self.store.peer_context('garik')['place_id'],'home')
        transmitter=next(e for e in self.store.state()['entities'] if e['id']=='arkady_transmitter')
        self.assertNotIn('garik',transmitter['known_to'])
        self.assertEqual(len(self.f.backends['garik'].inputs),4)
        self.assertEqual(len(self.store.get('shared_outcome_reviews')),1)

    def test_unknown_location_and_valid_thread_cannot_be_reviewed_away(self):
        cid=self.rejected();self.pause();before=copy.deepcopy(self.store.cycle(cid))
        with self.assertRaises(ValueError):self.store.review_saved_outcome(cid,['private_route'],'unknown_place','test')
        with self.assertRaises(WorldConflict):self.store.review_saved_outcome(cid,['some_other_thread'],'home','test')
        self.assertEqual(self.store.cycle(cid),before)
        self.assertFalse(self.store.get('shared_outcome_reviews'))

    def test_unanswered_attempt_can_be_released_without_creating_result(self):
        cid=self.f.plan('arkady',5,'arkady_garage');self.due(cid)
        call,_=self.store.claim(cid,None)
        self.store.fail(call,error_type='CodexError',diagnostics={'turn_completed':False,'failure_reason':'stream_error'})
        before=copy.deepcopy(self.store.cycle(cid));source=dict(self.store.db.execute('SELECT * FROM world_calls WHERE id=?',(call,)).fetchone())
        self.pause();self.store.release_interrupted_outcome(cid,'explicit stream interruption review')
        self.store.release_interrupted_outcome(cid,'idempotent')
        fixed=self.store.cycle(cid);self.assertEqual(fixed['status'],'abandoned');self.assertEqual(fixed['value'],before['value'])
        self.assertNotIn('outcome',fixed['value']['results'])
        self.assertEqual(source,dict(self.store.db.execute('SELECT * FROM world_calls WHERE id=?',(call,)).fetchone()))
        self.assertEqual(source['usage'],'{}')
        self.assertEqual(self.store.actor_view('arkady')['unfinished_attempts'][-1]['cycle_id'],cid)
        self.assertIsNone(self.store.peer_context('arkady')['place_id'])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone())

    def test_completed_rejected_answer_cannot_be_released_as_unanswered(self):
        cid=self.rejected();self.pause()
        with self.assertRaises(WorldConflict):self.store.release_interrupted_outcome(cid,'not an interrupted attempt')

    def test_incompatible_timing_review_preserves_answer_and_never_applies_it(self):
        cid=self.f.plan('garik',5,'home');self.due(cid)
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=10,observation='UNACCEPTED physical result.'),'final_place_id':'home'}]
        with self.assertRaisesRegex(ValueError,'elapsed wall time'):self.f.lives['garik'].lab.session.step(cid)
        before=copy.deepcopy(self.store.cycle(cid));state=copy.deepcopy(self.store.state())
        call=dict(self.store.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone())
        with self.assertRaises(WorldConflict):self.store.release_rejected_timing_outcome(cid,'review')
        self.pause();self.store.release_rejected_timing_outcome(cid,'Completed answer exceeds accepted action window; no canonical result.')
        self.store.release_rejected_timing_outcome(cid,'idempotent')
        after=self.store.cycle(cid)
        self.assertEqual(after['status'],'abandoned');self.assertEqual(after['value'],before['value'])
        self.assertEqual(self.store.get('minute'),state['minute'])
        self.assertEqual(self.store.state()['entities'],state['entities']);self.assertEqual(self.store.state()['threads'],state['threads'])
        self.assertEqual(call,dict(self.store.db.execute('SELECT * FROM world_calls WHERE id=?',(call['id'],)).fetchone()))
        self.assertNotIn('UNACCEPTED',json.dumps(self.store.actor_view('garik')))
        self.assertTrue(any(a['cycle_id']==cid for a in self.store.actor_view('garik')['unfinished_attempts']))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone())
        self.assertEqual(len(self.store.get('rejected_timing_reviews')),1)
        self.assertEqual(len(self.f.backends['garik'].inputs),4)

    def test_matching_time_rejection_cannot_be_released_through_timing_review(self):
        cid=self.rejected();self.pause();before=copy.deepcopy(self.store.cycle(cid))
        with self.assertRaises(WorldConflict):self.store.release_rejected_timing_outcome(cid,'unrelated failure')
        self.assertEqual(self.store.cycle(cid),before)

    def test_native_schema_adds_location_only_for_shared_outcome(self):
        backend=WorldBackend(self.f.root/'backend');backend._start=lambda:None;backend.overrides={};backend.workspace=self.f.root
        backend.client=Client({**outcome(),'final_place_id':'home'})
        backend.generate('outcome','test',{'shared_action_window':{'duration_minutes':15}})
        schema=next(p['outputSchema'] for m,p in backend.client.calls if m=='turn/start')
        self.assertIn('final_place_id',schema['required'])
        self.assertEqual(schema['properties']['elapsed_minutes']['enum'],[15])
        self.assertNotIn('final_place_id',SCHEMAS['outcome']['properties'])
        self.assertNotIn('enum',SCHEMAS['outcome']['properties']['elapsed_minutes'])
