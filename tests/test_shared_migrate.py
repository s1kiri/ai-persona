"""Merge stopped histories with archival originals; never touch live sources or re-send old posts."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.channel_life import ChannelLife,fresh_start
from persona_runtime.profile_host import open_world,claim_setup
from persona_runtime.shared_host import open_shared_world
from persona_runtime.shared_migrate import migrate
from persona_runtime.world import WorldSession,WorldConflict
from persona_runtime.world_lab import Laboratory
from test_shared_life import PairAPI,ROOT
from test_telegram import Connection,TOKEN
from test_world import Backend
from test_world_personal import quiet


class SharedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.config={'version':1,'data_dir':self.root/'combined','port':0,'shared_places':['arkady_garage_yard'],'actors':[]}
        self.worlds={};self.telegrams={}
        for number,actor in enumerate(('arkady','garik'),1):
            p=CharacterProfile.load(ROOT/'characters'/actor/'character.json')
            world=self.root/('old-'+actor);telegram=self.root/('old-tg-'+actor)
            s=open_world(world,p);c=Connection(telegram,lambda token,n=number:PairAPI(token,n));c.configure(TOKEN);c.bind(c.api.channel['id']);claim_setup(c,actor)
            fresh_start(s,c,now=1800000000.,actor_id=actor)
            session=WorldSession(s,Backend(),{actor:p.persona},p.speech_contract)
            lab=Laboratory(s,session);life=ChannelLife(lab,c,p.persona,social_enabled=True)
            event=quiet();event.update(visible_to=[actor],entity_ids=['arkady_garage'] if actor=='arkady' else ['home'])
            with s.lock,s.db:
                s.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',('same',actor,'complete','done',s.get('revision'),json.dumps({'results':{'world':event},'inputs':{}}),1.))
                s.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',('same-world','same',actor,'world',10,json.dumps(event)))
                s.db.execute('INSERT INTO world_calls VALUES(?,?,?,?,?,?,?)',('same-call','same','world','completed','{"input_tokens":20}',1.,None))
                s.db.execute('INSERT INTO world_notes VALUES(?,?,?,?,?)',('same-note',actor,'same',10,'PRIVATE-'+actor))
                s.actor_put(actor,'social_review_complete',True)
                s.actor_put(actor,'life_controls',{**life.get('life_controls'),'sharing_appetite':87 if actor=='arkady' else 61})
                if actor=='garik':s.put('minute',s.get('minute')+100)
            c.close();s.close()
            self.worlds[actor]=world;self.telegrams[actor]=telegram
            self.config['actors'].append({'profile':p,'telegram_dir':self.root/('combined-tg-'+actor),'setup_port':0,'initial_place':'arkady_garage' if actor=='arkady' else 'home'})

    def test_plan_is_read_only_and_apply_preserves_both_histories_and_private_scopes(self):
        def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
        before={a:digest(p/'world.sqlite3') for a,p in self.worlds.items()}
        plan=migrate(self.config,self.worlds,self.telegrams)
        self.assertTrue(plan['ready']);self.assertFalse(plan['applied']);self.assertFalse(self.config['data_dir'].exists())
        result=migrate(self.config,self.worlds,self.telegrams,apply=True);self.assertTrue(result['applied'])
        self.assertEqual(before,{a:digest(p/'world.sqlite3') for a,p in self.worlds.items()})
        s=open_shared_world(self.config);self.addCleanup(s.close)
        self.assertEqual(len(s.state()['actors']),2)
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM world_calls').fetchone()[0],2)
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM world_notes').fetchone()[0],2)
        self.assertEqual(s.actor_get('arkady','life_controls')['sharing_appetite'],87)
        self.assertEqual(s.actor_get('garik','life_controls')['sharing_appetite'],61)
        self.assertIn('PRIVATE-garik',json.dumps(s.actor_view('garik')))
        self.assertNotIn('PRIVATE-arkady',json.dumps(s.actor_view('garik')))
        self.assertNotIn('PRIVATE-garik',json.dumps(s.actor_view('arkady')))
        imports=s.get('shared_imports');self.assertTrue(imports[1]['aliases']);self.assertNotEqual(imports[0]['clock_offset'],imports[1]['clock_offset'])
        self.assertTrue((s.directory/'imports/garik/original-world.sqlite3').is_file())
        self.assertIsNone(s.db.execute("SELECT place_id FROM peer_positions WHERE actor='arkady'").fetchone()[0])
        for item in self.config['actors']:
            c=Connection(item['telegram_dir'],lambda token:PairAPI(token,1));self.addCleanup(c.close)
            self.assertEqual(c.get('host_profile')['world'],str(s.directory))
            self.assertEqual(c.get('host_profile')['actor_id'],item['profile'].actor_id)

    def test_running_source_and_profile_mismatch_leave_destinations_absent(self):
        actor='arkady';s=open_world(self.worlds[actor],self.config['actors'][0]['profile'])
        try:
            with self.assertRaises(WorldConflict):migrate(self.config,self.worlds,self.telegrams,apply=True)
        finally:s.close()
        s=open_world(self.worlds[actor],self.config['actors'][0]['profile']);s.pause(False);s.close()
        with self.assertRaises(WorldConflict):migrate(self.config,self.worlds,self.telegrams,apply=True)
        self.assertFalse(self.config['data_dir'].exists())

    def test_pending_call_or_unknown_delivery_requires_explicit_review(self):
        import sqlite3
        with sqlite3.connect(self.worlds['arkady']/'world.sqlite3') as db:
            db.execute("UPDATE world_cycles SET status='ready',phase='experience'")
        with self.assertRaises(WorldConflict):migrate(self.config,self.worlds,self.telegrams,apply=True)
        self.assertFalse(self.config['data_dir'].exists())

    def test_legacy_export_requires_explicit_adoption_and_preserves_actual_voice(self):
        import sqlite3,subprocess,sys
        with sqlite3.connect(self.worlds['garik']/'world.sqlite3') as db:
            db.execute('UPDATE world_meta SET value=? WHERE key=?', ('{}','character_packages'))
        output=self.root/'private-garik'
        result=subprocess.run([sys.executable,'-m','persona_runtime.shared_export',
            '--world',str(self.worlds['garik']),'--persona',str(ROOT/'characters/garik/persona.md'),
            '--speech-contract',str(ROOT/'characters/garik/speech-contract.md'),'--output',str(output)],
            text=True,capture_output=True,check=True,cwd=ROOT,timeout=10)
        self.assertEqual(json.loads(result.stdout)['actor_id'],'garik')
        p=CharacterProfile.load(output/'character.json')
        self.assertEqual(p.persona,(ROOT/'characters/garik/persona.md').read_text())
        self.config['actors'][1]['profile']=p
        with self.assertRaises(WorldConflict):migrate(self.config,self.worlds,self.telegrams)
        result=migrate(self.config,self.worlds,self.telegrams,apply=True,adopt_legacy=['garik'])
        self.assertTrue(result['actors'][1]['adopt_legacy'])
        s=open_shared_world(self.config);self.addCleanup(s.close)
        self.assertIn('PRIVATE-garik',json.dumps(s.actor_view('garik')))


if __name__=='__main__':unittest.main()
