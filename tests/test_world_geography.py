"""Concrete fictional geography persists, with per-actor knowledge and atomic commits."""
import copy
import json
import unittest
import test_shared_life as fixtures
from test_world import outcome
from test_world_personal import quiet
from persona_runtime.shared_world import SharedWorldStore
from persona_runtime.world_geography import validate
from persona_runtime.world_backend import WorldBackend
from persona_runtime.world_protocol import SCHEMAS
from test_world_backend import Client


def street():
    return {'id':'lesnaya','kind':'place','name':'Лесная улица',
        'description':'Вымышленная улица российского города.','condition':'Открыта для пешеходов.',
        'known_to':['garik'],'world_note':'Private planning detail.'}


def route(**updates):
    value={'id':'home_lesnaya','from_id':'home','to_id':'lesnaya',
        'directions':'Из подъезда налево, через арку к табличке «Лесная».','minutes':4,
        'bidirectional':True,'status':'open','known_to':['garik']}
    value.update(updates);return value


class GeographyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store

    def discovery(self,**updates):
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':6},self.store.get('settings_revision'))
        session=self.f.lives['garik'].lab.session;cid=session.prepare('garik',minutes=10,draw=0)['id']
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET phase='world' WHERE id=?",(cid,))
        answer={**quiet(),'beat':'discovery','pattern':'street_sign','observation':'На табличке написано «Лесная». От дома сюда ведёт арка.',
                'entities':[street()],'place_links':[route()],**updates}
        self.f.backends['garik'].answers=[answer];session.step(cid);return cid

    def test_new_street_and_route_persist_after_restart_and_history_eviction(self):
        cid=self.discovery()
        with self.store.db:
            for n in range(40):
                later='later'+str(n)
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',(later,'garik','complete','done',0,'{"results":{},"inputs":{}}',0))
                self.store.db.execute("INSERT INTO world_events VALUES(?,?,?,?,?,?)",(later,later,'garik','peer',800+n,'{}'))
        path=self.store.directory;self.store.close()
        self.store=self.f.store=SharedWorldStore(path)
        view=self.store.actor_view('garik');link=view['place_links'][0]
        self.assertEqual(link['directions'],route()['directions']);self.assertEqual(link['source_id'],cid+'-world')
        self.assertEqual(next(e['name'] for e in view['entities'] if e['id']=='lesnaya'),'Лесная улица')
        self.assertNotIn(cid+'-world',[e['id'] for e in view['history']])
        self.assertEqual(self.store.actor_view('arkady')['place_links'],[])
        self.assertEqual(self.store.cycle(cid)['value']['geography_proposals']['world'],[route()])
        from persona_runtime.channel_social import SocialMemory
        social=SocialMemory(self.store)
        payload=social.with_source_ids({'life':view},'garik')
        self.assertIn(cid+'-world',payload['_source_ids'])
        self.assertNotIn(cid+'-world',social.with_source_ids({'life':self.store.actor_view('arkady')},'arkady')['_source_ids'])

    def test_routes_do_not_move_anyone_or_disclose_other_actor_places(self):
        before=self.store.state()['positions'];self.discovery()
        self.assertEqual(self.store.state()['positions'],before)
        self.assertNotIn('lesnaya',[e['id'] for e in self.store.actor_view('arkady')['entities']])
        self.assertNotIn('Private planning detail.',str(self.store.actor_view('garik')))

    def test_unknown_endpoint_or_observer_or_knowledge_rejects_before_commit(self):
        for patch in [route(to_id='nowhere'),route(known_to=['arkady']),route(known_to=['unregistered']),route(to_id='home')]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                validate([patch],self.store.state(),[street()])
        self.assertEqual(self.store.state()['place_links'],[])

    def test_duplicate_and_changed_route_identity_reject(self):
        with self.assertRaises(ValueError):validate([route(),route()],self.store.state(),[street()])
        self.discovery()
        with self.assertRaisesRegex(ValueError,'identity cannot change'):
            validate([route(from_id='lesnaya',to_id='home')],self.store.state(),[])

    def test_familiar_places_do_not_give_a_distant_actor_the_new_route(self):
        # Both know the endpoints, but only Garik observes this particular path.
        with self.store.db:
            home=next(e for e in self.store.state()['entities'] if e['id']=='home')
            home['known_to']=['garik','arkady']
            self.store.db.execute('UPDATE world_entities SET value=? WHERE id=?',(json.dumps(home),'home'))
        from persona_runtime.world_geography import extract
        cid=self.f.lives['garik'].lab.session.prepare('garik',minutes=0,draw=1)['id']
        answer={**quiet(),'entities':[{**street(),'known_to':['garik','arkady']}],
                'place_links':[route(known_to=['garik','arkady'])]}
        with self.assertRaisesRegex(ValueError,'needs an observer'):
            extract(answer,{'world':self.store.state()},self.store,self.store.cycle(cid),'world')
        self.assertEqual(self.store.state()['place_links'],[])

    def test_bad_main_event_does_not_apply_valid_geography(self):
        bad=copy.deepcopy(next(e for e in self.store.state()['entities'] if e['id']=='home'));bad['description']='Unauthorized rewrite.'
        with self.assertRaisesRegex(ValueError,'author-established'):self.discovery(entities=[street(),bad])
        self.assertEqual(self.store.state()['place_links'],[])
        self.assertNotIn('lesnaya',[e['id'] for e in self.store.state()['entities']])
        cid=self.store.db.execute("SELECT id FROM world_cycles WHERE status='blocked'").fetchone()[0]
        self.assertEqual(self.store.cycle(cid)['value']['geography_proposals']['world'],[route()])

    def test_bad_timing_rolls_back_routes_and_places_with_outcome(self):
        cid=self.f.plan('garik',5,'home');self.f.now+=5*60;self.f.scheduler.advance()
        with self.store.db:self.store.db.execute("UPDATE world_cycles SET status='ready',revision=? WHERE id=?",(self.store.get('revision'),cid))
        self.f.backends['garik'].answers=[{**outcome(elapsed_minutes=10,entities=[street()]),'place_links':[route()],'final_place_id':'home'}]
        with self.assertRaisesRegex(ValueError,'elapsed wall time'):self.f.lives['garik'].lab.session.step(cid)
        self.assertEqual(self.store.state()['place_links'],[])
        self.assertNotIn('lesnaya',[e['id'] for e in self.store.state()['entities']])

    def test_native_schema_requires_routes_without_mutating_core_schema(self):
        b=WorldBackend(self.f.root/'native');b._start=lambda:None;b.workspace=self.f.root;b.overrides={};b.client=Client(quiet())
        b.generate('world','test',{})
        wire=next(p['outputSchema'] for m,p in b.client.calls if m=='turn/start')
        self.assertIn('place_links',wire['required']);self.assertNotIn('place_links',SCHEMAS['world']['properties'])

    def test_plain_geography_does_not_require_an_incident_slot(self):
        session=self.f.lives['garik'].lab.session;cid=session.prepare('garik',minutes=0,draw=1)['id']
        answer={**quiet(),'beat':'progress','domain':'city','entities':[street()],'place_links':[route()]}
        self.f.backends['garik'].answers=[answer];session.step(cid)
        self.assertEqual(self.store.actor_view('garik')['place_links'][0]['to_id'],'lesnaya')

    def test_geographic_exception_cannot_add_a_character_or_story(self):
        from persona_runtime.world_protocol import validate_result
        from test_shared_world_validation import thread
        session=self.f.lives['garik'].lab.session;cid=session.prepare('garik',minutes=0,draw=1)['id']
        base={**quiet(),'beat':'progress','domain':'city','entities':[street()]}
        for changes in ({'entities':[{**street(),'kind':'person'}]}, {'threads':[thread('new_story',['garik'])]}, {'surprise':1}):
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'new incidents are not due'):
                validate_result('world',{**base,**changes},self.store.state(),self.store.cycle(cid)['value']['brief'])
