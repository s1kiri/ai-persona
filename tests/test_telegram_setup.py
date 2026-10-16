"""Loopback setup exercises with a synthetic Bot API only."""
from http.cookiejar import CookieJar
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

from persona_runtime.telegram_setup import LocalServer, Setup, handler, main
from test_telegram import Connection, API, TOKEN, CHANNEL


class SetupTransportTests(unittest.TestCase):
    def test_setup_ui_uses_owned_shared_proxy_without_any_initial_api_call(self):
        with tempfile.TemporaryDirectory() as folder:
            connection=Mock();server=Mock(server_port=12345);owner=Mock()
            with patch('persona_runtime.proxy.SharedTransport',return_value=owner), \
                 patch('persona_runtime.telegram_setup.TelegramConnection',return_value=connection) as connect, \
                 patch('persona_runtime.telegram_setup.BotAPI') as api, \
                 patch('persona_runtime.telegram_setup.LocalServer',return_value=server), \
                 patch('persona_runtime.telegram_setup.private_json'),patch('persona_runtime.telegram_setup.signal.signal'):
                main(['--data-dir',folder,'--port','0'])
                api.assert_not_called()
                connect.call_args.kwargs['api_factory'](TOKEN)
                api.assert_called_once_with(TOKEN,transport_factory=owner.borrow)
                connection.close.assert_called_once()
                owner.close.assert_called_once()

    def test_setup_constructor_failure_releases_transport_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            owner=Mock()
            with patch('persona_runtime.proxy.SharedTransport',return_value=owner), \
                 patch('persona_runtime.telegram_setup.TelegramConnection',side_effect=ValueError('fixture')):
                with self.assertRaises(ValueError):main(['--data-dir',folder,'--port','0'])
            owner.close.assert_called_once()


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

    def test_link_from_another_local_port_can_open_page_but_cannot_read_api_or_post(self):
        client=build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()))
        headers={'Sec-Fetch-Site':'same-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
        page=client.open(Request(self.url+'/',headers=headers))
        self.assertEqual(page.status,200)
        self.assertIn('garik_telegram_',page.headers['Set-Cookie'])
        self.assertFalse(json.load(client.open(self.url+'/api/state'))['configured'])
        with self.assertRaises(HTTPError) as err:
            client.open(Request(self.url+'/api/state',headers=headers))
        self.assertEqual(err.exception.code,403)
        with self.assertRaises(HTTPError) as err:
            self.request('/api/token',{'token':TOKEN},{'Sec-Fetch-Site':'same-site'})
        self.assertEqual(err.exception.code,403)


if __name__ == '__main__':
    unittest.main()
