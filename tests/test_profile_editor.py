"""Private author edits preserve lived state and survive normal host restart."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import test_comments_during_action as waiting
import test_shared_life as shared
from persona_runtime.profile_editor import edit_profile
from persona_runtime.profile_host import effective_profile
from persona_runtime.shared_host import load_config, identity
from persona_runtime.world import WorldConflict


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.case=waiting.WaitingCommentsTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.f=self.case.fixture;self.f.lab.profile=self.f.profile
        self.f.backend.close=Mock();self.f.life.action(False)

    def value(self, profile=None):
        p=profile or self.f.profile
        return {'digest':p.digest,'persona':p.persona+'\nАвторская добавка: замечай смешные мелочи.',
                'model':'synthetic-small-model','reasoning_effort':'none'}

    def apply(self,value=None):
        f=self.f
        return edit_profile(f.store,f.profile,value or self.value(),[f.connection],lives=[f.life])

    def test_edit_keeps_life_and_deadline_and_restart_uses_exact_private_revision(self):
        f=self.f;f.life.configure({'wake_minutes':5,'photos':False})
        pending=copy.deepcopy(f.store.cycle(self.case.cycle['id'])['value'])
        calls=copy.deepcopy(f.store.monitor()['calls']);memory=copy.deepcopy(f.store.actor_view('arkady')['memories'])
        before_text=(shared.ROOT/'characters/arkady/persona.md').read_text()
        updated=self.apply()
        self.assertEqual(f.store.cycle(self.case.cycle['id'])['value'],pending)
        self.assertEqual(f.store.monitor()['calls'],calls)
        self.assertEqual(f.store.actor_view('arkady')['memories'],memory)
        self.assertEqual(f.life.get('life_controls')['wake_minutes'],5)
        self.assertFalse(f.life.get('life_controls')['photos'])
        self.assertTrue(f.store.get('paused'))
        self.assertEqual(f.session.actor_prompts['arkady'],updated.persona)
        self.assertEqual(f.backend.model,'synthetic-small-model')
        self.assertEqual(f.backend.reasoning_effort,'none');f.backend.close.assert_called_once()
        self.assertEqual((shared.ROOT/'characters/arkady/persona.md').read_text(),before_text)
        actual=effective_profile(f.store.directory,f.profile);self.assertEqual(actual.digest,updated.digest)
        root=f.store.directory/f.store.get('profile_overrides')['arkady']['manifest']
        for path in (root.parent/'backup').glob('*.sqlite3'):
            with sqlite3.connect(path) as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        f.profile=actual;f.restart()
        self.assertEqual(f.session.actor_prompts['arkady'],updated.persona)
        self.assertEqual(f.store.cycle(self.case.cycle['id'])['value'],pending)
        self.assertEqual(f.store.monitor()['calls'],calls)

    def test_running_stale_and_invalid_requests_do_not_change_binding(self):
        f=self.f;before=f.store.get('host_profile')
        for changes, running in (({},True),({'digest':'stale'},False),({'model':'bad\nmodel'},False),({'persona':''},False)):
            f.life.action(running)
            with self.assertRaises((ValueError,WorldConflict)):
                self.apply({**self.value(),**changes})
            self.assertEqual(f.store.get('host_profile'),before)
            self.assertIsNone(f.store.get('profile_overrides'))

    def test_pointer_and_profile_commit_atomically(self):
        f=self.f;binding=f.store.get('host_profile');revision=f.store.get('revision');put=f.store.put
        def failed_pointer(key,value):
            if key=='profile_overrides':raise RuntimeError('Synthetic disk failure')
            return put(key,value)
        with patch.object(f.store,'put',side_effect=failed_pointer), self.assertRaises(RuntimeError):self.apply()
        self.assertEqual(f.store.get('host_profile'),binding)
        self.assertEqual(f.store.get('revision'),revision)
        self.assertIsNone(f.store.get('profile_overrides'))
        self.assertEqual(f.session.actor_prompts['arkady'],f.profile.persona)

    def test_owner_lock_does_not_wait_or_interrupt_active_call(self):
        self.f.life.active_lock.acquire()
        try:
            with self.assertRaises(WorldConflict):self.apply()
        finally:self.f.life.active_lock.release()


class SharedEditorTests(unittest.TestCase):
    def test_only_selected_profile_changes_with_parked_actions_and_restart(self):
        f=shared.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        plans=[f.plan('arkady',30),f.plan('garik',10)]
        saved={cid:copy.deepcopy(f.store.cycle(cid)['value']) for cid in plans}
        for life in f.lives.values():life.action(False);life.lab.session.backend.close=Mock()
        foreign=f.lives['garik'].persona;foreign_memory=copy.deepcopy(f.store.memories('garik'))
        previous=f.profiles['arkady'];value={'digest':previous.digest,'persona':previous.persona+'\nНовая интонация.',
            'model':'synthetic-small-model','reasoning_effort':'low'}
        updated=edit_profile(f.store,previous,value,f.connections.values(),lives=f.lives.values(),scheduler=f.scheduler)
        self.assertEqual(f.lives['garik'].persona,foreign)
        self.assertEqual(f.store.memories('garik'),foreign_memory)
        for cid in plans:self.assertEqual(f.store.cycle(cid)['value'],saved[cid])
        config=f.root/'config.json';config.write_text(json.dumps({'version':1,'port':0,'data_dir':str(f.store.directory),
            'shared_places':['arkady_garage_yard'],'actors':[{'profile':str(shared.ROOT/'characters'/a/'character.json'),
            'telegram_dir':str(f.connections[a].directory),'setup_port':0,'initial_place':None} for a in ('arkady','garik')]}))
        loaded=load_config(config);self.assertEqual(loaded['actors'][0]['profile'].digest,updated.digest)
        self.assertEqual(identity(loaded),f.store.get('shared_host'))
