"""Loopback setup exercises with a synthetic Bot API only."""
from http.cookiejar import CookieJar
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

from persona_runtime.telegram_setup import LocalServer, Setup, handler
from test_telegram import Connection, API, TOKEN, CHANNEL


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = Connection(self.temp.name, API)
        self.setup = Setup(self.connection)
        self.server = LocalServer(('127.0.0.1', 0), handler(self.setup))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.opener.open(self.url).read()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.connection.close()
        self.temp.cleanup()

    def request(self, path, value=None, headers=None, opener=None):
        body = json.dumps(value).encode() if value is not None else None
        extra = {'Origin': self.url}
        if body is not None:
            extra.update({'Content-Type': 'application/json', 'X-Telegram-CSRF': self.setup.csrf})
        extra.update(headers or {})
        return json.loads((opener or self.opener).open(Request(self.url+path, body, extra)).read())

    def test_token_roundtrip_never_echoes_token_and_opening_panel_sends_nothing(self):
        self.assertFalse(self.request('/api/state')['configured'])
        result = self.request('/api/token', {'token': TOKEN})
        self.assertTrue(result['configured'])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.request('/api/bind', {'channel': str(CHANNEL['id'])})
        self.assertFalse(self.connection.state()['outbox'])
        self.request('/api/test-post', {})
        self.request('/api/test-post', {})
        self.assertEqual(len([c for c in self.connection.api.calls if c[0] == 'sendMessage']), 1)

    def test_cross_origin_missing_csrf_and_anonymous_state_rejected(self):
        for headers in ({'Origin': 'https://other.example'}, {'X-Telegram-CSRF': ''}, {'Host': 'evil.example'}):
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/token', {'token': TOKEN}, headers)
            self.assertEqual(caught.exception.code, 403)
            self.assertNotIn(TOKEN.encode(), caught.exception.read())
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/state', opener=build_opener(ProxyHandler({})))
        self.assertEqual(caught.exception.code, 401)
        self.assertIsNone(self.connection.api)

    def test_static_assets_are_local_and_have_no_secret_or_runtime_data(self):
        for path in ('/', '/telegram.js', '/telegram.css'):
            response = self.opener.open(self.url+path)
            text = response.read().decode()
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(self.setup.csrf, text)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        with self.assertRaises(HTTPError):
            self.request('/bot-token.json')


if __name__ == '__main__':
    unittest.main()
