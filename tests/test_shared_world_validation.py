"""Author receipts and independent story capacity; exact saved-result recovery only."""
import copy
import json
import unittest
from unittest.mock import patch

import test_shared_life as fixtures
from test_world import outcome
from test_world_personal import quiet
from test_world_interventions import event
from persona_runtime.world import director,WorldConflict
from persona_runtime.world_protocol import validate_patch
from persona_runtime.world_interventions import enqueue


def thread(identity,visible):
    return {'id':identity,'title':'A test matter','domain':'people','status':'open',
            'situation':'An unresolved test matter.','open_question':'What happens next?',
            'due_minute':None,'entity_ids':[],'visible_to':visible}


class SharedValidationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def seed_private(self,count=3):
        with self.store.db:
            for n in range(count):
                t=thread('private_'+str(n),['garik'])
                self.store.db.execute('INSERT INTO world_threads VALUES(?,?,?)',(t['id'],json.dumps(t),'test'))
            self.store.set_settings({**self.store.get('settings'),'max_threads':1},self.store.get('settings_revision'))

    def pause_both(self):
        for life in self.f.lives.values():life.action(False)

    def test_private_backlog_and_lowered_limit_leave_other_actor_a_slot(self):
        self.seed_private();state=self.store.state()
        validate_patch({'entities':[],'threads':[thread('arkady_one',['arkady'])]},state,state['settings'])
        self.assertEqual(director(state,'arkady',5,1)['open_slots'],1)
        self.assertEqual(director(state,'garik',5,1)['open_slots'],0)
        for proposed in ([thread('shared',['arkady','garik'])],
                         [thread('arkady_one',['arkady']),thread('arkady_two',['arkady'])],
                         [thread('back_one',[]),thread('back_two',[])]):
            with self.subTest(proposed=proposed),self.assertRaisesRegex(ValueError,'too many open stories'):
                validate_patch({'entities':[],'threads':proposed},state,state['settings'])

    def author_event(self):
        value=event()
        for obs in value['observations']:obs.update(via='unaware',observation='',entity_ids=[])
        enqueue(self.store,'author-source-one','A test external event only the world knows about.')
        self.f.backends['arkady'].answers=[value];self.f.scheduler.tick()

    def consequence(self,source='author-source-one'):
        return {**quiet(),'beat':'consequence','domain':'city','pattern':'author_consequence',
                'causes':[source],'observation':'A new observable consequence occurs.','visible_to':['garik']}

    def test_applied_author_ids_are_director_receipts_without_actor_knowledge(self):
        self.author_event();session=self.f.lives['garik'].lab.session
        cycle=session.prepare('garik',minutes=0,draw=1)
        self.assertIn('author-source-one',cycle['value']['brief']['cause_ids'])
        self.assertIn('consequence',cycle['value']['brief']['allowed_beats'])
        self.assertNotIn('author-source-one',json.dumps(self.store.actor_view('garik')))
        self.f.backends['garik'].answers=[self.consequence()];session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['phase'],'impulse')

    def rejected_world(self,source='author-source-one'):
        self.author_event();session=self.f.lives['garik'].lab.session
        cycle=session.prepare('garik',minutes=0,draw=1);value=copy.deepcopy(cycle['value'])
        value['brief']['cause_ids']=[]
        with self.store.db:self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(value),cycle['id']))
        self.f.backends['garik'].answers=[self.consequence(source)]
        with self.assertRaisesRegex(ValueError,'invented causal receipt'):session.step(cycle['id'])
        return cycle['id']

    def test_saved_world_recovery_preserves_failed_usage_and_never_calls_again(self):
        cid=self.rejected_world();old=copy.deepcopy(self.store.cycle(cid))
        call=dict(self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,)).fetchone())
        with self.assertRaises(WorldConflict):self.store.recover_saved_shared_stage(cid,'explicit test review')
        self.pause_both();self.store.recover_saved_shared_stage(cid,'explicit test review')
        self.store.recover_saved_shared_stage(cid,'no second application')
        fixed=self.store.cycle(cid)
        self.assertEqual(fixed['phase'],'impulse')
        self.assertEqual(fixed['status'],'held')
        self.assertEqual(fixed['value']['rejected'],old['value']['rejected'])
        self.assertEqual(fixed['value']['inputs'],old['value']['inputs'])
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,)).fetchone()),call)
        self.assertEqual(len(self.f.backends['garik'].inputs),1)
        self.assertEqual(len(self.store.get('shared_stage_repairs')),1)
        with self.store.db:self.store.put('revision',self.store.get('revision')+1)
        self.f.backends['garik'].answers=['Хочу заняться своим делом.']
        for life in self.f.lives.values():life.action(True)
        self.f.scheduler.tick()
        self.assertEqual(self.store.cycle(cid)['phase'],'choice')
        self.assertEqual(self.f.lives['garik'].get('life_status')['status'],'running')

    def test_unknown_author_receipt_still_rejects_and_review_rolls_back(self):
        cid=self.rejected_world('invented-author-source');self.pause_both();before=copy.deepcopy(self.store.cycle(cid))
        with self.assertRaisesRegex(ValueError,'invented causal receipt'):
            self.store.recover_saved_shared_stage(cid,'not a real receipt')
        self.assertEqual(self.store.cycle(cid),before)
        self.assertFalse(self.store.get('shared_stage_repairs'))

    def test_saved_outcome_uses_own_slot_and_keeps_accepted_action_window(self):
        self.seed_private();cid=self.f.plan('arkady',5,'arkady_garage')
        window=copy.deepcopy(self.store.cycle(cid)['value']['shared_window'])
        self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),cid))
        self.f.backends['arkady'].answers=[outcome(elapsed_minutes=5,time_costs=[{'activity':'Test attempt','minutes':5}],threads=[thread('arkady_one',['arkady'])])]
        with patch('persona_runtime.world_protocol.validate_patch',side_effect=ValueError('too many open stories')):
            with self.assertRaisesRegex(ValueError,'too many open stories'):self.f.lives['arkady'].lab.session.step(cid)
        call=dict(self.store.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone())
        self.pause_both();self.store.recover_saved_shared_stage(cid,'independent actor capacity review')
        fixed=self.store.cycle(cid)
        self.assertEqual(fixed['phase'],'experience');self.assertEqual(fixed['value']['shared_window'],window)
        self.assertEqual(dict(self.store.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone()),call)
        self.assertEqual(len(self.store.state()['threads']),4)
