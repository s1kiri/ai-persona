import json
import unittest
import test_shared_life as fixtures
from persona_runtime.world_hooks import select_hook
from test_world_personal import quiet
from test_world import outcome


class SceneScopeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def test_distant_planned_scene_cannot_be_delivered_at_home(self):
        h={'id':'remote','actor_ids':['garik'],'anchor_ids':['arkady_garage_yard'],'view_from':['arkady_garage'],
           'domain':'people','opens_minute':0,'closes_minute':999999,'exposed_to':[]}
        state=self.store.state();state['hooks']=[h]
        brief={'new_event_allowed':True,'open_slots':1,'allowed_domains':['people']}
        self.assertIsNone(select_hook(state,'garik',brief))
        state['hooks'].append({**h,'id':'near','view_from':['home']})
        self.assertEqual(select_hook(state,'garik',brief)['id'],'near')
        state['positions']=[{**p,'place_id':None} for p in state['positions']]
        self.assertIsNone(select_hook(state,'garik',brief))

    def test_scene_names_nearby_friend_without_disclosing_a_distant_position(self):
        distant=self.store.peer_context('garik')['current_scene']
        self.assertEqual(distant['place_name'],'Съёмная квартира');self.assertEqual(distant['nearby'],[])
        self.assertNotIn('Гараж-мастерская',str(distant))
        with self.store.db:self.store.db.execute("UPDATE peer_positions SET place_id='home',source_id='confirmed-arrival' WHERE actor='arkady'")
        scene=self.store.peer_context('garik')['current_scene']
        self.assertEqual(scene['nearby'],[{'actor_id':'arkady','name':'Аркадий Кедров'}])

    def test_viewpoint_is_recorded_but_not_leaked_as_character_knowledge(self):
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':2},self.store.get('settings_revision'))
        cid=self.f.lives['garik'].lab.session.prepare('garik',minutes=10,draw=0)['id']
        hook={'id':'window_scene','anchor_ids':['home'],'actor_ids':['garik'],'domain':'home',
              'opens_minute':self.store.get('minute'),'closes_minute':self.store.get('minute')+60,
              'route':'Видно из окна.','npc_want':'Его собственное дело.','friction':'Люди не договорились.','temptation':'Можно посмотреть.','view_from':['home']}
        self.f.backends['garik'].answers=[{'hooks':[hook]}];self.f.lives['garik'].lab.session.step(cid)
        self.assertEqual(self.store.state()['hooks'][0]['view_from'],['home'])
        self.assertNotIn('hooks',self.store.actor_view('garik'))

    def test_observed_opening_need_not_create_an_unfinished_story(self):
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':2},self.store.get('settings_revision'))
        session=self.f.lives['garik'].lab.session;session.lively_world=True;cid=session.prepare('garik',minutes=10,draw=0)['id']
        hook={'id':'small_scene','anchor_ids':['home'],'actor_ids':['garik'],'domain':'home',
              'opens_minute':self.store.get('minute'),'closes_minute':self.store.get('minute')+60,
              'route':'Видно из окна.','npc_want':'Донести свою вещь.','friction':'Не помещается в дверь.','temptation':'Можно посмотреть.','view_from':['home']}
        self.f.backends['garik'].answers=[{'hooks':[hook]}, {**quiet(),'beat':'encounter','domain':'home','pattern':'small_passing_scene','entity_ids':['home'],'observation':'Мужик у подъезда развернул большую коробку и наконец прошёл.'}]
        session.step(cid);session.step(cid)
        self.assertEqual(self.store.state()['threads'],[])
        self.assertEqual(self.store.state()['hooks'][0]['thread_ids'],[])
        self.assertEqual(self.store.state()['hooks'][0]['exposed_to'],['garik'])

    def test_world_cannot_speak_for_other_managed_actor(self):
        cid=self.f.plan('garik',5,'arkady_garage_yard');self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),cid))
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5,observation='Аркадий ответил, что согласен.'),'final_place_id':'arkady_garage_yard'}]
        with self.assertRaisesRegex(ValueError,'another managed actor speech'):self.f.lives['garik'].lab.session.step(cid)
        self.assertNotIn('outcome',self.store.cycle(cid)['value']['results'])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE id=?",(cid+'-outcome',)).fetchone())
        self.assertEqual(self.store.cycle(cid)['value']['rejected']['answer']['observation'],'Аркадий ответил, что согласен.')

    def test_observed_npc_story_may_already_be_closed(self):
        from test_shared_world_validation import thread
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':2},self.store.get('settings_revision'))
        session=self.f.lives['garik'].lab.session;session.lively_world=True
        cid=session.prepare('garik',minutes=10,draw=0)['id'];minute=self.store.get('minute')
        hook={'id':'finished_scene','anchor_ids':['home'],'actor_ids':['garik'],'domain':'home',
              'opens_minute':minute,'closes_minute':minute+60,'route':'Видно из окна.',
              'npc_want':'Занести свою коробку.','friction':'Не помещается.','temptation':'Можно посмотреть.','view_from':['home']}
        closed={**thread('box_delivered',['garik']),'status':'closed','entity_ids':['home'],
                'situation':'Житель развернул коробку и занёс её.','open_question':''}
        self.f.backends['garik'].answers=[{'hooks':[hook]}, {**quiet(),'beat':'encounter','domain':'home',
            'pattern':'finished_passing_scene','entity_ids':['home'],'thread_id':'box_delivered','threads':[closed],
            'observation':'Житель развернул коробку и наконец занёс её в подъезд.'}]
        session.step(cid);session.step(cid)
        self.assertEqual(self.store.state()['threads'][0]['status'],'closed')
        self.assertEqual(self.store.state()['hooks'][0]['thread_ids'],['box_delivered'])

    def test_world_cannot_complete_other_actor_pending_trip(self):
        cid=self.f.plan('garik',5,'home');self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),cid))
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5,observation='Аркадий пришёл туда и поел с Гариком.'),'final_place_id':'home'}]
        with self.assertRaisesRegex(ValueError,'another managed actor speech/actions'):self.f.lives['garik'].lab.session.step(cid)
        self.assertNotIn('outcome',self.store.cycle(cid)['value']['results'])

    def test_shared_meal_cannot_be_created_in_only_one_actor_outcome(self):
        for observation in ('Гарик спустился за шаурмой. Аркадий ждал; они сели вместе и поели.',
                            'Аркадий и Гарик вместе прошли к шаурмичной и поели.',
                            'Аркадий осмотрел рейку и не заметил гвоздей.'):
            with self.subTest(observation=observation):
                f=fixtures.SharedLifeTests();f.setUp()
                try:
                    cid=f.plan('garik',5,'shawarma');f.now+=5*60;f.scheduler.advance()
                    with f.store.db:f.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(f.store.get('revision'),cid))
                    f.backends['garik'].answers=[{**outcome(elapsed_minutes=5,observation=observation),'final_place_id':'shawarma'}]
                    with self.assertRaisesRegex(ValueError,'another managed actor speech/actions'):f.lives['garik'].lab.session.step(cid)
                    self.assertNotIn('outcome',f.store.cycle(cid)['value']['results'])
                finally:f.doCleanups()

    def test_bad_model_plan_ids_can_be_assigned_without_replaying_or_editing_scenes(self):
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':2},self.store.get('settings_revision'))
        cid=self.f.lives['garik'].lab.session.prepare('garik',minutes=10,draw=0)['id']
        hook={'id':'сцена_у_окна','anchor_ids':['home'],'actor_ids':['garik'],'domain':'home',
              'opens_minute':self.store.get('minute'),'closes_minute':self.store.get('minute')+60,
              'route':'Видно из окна.','npc_want':'Его собственное дело.','friction':'Люди не договорились.','temptation':'Можно посмотреть.','view_from':['home']}
        self.f.backends['garik'].answers=[{'hooks':[hook]}]
        with self.assertRaises(ValueError):self.f.lives['garik'].lab.session.step(cid)
        import copy
        old=copy.deepcopy(self.store.cycle(cid));call=tuple(self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,)).fetchone())
        for life in self.f.lives.values():life.action(False)
        self.store.recover_horizon_identities(cid,'service ID review')
        self.store.recover_horizon_identities(cid,'idempotent')
        fixed=self.store.cycle(cid);self.assertEqual(fixed['status'],'held');self.assertEqual(fixed['phase'],'world')
        self.assertEqual(fixed['value']['rejected'],old['value']['rejected'])
        self.assertEqual(fixed['value']['inputs'],old['value']['inputs'])
        self.assertEqual(call,tuple(self.store.db.execute('SELECT * FROM world_calls WHERE cycle_id=?',(cid,)).fetchone()))
        accepted=self.store.state()['hooks'][0]
        self.assertRegex(accepted['id'],r'^hook_[a-f0-9]+$');self.assertEqual(accepted['view_from'],['home'])
        for key in hook:
            if key!='id':self.assertEqual(accepted[key],hook[key])
        self.assertEqual(len(self.f.backends['garik'].inputs),1)
