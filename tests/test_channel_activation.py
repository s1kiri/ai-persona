"""An existing fictional life can join Telegram without exporting its old drafts."""
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.channel_life import activate_existing_world, fresh_start, ChannelLife
from persona_runtime.character_profile import CharacterProfile
from persona_runtime.profile_host import open_world
from persona_runtime.world import WorldConflict, WorldSession
from persona_runtime.world_lab import Laboratory
from test_arkady_profile import MANIFEST
from test_channel_social import rich
from test_telegram import Connection, API, TOKEN, CHANNEL
from test_world import Backend


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.profile = CharacterProfile.load(MANIFEST)
        self.store = open_world(root / 'world', self.profile)
        self.addCleanup(self.store.close)
        self.connection = Connection(root / 'telegram', API)
        self.addCleanup(self.connection.close)
        self.connection.configure(TOKEN)
        self.connection.bind(CHANNEL['id'])
        with self.store.lock, self.store.db:
            self.store.put('minute', 83)
            self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                 ('past', 'arkady', 'complete', 'done', 0,
                                  json.dumps({'results': {}, 'inputs': {}}), 0))
            for index in range(16):
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                                     ('past-' + str(index), 'arkady', 'complete', 'done', 0,
                                      json.dumps({'results': {}, 'inputs': {}}), 0))
                self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                     ('draft-' + str(index), 'past-' + str(index), 'arkady', 'post', index,
                                      json.dumps({'text': 'Local draft ' + str(index), 'draft': True, 'source_ids': []})))

    def test_promotion_preserves_memory_time_and_drafts_without_sending(self):
        before = self.store.actor_view('arkady')
        activate_existing_world(self.store, self.connection, now=1000, actor_id='arkady')
        after = self.store.actor_view('arkady')
        self.assertEqual(after['memories'], before['memories'])
        self.assertEqual(after['concerns'], before['concerns'])
        self.assertEqual(self.store.get('minute'), 83)
        self.assertEqual(self.store.actor_get('arkady', 'channel_epoch'), {'wall': 1000, 'minute': 83})
        self.assertEqual(len(self.store.monitor()['posts']), 16)
        jobs = self.store.db.execute('SELECT status,receipts FROM channel_jobs').fetchall()
        self.assertEqual(len(jobs), 16)
        self.assertTrue(all(r['status'] == 'local_only' and r['receipts'] == '[]' for r in jobs))
        self.assertFalse(self.connection.state()['outbox'])
        backend = Backend({'people': [], 'intentions': []}, rich(decision='keep'))
        session = WorldSession(self.store, backend, {'arkady': self.profile.persona}, self.profile.speech_contract,
                               personal_life=True, active_world=True, autonomous_posts=True, direct_channel=True,
                               continuous=True, external_channel=True)
        life = ChannelLife(Laboratory(self.store, session), self.connection, self.profile.persona,
                            now=lambda: 1000, social_enabled=True)
        life.action(True)
        for _ in range(3):
            life.tick()
        self.assertEqual(life.state()['status'], 'running')
        self.assertFalse([c for c in self.connection.api.calls if c[0] == 'sendMessage'])
        self.assertEqual([item[0] for item in backend.inputs], ['social_review', 'live_channel'])
        with self.assertRaises(WorldConflict):
            activate_existing_world(self.store, self.connection, actor_id='arkady')

    def test_normal_fresh_start_still_rejects_existing_history(self):
        with self.assertRaises(WorldConflict):
            fresh_start(self.store, self.connection, actor_id='arkady')
        self.store.pause(False)
        with self.assertRaises(WorldConflict):
            activate_existing_world(self.store, self.connection, actor_id='arkady')
        self.assertIsNone(self.store.actor_get('arkady', 'channel_epoch'))

    def test_old_focused_comment_is_retrieved_for_its_own_actor(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                 ('old-comment', 'past', 'arkady', 'audience', 0,
                                  json.dumps({'post_id': 'draft-0', 'comments': [], 'simulated': False})))
            # Move the focused entry outside the moving recent window.
            self.store.db.execute('UPDATE world_events SET rowid=-1 WHERE id=?', ('old-comment',))
            self.store.actor_put('arkady', 'channel_attention', {'focus_ids': ['old-comment']})
        context = self.store.channel_context('arkady')
        self.assertIn('old-comment', [e['id'] for e in context])
        self.assertIn('draft-0', [e['id'] for e in context])
        self.assertNotIn('old-comment', [e['id'] for e in self.store.channel_context('garik')])


if __name__ == '__main__':
    unittest.main()
