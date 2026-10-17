import copy
import json
import unittest
import test_shared_life as fixtures
from test_world_personal import quiet
from test_world_geography import street,route
from persona_runtime.world_wire import MODE,project,schema
from persona_runtime.world_protocol import SCHEMAS
from persona_runtime.world_geography import LINKS


def native(answer):
    value=copy.deepcopy(answer);value['patch_mode']=MODE
    for field in ('entities','place_links'):
        for item in value.get(field,[]):item['learned_by']=item.pop('known_to')
    if 'entity_ids' in value:value['observed_ids']=value.pop('entity_ids')
    return value


class WorldWireTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def test_native_schema_uses_discoveries_and_only_known_reference_options(self):
        core=copy.deepcopy(SCHEMAS['world']);core['properties']['place_links']=copy.deepcopy(LINKS);core['required'].append('place_links')
        wire=schema(core,'world',{'director':{'actor_id':'garik'},'world':self.store.state()})
        props=wire['properties'];self.assertNotIn('entity_ids',props)
        self.assertNotIn('arkady_garage',props['observed_ids']['items']['enum'])
        for field in ('entities','place_links'):
            self.assertNotIn('known_to',props[field]['items']['properties'])
            self.assertEqual(props[field]['items']['properties']['learned_by']['items']['enum'],['garik'])
        self.assertIn('entity_ids',SCHEMAS['world']['properties'])

    def test_discovery_preserves_previous_knower_and_assigns_observed_reference(self):
        original={**street(),'known_to':['arkady']}
        with self.store.db:self.store.db.execute('INSERT INTO world_entities VALUES(?,?,?)',('lesnaya',json.dumps(original),'old-observation'))
        payload={'director':{'actor_id':'garik'},'world':self.store.state()}
        wire=native({**quiet(),'entities':[street()],'place_links':[route()]})
        answer=project(wire,'world',payload)
        self.assertEqual(answer['entities'][0]['known_to'],['arkady','garik'])
        self.assertIn('lesnaya',answer['entity_ids']);self.assertEqual(answer['place_links'][0]['known_to'],['garik'])
        self.assertEqual(wire['entities'][0]['learned_by'],['garik'])

    def test_remote_discovery_and_link_as_observation_are_not_silently_accepted(self):
        payload={'director':{'actor_id':'garik'},'world':self.store.state()}
        for patch in ({'entities':[{**street(),'known_to':['arkady']}]},{'entity_ids':['home_lesnaya']}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                project(native({**quiet(),'place_links':[],**patch}),'world',payload)

    def test_valid_native_discovery_commits_atomically_and_retains_exact_wire(self):
        s=self.f.lives['garik'].lab.session;cid=s.prepare('garik',minutes=0,draw=1)['id']
        wire=native({**quiet(),'beat':'progress','domain':'city','entities':[street()],'place_links':[route()]})
        self.f.backends['garik'].answers=[wire];s.step(cid)
        self.assertEqual(self.store.cycle(cid)['value']['native_world_outputs']['world'],wire)
        self.assertEqual(self.store.actor_view('garik')['place_links'][0]['source_id'],cid+'-world')
        self.assertEqual(self.store.actor_view('arkady')['place_links'],[])

    def test_invalid_native_patch_preserves_wire_without_committing_knowledge(self):
        s=self.f.lives['garik'].lab.session;cid=s.prepare('garik',minutes=0,draw=1)['id']
        wire=native({**quiet(),'entities':[street()],'place_links':[]});wire['entities'][0]['learned_by']=['arkady']
        self.f.backends['garik'].answers=[wire]
        with self.assertRaisesRegex(ValueError,'current observer'):s.step(cid)
        self.assertEqual(self.store.cycle(cid)['value']['native_world_outputs']['world'],wire)
        self.assertNotIn('lesnaya',[e['id'] for e in self.store.state()['entities']])

    def test_each_observer_keeps_their_route_version_and_citable_source(self):
        with self.store.db:
            home=next(e for e in self.store.state()['entities'] if e['id']=='home');home['known_to']=['garik','arkady']
            self.store.db.execute('UPDATE world_entities SET value=? WHERE id=?',(json.dumps(home),'home'))
        def observe(actor,entity,link):
            s=self.f.lives[actor].lab.session;cid=s.prepare(actor,minutes=0,draw=1)['id']
            answer={**quiet(),'beat':'progress','domain':'city','visible_to':[actor],
                    'entities':[entity],'place_links':[link]}
            self.f.backends[actor].answers=[native(answer)];s.step(cid)
            # Only the world observation is under test; no physical act is fabricated.
            with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='complete',phase='done' WHERE id=?",(cid,))
            return cid+'-world'
        first=observe('garik',street(),route())
        old_state=self.store.state()
        second=observe('arkady',{**street(),'known_to':['arkady']},route(known_to=['arkady'],minutes=6,directions='Новый обход справа.'))
        g=self.store.actor_view('garik')['place_links'][0];a=self.store.actor_view('arkady')['place_links'][0]
        self.assertEqual((g['minutes'],g['source_id']),(4,first));self.assertEqual((a['minutes'],a['source_id']),(6,second))
        self.assertNotIn('Новый обход',str(self.store.actor_view('garik')))
        self.assertNotIn('_observer_views',str(self.store.actor_view('arkady')))
        from persona_runtime.channel_social import SocialMemory
        evidence=SocialMemory(self.store).with_source_ids({'life':self.store.actor_view('garik')},'garik')['_source_ids']
        self.assertIn(first,evidence);self.assertNotIn(second,evidence)
        rewind=self.store.state();rewind['revision']=old_state['revision']
        self.assertEqual(self.store.actor_view('arkady',rewind)['place_links'],[])
        third=observe('garik',street(),route(status='blocked'))
        self.assertEqual(self.store.actor_view('garik')['place_links'][0]['source_id'],third)
        self.assertEqual(self.store.actor_view('arkady')['place_links'][0]['status'],'open')
        frozen=self.store.actor_view('garik',old_state)['place_links'][0]
        self.assertEqual((frozen['source_id'],frozen['status']),(first,'open'))

    def test_waiting_visit_does_not_receive_a_route_from_the_future_outcome(self):
        from test_world import outcome
        s=self.f.lives['garik'].lab.session;cid=s.prepare('garik',minutes=0,draw=1)['id']
        self.f.backends['garik'].answers=[native({**quiet(),'beat':'progress','domain':'city','entities':[street()],'place_links':[route()]})]
        s.step(cid)
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='complete',phase='done' WHERE id=?",(cid,))
        source=cid+'-world'
        pending=self.f.plan('garik',5,'home');self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),pending))
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=5),'final_place_id':'home','place_links':[route(status='blocked')]}]
        s.step(pending)
        self.assertEqual(self.store.actor_view('garik')['place_links'][0]['status'],'blocked')
        visit={'actor_id':'garik','value':{'suspended_cycle_id':pending,'service_minute':self.store.get('minute')}}
        view=self.store.actor_view('garik',self.store.state_for_cycle(visit,self.store.state()))
        self.assertEqual(view['place_links'][0]['status'],'open');self.assertEqual(view['place_links'][0]['source_id'],source)
        self.assertNotIn('_geography_revision',view)

    def test_saved_invalid_legacy_map_can_be_discarded_only_on_explicit_pause(self):
        s=self.f.lives['garik'].lab.session;cid=s.prepare('garik',minutes=0,draw=1)['id']
        answer={**quiet(),'beat':'progress','domain':'city','entity_ids':['lesnaya'],
                'entities':[street()],'place_links':[route(known_to=['garik','arkady'])]}
        self.f.backends['garik'].answers=[answer]
        with self.assertRaises(ValueError):s.step(cid)
        original=copy.deepcopy(self.store.cycle(cid));calls=[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls')]
        positions=self.store.state()['positions']
        from persona_runtime.world import WorldConflict
        with self.assertRaises(WorldConflict):self.store.release_rejected_map_event(cid,'review')
        for life in self.f.lives.values():life.action(False)
        self.store.release_rejected_map_event(cid,'Discard invalid knowledge metadata; no model replay')
        self.store.release_rejected_map_event(cid,'idempotent')
        self.assertEqual(self.store.cycle(cid)['status'],'abandoned')
        self.assertEqual(self.store.cycle(cid)['value'],original['value'])
        self.assertEqual(calls,[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls')])
        self.assertEqual(self.store.state()['positions'],positions);self.assertEqual(self.store.state()['place_links'],[])
        self.assertEqual(len(self.store.get('rejected_map_reviews')),1)
