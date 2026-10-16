"""Regression scenarios for importing real legacy lives and sharing local transport."""
from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest.mock import patch, Mock

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.profile_editor import edit_profile
from persona_runtime.profile_host import effective_profile, open_world
from persona_runtime.shared_export import main as export
from persona_runtime.shared_host import open_shared_world
from persona_runtime.shared_migrate import migrate
from persona_runtime.proxy import SharedTransport
from persona_runtime.telegram import BotAPI, TelegramError
import test_shared_migrate as migration_fixtures
import test_shared_life as life_fixtures
from test_shared_life import peer_answer, ROOT
from test_telegram import TOKEN, Opener


class MigrationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.f=migration_fixtures.SharedMigrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_actual_legacy_schema_exports_without_installing_new_tables(self):
        f=self.f;source=f.worlds['garik']/'world.sqlite3'
        with sqlite3.connect(source) as db:
            db.execute('DROP TABLE world_memories')
            db.execute("UPDATE world_meta SET value='{}' WHERE key='character_packages'")
        before=source.read_bytes();output=f.root/'export'
        with patch('builtins.print'):
            export(['--world',str(f.worlds['garik']),'--persona',str(ROOT/'characters/garik/persona.md'),
                    '--speech-contract',str(ROOT/'characters/garik/speech-contract.md'),'--output',str(output)])
        self.assertEqual(CharacterProfile.load(output/'character.json').memories,())
        self.assertEqual(source.read_bytes(),before)

    def test_nested_virtual_deadlines_shift_but_wall_deadlines_and_durations_do_not(self):
        f=self.f
        with sqlite3.connect(f.worlds['arkady']/'world.sqlite3') as db:
            db.execute("UPDATE world_meta SET value='500' WHERE key='minute'")
            thread={'id':'clock-thread','title':'Clock','domain':'leisure','status':'waiting','situation':'Waiting',
                    'open_question':'Later?','due_minute':650,'entity_ids':['arkady_garage'],'visible_to':['arkady']}
            hook={'id':'clock-hook','actor_ids':['arkady'],'anchor_ids':['arkady_garage'],'domain':'leisure',
                  'opens_minute':550,'closes_minute':900,'route':'Route','npc_want':'Want','friction':'Friction',
                  'temptation':'Temptation','exposed_to':[]}
            db.execute('INSERT INTO world_threads VALUES(?,?,?)',('clock-thread',json.dumps(thread),'same-world'))
            db.execute('INSERT INTO world_hooks VALUES(?,?,?)',('clock-hook',json.dumps(hook),'same-world'))
            value=json.loads(db.execute("SELECT value FROM world_cycles WHERE id='same'").fetchone()[0])
            value['inputs']={'clock':{'minute':500},'due':1800000010.,'duration_minutes':30}
            db.execute("UPDATE world_cycles SET value=? WHERE id='same'",(json.dumps(value),))
        with sqlite3.connect(f.worlds['garik']/'world.sqlite3') as db:
            db.execute("UPDATE world_meta SET value='1000' WHERE key='minute'")
        migrate(f.config,f.worlds,f.telegrams,apply=True)
        s=open_shared_world(f.config);self.addCleanup(s.close)
        self.assertEqual(next(t for t in s.state()['threads'] if t['id']=='clock-thread')['due_minute'],1150)
        hook=next(h for h in s.state()['hooks'] if h['id']=='clock-hook')
        self.assertEqual((hook['opens_minute'],hook['closes_minute']),(1050,1400))
        inputs=s.cycle('same')['value']['inputs']
        self.assertEqual(inputs,{'clock':{'minute':1000},'due':1800000010.,'duration_minutes':30})

    def test_both_private_edited_packages_survive_migration_and_restart_resolution(self):
        f=self.f;bases={}
        for item in f.config['actors']:
            p=item['profile'];bases[p.actor_id]=p;s=open_world(f.worlds[p.actor_id],p)
            try:
                updated=edit_profile(s,p,{'digest':p.digest,'persona':p.persona+'\nPrivate author detail.',
                                         'model':'gpt-6-luna','reasoning_effort':'medium'},())
            finally:s.close()
            item['profile']=updated
        migrate(f.config,f.worlds,f.telegrams,apply=True)
        for item in f.config['actors']:
            updated=effective_profile(f.config['data_dir'],bases[item['profile'].actor_id])
            self.assertEqual(updated.digest,item['profile'].digest)
            self.assertIn('Private author detail.',updated.persona)
        s=open_shared_world(f.config);self.addCleanup(s.close)
        self.assertEqual(len(s.get('profile_revisions')),2)


class PresenceRegressionTests(unittest.TestCase):
    def test_unknown_destination_does_not_allow_remote_face_to_face_speech(self):
        f=life_fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        with f.store.lock,f.store.db:f.store.db.execute("UPDATE peer_positions SET place_id='arkady_garage_yard'")
        f.plan('arkady',30,None,'Выйду и полчаса погуляю по улицам без определённой цели.')
        self.assertEqual(f.store.nearby('garik'),[])
        payload={'life':f.store.actor_view('garik'),'peer_context':f.store.peer_context('garik'),'available_source_ids':[]}
        with self.assertRaises(ValueError):
            f.scheduler.validate_peer('garik',peer_answer(decision='say',recipient_actor_id='arkady',text='Смотри!'),payload)


class PeerBoundaryRegressionTests(unittest.TestCase):
    def test_peer_speech_cannot_bypass_the_character_role_boundary(self):
        from persona_runtime.role_boundary import RoleBoundaryError
        f=life_fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        f.backends['garik'].answers=[peer_answer(decision='message',recipient_actor_id='arkady',
                                               text='Я ИИ-персонаж, а не настоящий Гарик.')]
        with self.assertRaises(RoleBoundaryError):f.scheduler.peer_turn('garik')
        self.assertFalse(f.store.db.execute('SELECT 1 FROM peer_jobs').fetchone())
        self.assertEqual(f.store.monitor()['calls'][-1]['status'],'failed')


class SharedTransportRegressionTests(unittest.TestCase):
    def test_system_proxy_bypass_cannot_disable_explicit_telegram_route(self):
        from urllib.request import Request
        from persona_runtime.telegram import LoopbackProxyHandler
        handler=LoopbackProxyHandler({'https':'http://127.0.0.1:12345'})
        request=Request('https://api.telegram.org/synthetic')
        with patch('urllib.request.proxy_bypass',return_value=True) as bypass:
            handler.proxy_open(request,'http://127.0.0.1:12345','https')
        bypass.assert_not_called()
        self.assertEqual(request.host,'127.0.0.1:12345')
        self.assertEqual(request._tunnel_host,'api.telegram.org')

    def test_both_bot_calls_borrow_one_bridge_without_direct_fallback(self):
        entered=[];exited=[];bridge=object()
        @contextmanager
        def transport(environment,timeout):
            entered.append(timeout)
            try:yield {'HTTPS_PROXY':'http://127.0.0.1:12345'},bridge
            finally:exited.append(True)
        owner=SharedTransport();self.addCleanup(owner.close)
        direct=Opener(error=AssertionError('direct request forbidden'))
        proxied=Opener({'ok':True,'result':{'id':1}})
        with patch('persona_runtime.proxy.transport',transport),patch('persona_runtime.telegram.build_opener',return_value=proxied) as build:
            bots=[BotAPI(TOKEN,direct,transport_factory=owner.borrow) for _ in range(2)]
            for api in bots:self.assertEqual(api.call('getChat',{'chat_id':-1})['id'],1)
            self.assertEqual(len(entered),1)
            self.assertFalse(exited)
            self.assertFalse(direct.requests)
            self.assertEqual([c.args[0].proxies for c in build.call_args_list],
                             [{'https':'http://127.0.0.1:12345'}]*2)
            owner.close();self.assertEqual(exited,[True])
            calls=len(proxied.requests)
            with self.assertRaises(TelegramError):bots[0].call('getChat',{'chat_id':-1})
            self.assertEqual(len(proxied.requests),calls)
            self.assertFalse(direct.requests)

    def test_shared_awake_assertion_is_one_global_process_and_reaches_both_panels(self):
        from persona_runtime.shared_host import shared_awake_guard
        f=life_fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        child=Mock(pid=12345);child.poll.return_value=None
        with patch('persona_runtime.channel_life.sys.platform','darwin'),patch('persona_runtime.channel_life.subprocess.Popen',return_value=child) as start:
            guard=shared_awake_guard(f.store,0)
            try:
                guard.tick();guard.tick()
                self.assertEqual(start.call_count,1)
                for life in f.lives.values():self.assertEqual(life.state()['awake']['pid'],12345)
            finally:guard.close()
            child.terminate.assert_called_once()


if __name__=='__main__':unittest.main()
