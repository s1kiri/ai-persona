"""Loopback-only UI controls. Synthetic state, no model connection."""
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

from persona_runtime.world import WorldStore, WorldSession
from persona_runtime.world_lab import Laboratory, handler
from test_world import Backend, event


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorldStore(self.temp.name)
        self.lab = Laboratory(self.store)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.lab))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.opener.open(self.url).read()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, value=None, headers=None):
        data = json.dumps(value).encode() if value is not None else None
        h = {'Origin': self.url}
        if data is not None:
            h.update({'Content-Type': 'application/json', 'X-World-CSRF': self.lab.csrf})
        h.update(headers or {})
        return json.loads(self.opener.open(Request(self.url+path, data, h)).read())

    def test_preparation_and_sliders_never_enable_or_call_model(self):
        state = self.request('/api/state')
        self.assertFalse(state['model_enabled'])
        settings = {**state['settings'], 'events_per_hour': 0, 'surprise': 15}
        self.request('/api/settings', {'settings': settings, 'revision': state['settings_revision']})
        state = self.request('/api/prepare', {'actor_id': 'garik', 'minutes': 60})
        cycle = state['cycles'][0]
        self.assertFalse(cycle['value']['brief']['new_event_allowed'])
        self.assertEqual(state['minute'], 60)
        self.assertEqual(state['call_count'], 0)
        self.request('/api/action', {'paused': False})
        with self.assertRaises(HTTPError) as e:
            self.request('/api/step', {'cycle_id': cycle['id'], 'phase': cycle['phase'], 'revision': cycle['revision']})
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(self.store.monitor()['call_count'], 0)

    def test_lost_step_receipt_cannot_advance_another_phase_on_replay(self):
        backend = Backend(event())
        self.lab.session = WorldSession(self.store, backend, {'garik': 'Synthetic persona'}, 'Synthetic speech')
        cycle = self.store.prepare(draw=0)
        self.store.pause(False)
        request = {'cycle_id': cycle['id'], 'phase': cycle['phase'], 'revision': cycle['revision']}
        self.request('/api/step', request)
        self.lab.worker.join(3)
        self.assertEqual(self.store.cycle(cycle['id'])['phase'], 'choice')
        with self.assertRaises(HTTPError) as e:
            self.request('/api/step', request)
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(len(backend.inputs), 1)

    def test_conflicting_controls_do_not_overwrite_and_can_abandon_preparation(self):
        state = self.request('/api/state')
        self.request('/api/settings', {'settings': state['settings'], 'revision': 0})
        with self.assertRaises(HTTPError) as e:
            self.request('/api/settings', {'settings': state['settings'], 'revision': 0})
        self.assertEqual(e.exception.code, 409)
        state = self.request('/api/prepare', {'actor_id': 'garik', 'minutes': 30})
        self.request('/api/abandon', {'cycle_id': state['cycles'][0]['id']})
        self.request('/api/prepare', {'actor_id': 'garik', 'minutes': 15})
        self.assertEqual(self.store.state()['minute'], 45)

    def test_cross_origin_missing_csrf_and_anonymous_private_state_rejected(self):
        for headers in ({'Origin': 'https://other.example'}, {'X-World-CSRF': 'invalid'}):
            with self.assertRaises(HTTPError) as e:
                self.request('/api/action', {'paused': False}, headers)
            self.assertEqual(e.exception.code, 403)
        anonymous = build_opener(ProxyHandler({}))
        with self.assertRaises(HTTPError) as e:
            anonymous.open(self.url+'/api/state')
        self.assertEqual(e.exception.code, 401)
        self.assertTrue(self.store.get('paused'))

    def test_page_assets_and_csp_are_local_and_never_contain_runtime_data(self):
        response = self.opener.open(self.url)
        html = response.read().decode()
        self.assertIn('frame-ancestors', response.headers['Content-Security-Policy'])
        self.assertNotIn(self.lab.csrf, html)
        for asset in ('/world.css', '/world.js'):
            self.assertTrue(self.opener.open(self.url+asset).read())
        self.assertFalse(self.store.monitor()['calls'])


if __name__ == '__main__':
    unittest.main()
