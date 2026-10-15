"""Incident direction preserves gates, source continuity and actor privacy."""
import json
import unittest

from persona_runtime.world import WorldStore,WorldSession,WorldConflict
from persona_runtime.world_hooks import select_hook
from persona_runtime.world_dynamics import planning_directions,resident_interests
import test_world_active as fixtures
import test_channel_social as social_fixtures
from test_world import choice,outcome


class DynamicsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ActiveWorldTests();self.fixture.setUp()
        self.store,self.backend=self.fixture.store,self.fixture.backend
        self.session=WorldSession(self.store,self.backend,{'garik':'SECRET-PERSONA'},'Speech',
                                  personal_life=True,active_world=True,lively_world=True)

    def tearDown(self):self.fixture.tearDown()

    def expose(self):
        cid=self.session.prepare(draw=0)['id']
        self.backend.answers=[{'hooks':[fixtures.hook()]},self.fixture.exposed()]
        self.session.step(cid);self.session.step(cid)
        return cid

    def test_continuing_interest_survives_restart_and_is_not_actor_knowledge(self):
        cid=self.expose()
        hook=self.store.state()['hooks'][0]
        self.assertEqual(hook['mechanism'],'public_mess')
        self.assertEqual(hook['thread_ids'],['workshop'])
        self.backend.answers=['Try it.',choice(action='Try it.'),outcome()]
        for _ in range(3):self.session.step(cid)
        actor_payload=self.backend.inputs[-3][2]
        self.assertNotIn('SELLER-WANTS-TO-SELL',json.dumps(actor_payload))
        self.assertNotIn('scene_dynamics',actor_payload)
        interests=self.backend.inputs[-1][2]['scene_dynamics']['resident_interests']
        self.assertEqual(interests[0]['initial_friction'],'SELLER-REFUSES-TO-ADMIT-A-MISTAKE')
        self.store.close();self.store=WorldStore(self.fixture.temp.name);self.fixture.store=self.store
        self.assertEqual(resident_interests(self.store.state(),'garik'),interests)
        thread=self.store.state()['threads'][0]
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE world_threads SET value=? WHERE id=?',
                (json.dumps({**thread,'status':'closed','due_minute':None,'open_question':''}),thread['id']))
        self.assertEqual(resident_interests(self.store.state(),'garik'),[])

    def test_closed_story_descendant_keeps_cause_with_current_facts(self):
        self.expose();thread=self.store.state()['threads'][0]
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE world_threads SET value=? WHERE id=?',
                (json.dumps({**thread,'status':'closed','due_minute':None}),thread['id']))
            self.store.db.execute('INSERT INTO world_threads VALUES(?,?,?)',
                ('aftermath',json.dumps({**thread,'id':'aftermath','situation':'A real agreement changed the original terms.'}),'fixture'))
            self.store.db.execute('INSERT INTO world_thread_links VALUES(?,?,?)',('aftermath',thread['id'],'fixture'))
        interest=resident_interests(self.store.state(),'garik')[0]
        self.assertEqual(interest['current_stories'][0]['id'],'aftermath')
        self.assertIn('real agreement',interest['current_stories'][0]['situation'])
        self.assertEqual(resident_interests(self.store.state(),'other'),[])

    def test_direction_varies_sources_without_overriding_frequency_or_domain(self):
        self.expose();state=self.store.state()
        repeated={**fixtures.hook('repeat'), 'mechanism':'public_mess','exposed_to':[]}
        fresh={**fixtures.hook('fresh',anchor='home'),'mechanism':'chance','exposed_to':[]}
        state['hooks'] += [repeated,fresh]
        brief={'lively_world':True,'new_event_allowed':True,'open_slots':2,'allowed_domains':['city']}
        self.assertEqual(select_hook(state,'garik',brief)['id'],'fresh')
        self.assertIsNone(select_hook(state,'garik',{**brief,'new_event_allowed':False}))
        self.assertIsNone(select_hook(state,'garik',{**brief,'open_slots':0}))
        self.assertIsNone(select_hook(state,'garik',{**brief,'allowed_domains':['work']}))
        directions=planning_directions(state,'garik')
        self.assertEqual(len({d['mechanism'] for d in directions}),3)
        self.assertNotEqual(directions[0]['mechanism'],'public_mess')

    def test_existing_default_and_inflight_mode_are_preserved(self):
        cid=self.fixture.session.prepare(draw=0)['id']
        self.assertFalse(self.store.cycle(cid)['value']['lively_world'])
        self.assertNotIn('plan_directions',self.store.cycle(cid)['value']['brief'])
        self.backend.answers=[{'hooks':[fixtures.hook()]}]
        with self.assertRaises(WorldConflict):self.session.step(cid)
        self.assertEqual(self.backend.inputs,[])

    def test_separate_channel_visit_keeps_the_candidate_mode(self):
        social=social_fixtures.SocialTests();social.setUp()
        try:
            social.fixture.session.lively_world=True
            social.root()
            cycle=social.store.monitor()['cycles'][0]
            self.assertTrue(cycle['value']['lively_world'])
            self.assertEqual(cycle['status'],'complete')
            self.assertEqual(social.connection.state()['outbox'][0]['status'],'sent')
        finally:social.tearDown()
