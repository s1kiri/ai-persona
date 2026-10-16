"""Settings HTTP requests are scoped, authenticated and never start a model or a send."""
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

import test_profile_runtime as standalone
import test_shared_life as shared
from persona_runtime.shared_host import SharedPanel, panel_handler
from persona_runtime.world_lab import handler


class PanelSettingsTests(unittest.TestCase):
    def serve(self, serve):
        server=ThreadingHTTPServer(('127.0.0.1',0),serve)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        def close():server.shutdown();thread.join(3);server.server_close()
        self.addCleanup(close)
        base='http://127.0.0.1:'+str(server.server_port)
        client=build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()));client.open(base).close()
        return base,client

    def post(self,base,client,path,value,header,csrf):
        return json.load(client.open(Request(base+path,json.dumps(value).encode(),
            {'Origin':base,'Content-Type':'application/json',header:csrf})))

    def test_standalone_actor_settings_and_private_author_edit_have_distinct_routes(self):
        f=standalone.ArkadyLifeTests();f.setUp();self.addCleanup(f.tearDown)
        f.initial_post();f.life.action(False);f.lab.profile=f.profile;f.backend.close=Mock()
        base,client=self.serve(handler(f.lab));state=json.load(client.open(base+'/api/state'))
        actors=state['character_settings'];self.assertEqual([a['id'] for a in actors],['arkady','garik'])
        self.assertTrue(actors[0]['editable']);self.assertFalse(actors[1]['editable'])
        self.assertEqual(actors[0]['profile']['persona'],f.profile.persona)
        controls=f.life.get('life_controls')
        self.post(base,client,'/api/character-settings',{'actor_id':'arkady','controls':{'photos':False}},'X-World-CSRF',f.lab.csrf)
        self.assertFalse(f.life.get('life_controls')['photos'])
        self.assertEqual(f.life.get('life_controls')['wake_minutes'],controls['wake_minutes'])
        with self.assertRaises(HTTPError) as error:
            self.post(base,client,'/api/character-settings',{'actor_id':'garik','controls':{'photos':False}},'X-World-CSRF',f.lab.csrf)
        self.assertEqual(error.exception.code,409)
        calls=f.store.monitor()['call_count'];outbox=len(f.connection.state()['outbox'])
        edited=self.post(base,client,'/api/character-profile',{'actor_id':'arkady','profile':{
            'digest':f.profile.digest,'persona':f.profile.persona+'\nНовая авторская строка.',
            'model':'synthetic-small-model','reasoning_effort':'low'}},'X-World-CSRF',f.lab.csrf)
        self.assertIn('Новая авторская строка.',edited['character_settings'][0]['profile']['persona'])
        self.assertEqual(edited['character_settings'][0]['model'],'synthetic-small-model')
        self.assertEqual(edited['call_count'],calls);self.assertEqual(len(f.connection.state()['outbox']),outbox)
        self.assertTrue(edited['paused'])

    def test_shared_preparation_has_separate_world_and_actor_settings_and_author_edits(self):
        f=shared.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        for life in f.lives.values():life.action(False)
        panel=SharedPanel(f.store,f.config,f.connections)
        base,client=self.serve(panel_handler(panel));state=json.load(client.open(base+'/api/state'))
        current=f.store.get('settings');controls={a:f.store.actor_get(a,'life_controls') for a in ('arkady','garik')}
        self.post(base,client,'/api/character-settings',{'actor_id':'garik','controls':{'photos':False}},'X-Shared-CSRF',panel.csrf)
        self.assertEqual(f.store.actor_get('arkady','life_controls'),controls['arkady'])
        self.assertFalse(f.store.actor_get('garik','life_controls')['photos'])
        self.assertEqual(f.store.get('settings'),current)
        world={**current,'surprise':20}
        self.post(base,client,'/api/settings',{'settings':world,'revision':state['settings_revision']},'X-Shared-CSRF',panel.csrf)
        self.assertEqual(f.store.get('settings'),world)
        with self.assertRaises(HTTPError):
            self.post(base,client,'/api/settings',{'settings':current,'revision':state['settings_revision']},'X-Shared-CSRF',panel.csrf)
        previous=f.profiles['garik']
        self.post(base,client,'/api/character-profile',{'actor_id':'garik','profile':{'digest':previous.digest,
            'persona':previous.persona+'\nАвторская строка.','model':'synthetic-model','reasoning_effort':'none'}},'X-Shared-CSRF',panel.csrf)
        self.assertEqual(f.store.get('shared_host')['profiles']['arkady'],f.profiles['arkady'].digest)
        self.assertNotEqual(f.store.get('shared_host')['profiles']['garik'],previous.digest)
        self.assertFalse(f.store.monitor()['calls'])
        self.assertTrue(all(not c.state()['outbox'] for c in f.connections.values()))
        headers={'Sec-Fetch-Site':'same-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
        self.assertEqual(client.open(Request(base+'/',headers=headers)).status,200)
        with self.assertRaises(HTTPError) as error:client.open(Request(base+'/api/state',headers=headers))
        self.assertEqual(error.exception.code,403)
