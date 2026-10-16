"""Live profile activation preserves in-flight work and restart continuity."""
import json
import unittest

from persona_runtime.channel_life import ChannelLife
from persona_runtime.world_hooks import pending_hooks
import test_channel_life as fixtures
import test_world_active as world_fixtures


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LifeTests()
        self.fixture.setUp()
        self.life, self.store = self.fixture.life, self.fixture.store

    def tearDown(self):
        self.fixture.tearDown()

    def test_switch_waits_for_old_cycle_and_keeps_exposed_stories(self):
        self.life.tick()
        cycle = self.store.monitor()['cycles'][0]
        before = self.store.cycle(cycle['id'])
        minute = self.store.get('minute')
        hooks = [world_fixtures.hook('unseen'), world_fixtures.hook('seen')]
        for hook in hooks:
            hook.update(opens_minute=minute, closes_minute=minute+300, exposed_to=[])
        hooks[1]['exposed_to'] = ['garik']
        with self.store.lock, self.store.db:
            for hook in hooks:
                self.store.db.execute('INSERT INTO world_hooks VALUES(?,?,?)', (hook['id'], json.dumps(hook), 'fixture'))
        self.life.request_world_profile(True)
        self.life.sync_world_profile()
        self.assertEqual(self.store.cycle(cycle['id']), before)
        self.assertEqual(self.life.state()['world_profile'], {'active': False, 'requested': True})
        self.assertFalse(self.store.get('world_profile_history'))
        self.fixture.backend.answers = [{'decision': 'keep', 'text': '', 'source_ids': []}]
        self.life.tick()
        self.assertEqual(self.store.cycle(cycle['id'])['status'], 'complete')
        self.life.sync_world_profile()
        self.assertEqual(self.life.state()['world_profile'], {'active': True, 'requested': True})
        saved = {h['id']: h for h in self.store.state()['hooks']}
        self.assertTrue(saved['unseen']['superseded'])
        self.assertEqual(saved['seen'], hooks[1])
        self.assertFalse(pending_hooks(self.store.state(), 'garik'))
        self.assertEqual(len(self.fixture.backend.inputs), 1)
        self.assertFalse(self.fixture.connection.state()['outbox'])
        new = self.fixture.session.prepare(minutes=0, draw=0)
        self.assertEqual(new['phase'], 'horizon')
        self.assertTrue(new['value']['lively_world'])
        self.assertTrue(new['value']['brief']['plan_directions'])

    def test_restart_keeps_requested_mode_and_does_not_supersede_new_hooks(self):
        self.life.request_world_profile(True)
        self.life.sync_world_profile()
        minute = self.store.get('minute')
        hook = {**world_fixtures.hook('new'), 'opens_minute': minute, 'closes_minute': minute+300,
                'exposed_to': [], 'mechanism': 'chance'}
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO world_hooks VALUES(?,?,?)', (hook['id'], json.dumps(hook), 'fixture'))
        self.fixture.session.lively_world = False  # CLI constructor's original default.
        restored = ChannelLife(self.fixture.lab, self.fixture.connection, 'Synthetic persona', now=lambda: self.fixture.now)
        self.assertEqual(restored.state()['world_profile'], {'active': True, 'requested': True})
        self.assertEqual(pending_hooks(self.store.state(), 'garik'), [hook])
        self.assertEqual(len(self.store.get('world_profile_history')), 1)


if __name__ == '__main__':
    unittest.main()
