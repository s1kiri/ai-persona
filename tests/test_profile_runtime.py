"""Standalone Arkady hosts, private author memory and fake Telegram delivery."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler
from http.cookiejar import CookieJar

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.channel_life import ChannelLife, fresh_start
from persona_runtime.channel_life import main as live_main
from persona_runtime.channel_social import SocialMemory
from persona_runtime.profile_host import open_world, bind_connection, claim_setup
from persona_runtime.telegram_setup import Setup
from persona_runtime.telegram import TelegramError
from persona_runtime.world import WorldStore, WorldSession, WorldConflict
from persona_runtime.world_lab import Laboratory
from test_arkady_profile import MANIFEST
from test_channel_social import rich
from test_telegram import Connection, API, TOKEN, CHANNEL, GROUP, comment, forward
from test_world import Backend, choice, outcome
from test_world_personal import quiet, experience


class ProfileHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = CharacterProfile.load(MANIFEST)

    def test_seed_memory_is_private_and_not_a_new_outcome(self):
        store = WorldStore(self.root / 'world')
        self.addCleanup(store.close)
        self.profile.install(store)
        own = store.actor_view('arkady')
        self.assertEqual(len(own['memories']), 8)
        self.assertEqual(len(own['concerns']), 3)
        self.assertTrue(all(m['provenance'] == 'author_backstory' for m in own['memories']))
        self.assertFalse(store.state()['history'])
        self.assertEqual(store.actor_view('garik')['memories'], [])
        social = SocialMemory(store)
        self.assertIn('arkady_memory_garik', social.evidence('arkady'))
        self.assertNotIn('arkady_memory_garik', social.evidence('garik'))
        with self.assertRaises(ValueError):
            social.sources(['arkady_memory_body'], social.evidence('arkady'), outcome=True)

    def test_restart_keeps_mutated_memory_and_pending_cycle_without_reimport(self):
        directory = self.root / 'world'
        store = open_world(directory, self.profile)
        try:
            with store.lock, store.db:
                store.db.execute("UPDATE world_concerns SET value=json_set(value,'$.status','closed') WHERE id='friendship'")
            store.pause(False)
            session = WorldSession(store, Backend(), {'arkady': self.profile.persona}, self.profile.speech_contract)
            cycle = session.prepare('arkady')
        finally:
            store.close()
        store = open_world(directory, self.profile)
        self.addCleanup(store.close)
        self.assertEqual(store.cycle(cycle['id'])['status'], 'ready')
        self.assertEqual(next(c for c in store.actor_view('arkady')['concerns'] if c['id'] == 'friendship')['status'], 'closed')
        with self.assertRaises(WorldConflict):
            Laboratory(store).prepare('garik', 10)

    def test_host_rejects_wrong_world_or_changed_profile(self):
        directory = self.root / 'world'
        open_world(directory).close()
        with self.assertRaises(WorldConflict):
            open_world(directory, self.profile)
        directory = self.root / 'arkady'
        open_world(directory, self.profile).close()
        package = self.root / 'package'
        shutil.copytree(MANIFEST.parent, package)
        (package / 'persona.md').write_text(self.profile.persona + '\nChanged.')
        with self.assertRaises(WorldConflict):
            open_world(directory, CharacterProfile.load(package / 'character.json'))

    def test_shared_observation_is_evidence_but_foreign_diary_stays_private(self):
        store = WorldStore(self.root / 'shared')
        self.addCleanup(store.close)
        self.profile.install(store)
        with store.lock, store.db:
            store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                             ('shared', 'garik', 'complete', 'done', 0, '{}', 0))
            for stage, value in [('world', {'observation': 'Видно обоим.', 'visible_to': ['garik', 'arkady'],
                                           'world_note': 'PRIVATE-BACKSTAGE'}),
                                 ('experience', {'note': 'PRIVATE-GARIK-DIARY'})]:
                store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                                 ('shared-' + stage, 'shared', 'garik', stage, 0, json.dumps(value)))
        evidence = SocialMemory(store).evidence('arkady')
        self.assertIn('shared-world', evidence)
        self.assertNotIn('shared-experience', evidence)
        self.assertNotIn('PRIVATE-', json.dumps(evidence))

    def test_own_setup_test_post_does_not_prevent_starting_profile_life(self):
        connection = Connection(self.root / 'telegram', API)
        self.addCleanup(connection.close)
        connection.configure(TOKEN)
        connection.bind(CHANNEL['id'])
        claim_setup(connection, 'arkady')
        setup = Setup(connection, profile=self.profile)
        self.assertIn('Аркадий', setup.test_post)
        self.assertNotIn('Гарик', setup.test_post)
        connection.send('setup-text-v1', setup.test_post)
        bind_connection(connection, 'arkady', self.root / 'life')
        self.assertEqual(setup.state()['actor_name'], 'Аркадий Кедров')
        with self.assertRaises(TelegramError):
            claim_setup(connection, 'garik')

    def test_memory_path_and_sources_cannot_escape_package_or_actor(self):
        package = self.root / 'package'
        shutil.copytree(MANIFEST.parent, package)
        path = package / 'memory.json'
        value = json.loads(path.read_text())
        value['concerns'][0]['source_ids'] = ['garik_private_note']
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            CharacterProfile.load(package / 'character.json')
        manifest = json.loads((package / 'character.json').read_text())
        manifest['memory'] = '../outside.json'
        (self.root / 'outside.json').write_text('{}')
        (package / 'character.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            CharacterProfile.load(package / 'character.json')

    def test_visual_references_are_portable_bounded_and_actor_owned(self):
        from test_world_image_backend import PNG
        package = self.root / 'package'
        shutil.copytree(MANIFEST.parent, package)
        (package / 'portrait.png').write_bytes(PNG)
        (package / 'references.json').write_text(json.dumps({'arkady': 'portrait.png'}))
        path = package / 'character.json'
        manifest = json.loads(path.read_text())
        manifest['visual_references'] = 'references.json'
        path.write_text(json.dumps(manifest))
        original = CharacterProfile.load(path)
        relocated = self.root / 'relocated'
        shutil.copytree(package, relocated)
        moved = CharacterProfile.load(relocated / 'character.json')
        self.assertEqual(original.digest, moved.digest)
        self.assertEqual(Path(moved.references['arkady']), (relocated / 'portrait.png').resolve())
        (package / 'references.json').write_text(json.dumps({'garik': 'portrait.png'}))
        with self.assertRaises(ValueError):
            CharacterProfile.load(path)
        (package / 'references.json').write_text(json.dumps({'arkady': '../outside.png'}))
        (self.root / 'outside.png').write_bytes(PNG)
        with self.assertRaises(ValueError):
            CharacterProfile.load(path)

    def test_offline_cli_starts_arkady_panel_with_history_and_no_model(self):
        directory = self.root / 'lab'
        process = subprocess.Popen([sys.executable, '-m', 'persona_runtime.world_lab',
                                    '--profile', str(MANIFEST), '--data-dir', str(directory), '--port', '0'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (directory / 'lab.json').exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('profile laboratory did not start')
                time.sleep(.02)
            descriptor = json.loads((directory / 'lab.json').read_text())
            client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            origin = 'http://127.0.0.1:' + str(descriptor['port'])
            html = client.open(origin, timeout=3).read().decode()
            state = json.loads(client.open(origin + '/api/state', timeout=3).read())
            self.assertIn('origin-memory', html)
            self.assertEqual(state['actor_id'], 'arkady')
            self.assertEqual(state['actor_name'], 'Аркадий Кедров')
            self.assertEqual([a['id'] for a in state['actors']], ['arkady'])
            self.assertEqual(len(state['memories']), 8)
            self.assertFalse(state['model_enabled'])
            self.assertEqual(state['call_count'], 0)
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_live_cli_selects_profile_and_actual_ports_without_model_or_delivery(self):
        project = MANIFEST.parents[2]
        with tempfile.TemporaryDirectory(dir=project / '.local', prefix='profile-cli-') as directory:
            directory = Path(directory)
            def connection_factory(path):
                connection = Connection(path, API)
                connection.configure(TOKEN)
                connection.bind(CHANNEL['id'])
                return connection
            with patch('persona_runtime.channel_life.TelegramConnection', side_effect=connection_factory), \
                 patch('persona_runtime.telegram.TelegramConnection.start'), \
                 patch('persona_runtime.proxy.SharedTransport') as transport, \
                 patch('persona_runtime.world_backend.WorldBackend') as model, \
                 patch('persona_runtime.world_image_backend.CodexImageBackend') as image, \
                 patch.object(ChannelLife, 'loop', lambda life, stopping: stopping.set()), \
                 patch('persona_runtime.channel_life.signal.signal'), patch('builtins.print'):
                live_main(['--profile', str(MANIFEST), '--data-dir', str(directory / 'life'),
                           '--telegram-dir', str(directory / 'telegram'), '--fresh',
                           '--world-port', '0', '--telegram-port', '0'])
                model.return_value.generate.assert_not_called()
                image.return_value.render.assert_not_called()
                for call in [*model.call_args_list, *image.call_args_list]:
                    self.assertEqual(call.kwargs['model'], 'gpt-6-luna')
                    self.assertEqual(call.kwargs['reasoning_effort'], 'low')
                transport.return_value.close.assert_called_once()
            descriptor = json.loads((directory / 'life/live.json').read_text())
            self.assertEqual(descriptor['actor_id'], 'arkady')
            self.assertEqual(descriptor['name'], 'Аркадий Кедров')
            self.assertNotEqual(descriptor['world_url'], descriptor['telegram_url'])
            store = open_world(directory / 'life', self.profile)
            try:
                self.assertEqual(len(store.actor_view('arkady')['memories']), 8)
                self.assertTrue(store.actor_get('arkady', 'channel_epoch'))
                self.assertFalse(store.monitor()['calls'])
            finally:
                store.close()


class ArkadyLifeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = CharacterProfile.load(MANIFEST)
        self.now = 1792064000.0
        self.store = open_world(self.root / 'life', self.profile)
        self.connection = Connection(self.root / 'telegram', API)
        self.connection.configure(TOKEN)
        self.connection.bind(CHANNEL['id'])
        bind_connection(self.connection, 'arkady', self.store.directory)
        fresh_start(self.store, self.connection, now=self.now, actor_id='arkady')
        self.attach()

    def attach(self):
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'arkady': self.profile.persona},
                                    self.profile.speech_contract, personal_life=True, active_world=True,
                                    autonomous_posts=True, direct_channel=True, continuous=True, external_channel=True)
        self.lab = Laboratory(self.store, self.session)
        self.life = ChannelLife(self.lab, self.connection, self.profile.persona,
                                now=lambda: self.now, social_enabled=True)
        self.life.action(True)

    def tearDown(self):
        self.store.close()
        self.connection.close()
        self.temp.cleanup()

    def initial_post(self):
        self.backend.answers = [{'people': [], 'intentions': []},
                                rich(decision='post', text='Двадцать лет передатчик собирал.',
                                     source_ids=['arkady_memory_contact'])]
        for _ in range(4):
            self.life.tick()
        self.assertEqual(self.life.state()['status'], 'running')
        with self.store.lock, self.store.db:
            status = self.life.get('life_status')
            self.life.put('life_status', {**status, 'next_wake': self.now + 3600})
        return self.connection.state()['outbox'][0]['message_id']

    def receive_comment(self, post):
        offset = self.connection.get('offset', 0)
        self.connection.api.batch = [{'update_id': offset, 'message': forward(50, post)},
                                     {'update_id': offset + 1, 'message': comment()}]
        self.connection.poll()
        self.life.tick()
        return self.life.get('channel_attention')['focus_ids'][0]

    def restart(self):
        self.store.close()
        self.connection.close()
        self.store = open_world(self.root / 'life', self.profile)
        self.connection = Connection(self.root / 'telegram', API)
        bind_connection(self.connection, 'arkady', self.store.directory)
        self.attach()

    def test_post_comment_reader_memory_and_promise_survive_restart(self):
        post = self.initial_post()
        source = self.receive_comment(post)
        reader = self.life.social.context('arkady')['reader_memory'][0]
        answer = 'Ладно. Потом покажу схему.'
        self.backend.answers = [rich(decision='reply', text=answer, source_ids=[source], read_ids=[source],
            people=[{'person_key': reader['key'], 'impression': 'Интересуется передатчиком.',
                     'source_ids': [source], 'facts': [{'key': 'interest', 'text': 'Спрашивал про передатчик.',
                                                     'source_ids': [source]}], 'forget_fact_keys': []}],
            commitments=[{'key': 'show_scheme', 'kind': 'post', 'action': 'Показать схему передатчика.',
                          'quote': 'Потом покажу схему.', 'source_ids': [source], 'after_minutes': 60}])]
        self.life.tick()
        self.life.tick()
        self.assertEqual(self.life.state()['own_intentions'][0]['status'], 'pending')
        self.assertEqual(self.store.actor_view('garik')['memories'], [])
        self.assertNotIn('channel_connection', self.store.actor_view('garik'))
        self.assertIsNone(self.store.get('life_status'))
        self.assertTrue(self.store.actor_get('arkady', 'social_review_complete'))
        self.assertIsNone(self.store.get('social_review_complete'))
        calls = self.store.monitor()['call_count']
        self.restart()
        state = self.life.state()
        self.assertEqual(state['name'], 'Аркадий Кедров')
        self.assertEqual(len(state['memories']), 8)
        self.assertEqual(state['reader_memory'][0]['impression'], 'Интересуется передатчиком.')
        self.assertEqual(state['own_intentions'][0]['key'], 'show_scheme')
        self.assertEqual(self.store.monitor()['call_count'], calls)
        self.life.tick()
        self.assertFalse(self.backend.inputs)
        self.assertFalse([call for call in self.connection.api.calls if call[0] == 'sendMessage'])
        self.assertTrue(all(c['actor_id'] == 'arkady' for c in self.store.monitor()['cycles']))
        with self.assertRaises(TelegramError):
            bind_connection(self.connection, 'garik', self.store.directory)
        with self.assertRaises(TelegramError):
            bind_connection(self.connection, 'arkady', self.root / 'another-world')

    def test_photographs_use_own_actor_sources_and_reference_map(self):
        self.initial_post()
        self.life.configure({'episode_minutes': 25})
        with self.store.lock, self.store.db:
            self.life.put('channel_attention', {'purpose': 'own_channel', 'focus_ids': []})
        self.store.set_settings({**self.store.get('settings'), 'events_per_hour': 0}, 0)
        observed = quiet()
        observed.update(entity_ids=['arkady_garage'], visible_to=['arkady'])
        intent = 'Осмотрю передатчик.'
        self.backend.answers = [observed, intent, choice(action=intent, entity_ids=['arkady_transmitter']),
                                outcome(observation='Передатчик осмотрен.')]
        cycle = self.session.prepare('arkady', minutes=0, draw=1)
        self.backend.answers += [{**experience(cycle['id'] + '-outcome'), 'intent_updates': []},
                                  rich(decision='post', text='Посмотрел разъём.', source_ids=[cycle['id'] + '-outcome'])]
        for _ in range(6):
            self.session.step(cycle['id'])
        impulse = next(payload for stage, _, payload in self.backend.inputs if stage == 'impulse')
        self.assertEqual(len(impulse['life']['memories']), 8)
        self.assertEqual(impulse['episode_pace']['target_minutes'], 25)
        captured = {}
        class Photo:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
            def run(self, context):
                captured['context'] = context
                return {'status': 'skipped'}
        self.life.renderer = object()
        self.life.photo_factory = Photo
        self.life.references = {'arkady_transmitter': '/synthetic-reference.png'}
        self.life.photo(self.store.monitor()['posts'][0])
        context = captured['context']
        self.assertEqual(context['actor_id'], 'arkady')
        self.assertTrue(all(s['id'].startswith('arkady_') for s in context['subjects']))
        self.assertIn(cycle['id'] + '-outcome', [s['id'] for s in context['sources']])
        self.assertEqual(captured['references'], self.life.references)

    def test_ambiguous_delivery_blocks_restart_without_retry(self):
        self.connection.api.fail_send = True
        self.backend.answers = [{'people': [], 'intentions': []}, rich(decision='post', text='Не терять текст.')]
        for _ in range(4):
            self.life.tick()
        self.assertEqual(self.life.state()['status'], 'error')
        self.store.close()
        self.connection.close()
        self.store = open_world(self.root / 'life', self.profile)
        self.connection = Connection(self.root / 'telegram', API)
        self.backend = Backend()
        self.session = WorldSession(self.store, self.backend, {'arkady': self.profile.persona}, self.profile.speech_contract)
        self.life = ChannelLife(Laboratory(self.store, self.session), self.connection, self.profile.persona,
                                now=lambda: self.now, social_enabled=True)
        with self.assertRaises(TelegramError):
            self.life.action(True)
        self.life.tick()
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'], 'Не терять текст.')
        self.assertFalse(self.backend.inputs)
        self.assertFalse([c for c in self.connection.api.calls if c[0] == 'sendMessage'])


if __name__ == '__main__':
    unittest.main()
