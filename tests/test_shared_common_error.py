import sqlite3
import unittest
from unittest.mock import patch
import test_shared_life as fixtures
from persona_runtime.telegram import TelegramError


class CommonErrorTests(unittest.TestCase):
    def test_shared_failure_is_visible_and_cannot_be_cleared_by_actor_unpause(self):
        f=fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        with patch.object(f.scheduler,'advance',side_effect=sqlite3.InterfaceError('SECRET-MUST-NOT-BE-LOGGED')):
            f.scheduler.tick()
        self.assertTrue(f.store.get('paused'))
        for life in f.lives.values():
            self.assertEqual(life.get('life_status')['status'],'running')
            state=life.state();self.assertEqual(state['status'],'error');self.assertTrue(state['paused'])
            self.assertIn('InterfaceError',state['error'])
            with self.assertRaises(TelegramError):life.action(True)
        diagnostic=f.store.get('shared_error_diagnostics')
        self.assertEqual(diagnostic['type'],'InterfaceError');self.assertTrue(diagnostic['frames'])
        self.assertNotIn('SECRET',str(diagnostic))
        self.assertFalse(f.store.db.execute('SELECT 1 FROM world_calls').fetchone())
