"""Offline central console, transport ownership and resuming accepted work."""
import copy
from http.cookiejar import CookieJar
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener,HTTPCookieProcessor,ProxyHandler,Request

from persona_runtime.shared_host import load_config,open_shared_world,verify_connections
from persona_runtime.shared_world import SharedWorldStore
from persona_runtime.telegram import TelegramError
from persona_runtime.world import WorldConflict
import test_shared_life as fixtures
from test_world import outcome
from test_world_personal import experience
from test_channel_social import rich


class SharedConsoleTests(unittest.TestCase):
    def test_offline_background_console_never_enables_model_or_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'shared.json'
            data={'version':1,'data_dir':str(root/'world'),'port':0,'shared_places':['arkady_garage_yard'],
                  'actors':[{'profile':str(fixtures.ROOT/'characters'/a/'character.json'),'telegram_dir':str(root/a),'setup_port':0,
                             'initial_place':'arkady_garage' if a=='arkady' else 'home'} for a in ('arkady','garik')]}
            path.write_text(json.dumps(data))
            result=subprocess.run([sys.executable,'-m','persona_runtime.shared_host','--config',str(path),'--init','--background'],
                                  cwd=fixtures.ROOT,text=True,capture_output=True,timeout=15,check=True)
            descriptor=json.loads(result.stdout);pid=descriptor['pid']
            try:
                client=build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()));base=descriptor['url']
                page=client.open(base,timeout=3).read().decode();self.assertIn('Общий мир',page)
                state=json.load(client.open(base+'/api/state',timeout=3))
                self.assertFalse(state['live']);self.assertTrue(state['paused'])
                self.assertEqual({a['id'] for a in state['actors']},{'arkady','garik'})
                self.assertTrue(all(not a['configured'] and a['life'] is None for a in state['actors']))
                unauthenticated=build_opener(ProxyHandler({}))
                with self.assertRaises(HTTPError) as error:unauthenticated.open(base+'/api/state')
                self.assertEqual(error.exception.code,401)
                request=Request(base+'/api/action',data=b'{"actor_id":"arkady","running":true}',method='POST',
                    headers={'Content-Type':'application/json','Origin':base,'X-Shared-CSRF':state['csrf']})
                with self.assertRaises(HTTPError) as error:client.open(request)
                self.assertEqual(error.exception.code,409)
                with self.assertRaises(WorldConflict):open_shared_world(load_config(path))
            finally:
                import os,signal,time
                os.kill(pid,signal.SIGTERM)
                for _ in range(100):
                    try:
                        store=SharedWorldStore(root/'world');store.close();break
                    except WorldConflict:time.sleep(.1)
                else:self.fail('shared host did not release its world')
            store=open_shared_world(load_config(path));self.addCleanup(store.close)
            self.assertFalse(store.monitor()['calls']);self.assertFalse(store.monitor()['posts'])


class SharedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_waiting_restart_resolves_once_against_current_entities(self):
        f=self.f;cid=f.plan('arkady',10);before=len(f.backends['arkady'].inputs)
        f.store.close();f.store=SharedWorldStore(f.root/'world')
        f.scheduler.store=f.store
        for life in f.lives.values():
            life.store=f.store;life.lab.store=f.store;life.lab.session.store=f.store
            life.lab.session.timeline.store=f.store;life.social.store=f.store;life.social.gallery.store=f.store
        with f.store.lock,f.store.db:
            row=f.store.db.execute("SELECT value FROM world_entities WHERE id='arkady_workbench'").fetchone()
            entity=json.loads(row[0]);entity['condition']='CURRENT-AFTER-OTHER-ACTOR'
            f.store.db.execute("UPDATE world_entities SET value=? WHERE id='arkady_workbench'",(json.dumps(entity),))
            f.store.put('revision',f.store.get('revision')+1)
        f.now+=600;f.finish('arkady',cid,10)
        payload=next(p for role,_,p in f.backends['arkady'].inputs if role=='outcome')
        self.assertIn('CURRENT-AFTER-OTHER-ACTOR',json.dumps(payload))
        self.assertEqual(len(f.backends['arkady'].inputs),before+3)
        self.assertEqual(sum(role=='outcome' for role,_,_ in f.backends['arkady'].inputs),1)

    def test_blocked_actor_does_not_prevent_other_physical_choice(self):
        f=self.f
        f.backends['garik'].answers=[RuntimeError('synthetic failure')]
        f.lives['garik'].get('life_status')
        with f.store.lock,f.store.db:
            status=f.lives['garik'].get('life_status');status['next_wake']=f.now;f.lives['garik'].put('life_status',status)
        f.scheduler.tick();f.scheduler.tick()
        self.assertEqual(f.lives['garik'].get('life_status')['status'],'error')
        cid=f.plan('arkady',3)
        self.assertEqual(f.store.cycle(cid)['status'],'waiting')

    def test_peer_unknown_absence_repair_requires_paused_owner_and_new_delivery_key(self):
        f=self.f;post=f.root_post('arkady');f.forward('arkady',post);f.scheduler.circulate_posts()
        source=f.store.peer_context('garik')['events'][0]['id']
        f.backends['garik'].answers=[fixtures.peer_answer(decision='comment',recipient_actor_id='arkady',text='Repeat only after owner review.',post_id=post,source_ids=[source])]
        f.scheduler.peer_turn('garik');f.connections['garik'].api.fail_send=True;f.scheduler.tick()
        row=f.store.db.execute('SELECT * FROM peer_jobs').fetchone();self.assertEqual(row['status'],'unknown')
        with self.assertRaises(TelegramError):f.scheduler.confirm_peer_absent('arkady',row['id'])
        f.scheduler.confirm_peer_absent('garik',row['id']);self.assertEqual(f.lives['garik'].get('life_status')['status'],'paused')
        f.connections['garik'].api.fail_send=False;f.lives['garik'].action(True);f.scheduler.tick()
        outbox=f.connections['garik'].state()['outbox']
        self.assertEqual({o['status'] for o in outbox},{'sent','unknown'})
        self.assertTrue(any(':review1' in o['key'] for o in outbox))

    def test_own_and_peer_views_never_expose_other_positions_or_private_canon(self):
        f=self.f;view=f.store.actor_view('garik')
        self.assertNotIn('positions',view);self.assertEqual(set(view['basis']),{'garik'})
        self.assertNotIn('arkady_memory_body',{m['id'] for m in view['memories']})
        self.assertNotIn('arkady_garage',{e['id'] for e in view['entities']})

    def test_independent_pause_holds_work_without_discarding_it(self):
        f=self.f;cid=f.plan('arkady',10);value=copy.deepcopy(f.store.cycle(cid)['value'])
        f.lives['arkady'].action(False);self.assertFalse(f.store.get('paused'))
        f.now+=600
        for _ in range(3):f.scheduler.tick()
        self.assertEqual(f.store.cycle(cid)['value'],value);self.assertEqual(f.store.cycle(cid)['status'],'waiting')
        f.lives['arkady'].action(True);f.finish('arkady',cid,10)


if __name__=='__main__':unittest.main()
