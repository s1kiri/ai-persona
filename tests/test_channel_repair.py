"""Explicit review of an empty failed speech call must never replay it or send."""
import copy
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

import test_comments_during_action as waiting
from persona_runtime.codex import CodexError
from persona_runtime.telegram import TelegramError
from persona_runtime.telegram_setup import Setup, handler as telegram_handler
from persona_runtime.world_lab import handler as world_handler


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.case = waiting.WaitingCommentsTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f = self.case.fixture
        f = self.f
        with f.store.lock, f.store.db:
            f.life.put('own_channel_check_requested', True)
        f.life.tick()
        with patch.object(f.backend, 'generate', side_effect=CodexError('Synthetic stream failure')):
            f.life.tick()
        self.assertEqual(f.life.state()['status'], 'error')
        self.failed = next(c for c in f.store.monitor()['cycles'] if c['status']=='blocked')

    def test_skip_preserves_failed_call_and_physical_action_without_replay_or_send(self):
        f = self.f
        action = copy.deepcopy(f.store.cycle(self.case.cycle['id']))
        calls = copy.deepcopy(f.store.monitor()['calls'])
        minute = f.store.get('minute')
        outbox = copy.deepcopy(f.connection.state()['outbox'])
        self.assertTrue(f.life.state()['can_skip_failed_expression'])
        f.life.skip_failed_expression()
        self.assertEqual(f.store.cycle(self.failed['id'])['status'], 'abandoned')
        self.assertEqual(f.store.cycle(self.case.cycle['id']), action)
        self.assertEqual(f.store.monitor()['calls'], calls)
        self.assertEqual(f.store.get('minute'), minute)
        self.assertEqual(f.connection.state()['outbox'], outbox)
        self.assertEqual(f.life.state()['status'], 'paused')
        self.assertFalse(f.life.get('stage_reviews')[0]['model_replay'])
        with self.assertRaises(TelegramError):
            f.life.skip_failed_expression()
        f.life.action(True)
        f.life.tick()
        self.assertEqual(f.life.state()['status'], 'running')
        self.assertEqual(f.store.monitor()['calls'], calls)

    def test_saved_output_or_unknown_delivery_cannot_be_skipped(self):
        f = self.f
        for kind in ('saved_output', 'unknown_delivery'):
            with self.subTest(kind=kind), f.store.lock, f.store.db:
                value = copy.deepcopy(self.failed['value'])
                if kind == 'saved_output':
                    value['rejected'] = {'answer': 'SAVED TEXT'}
                else:
                    f.store.db.execute("UPDATE channel_jobs SET status='unknown'")
                f.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?', (json.dumps(value), self.failed['id']))
                self.assertFalse(f.life.state()['can_skip_failed_expression'])
                with self.assertRaises(TelegramError):
                    f.life.skip_failed_expression()

    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            server.shutdown(); thread.join(3); server.server_close()
        self.addCleanup(close)
        base = 'http://127.0.0.1:'+str(server.server_port)
        client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        client.open(base).close()
        return base, client

    def post(self, base, client, path, body, header, csrf):
        return json.load(client.open(Request(base+path, json.dumps(body).encode(),
            {'Content-Type': 'application/json', 'Origin': base, header: csrf})))

    def test_both_panels_use_same_life_controls_and_explicit_skip_leaves_pause(self):
        f = self.f
        world, wc = self.serve(world_handler(f.lab))
        setup = Setup(f.connection, life=f.life)
        telegram, tc = self.serve(telegram_handler(setup))
        with self.assertRaises(HTTPError) as err:
            self.post(world, wc, '/api/action', {'paused': False}, 'X-World-CSRF', f.lab.csrf)
        self.assertEqual(err.exception.code, 409)
        self.assertTrue(f.store.get('paused'))
        state = self.post(telegram, tc, '/api/life/skip-expression',
                          {'confirmation': 'skip_without_retry'}, 'X-Telegram-CSRF', setup.csrf)
        self.assertTrue(state['life']['paused'])
        state = self.post(world, wc, '/api/action', {'paused': False}, 'X-World-CSRF', f.lab.csrf)
        self.assertEqual(state['live_status']['status'], 'running')
        self.assertFalse(state['paused'])
        state = self.post(world, wc, '/api/action', {'paused': True}, 'X-World-CSRF', f.lab.csrf)
        self.assertEqual(state['live_status']['status'], 'paused')
        self.assertTrue(state['paused'])
        self.assertEqual(len(f.connection.state()['outbox']), 1)
