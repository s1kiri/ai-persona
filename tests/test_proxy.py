"""SOCKS transport tests with synthetic credentials and local sockets only."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from persona_runtime import proxy
from persona_runtime.codex import CodexBackend, CodexError

SECRET = 'synthetic:p@ss/with?characters'
USER = 'synthetic-user'


class Vault:
    values = {}
    def put(self, key, value): self.values[key] = value
    def get(self, key): return self.values[key]
    def delete(self, key): self.values.pop(key, None)


def config(port=1080, username=USER):
    return {'version': 1, 'enabled': True, 'host': '127.0.0.1', 'port': port,
            'username': username, 'account': str(uuid.uuid4())}


class MockSocks(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self, method=2, auth=0, reply=0, echo=True):
        self.method, self.auth, self.reply, self.echo = method, auth, reply, echo
        self.credentials = []
        self.destinations = []
        self.errors = []
        super().__init__(('127.0.0.1', 0), MockHandler)
    def handle_error(self, request, client_address):
        self.errors.append('handler failed')


class MockHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(3)
        greeting = proxy.receive(sock, 2, 'test')
        proxy.receive(sock, greeting[1], 'test')
        sock.sendall(bytes((5, self.server.method)))
        if self.server.method == 255:
            return
        if self.server.method == 2:
            version, length = proxy.receive(sock, 2, 'test')
            user = proxy.receive(sock, length, 'test')
            length = proxy.receive(sock, 1, 'test')[0]
            secret = proxy.receive(sock, length, 'test')
            self.server.credentials.append((user.decode(), secret.decode()))
            sock.sendall(bytes((1, self.server.auth)))
            if self.server.auth:
                return
        head = proxy.receive(sock, 5, 'test')
        host = proxy.receive(sock, head[4], 'test').decode()
        port = struct.unpack('!H', proxy.receive(sock, 2, 'test'))[0]
        self.server.destinations.append((host, port, head[3]))
        sock.sendall(bytes((5, self.server.reply, 0, 1)) + b'\x00\x00\x00\x00\x00\x00')
        if self.server.reply:
            return
        if self.server.echo:
            while True:
                data = sock.recv(4096)
                if not data:
                    return
                sock.sendall(data)


@contextmanager
def server(**kwargs):
    mock = MockSocks(**kwargs)
    thread = threading.Thread(target=mock.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield mock
    finally:
        mock.shutdown()
        mock.server_close()
        thread.join(1)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'proxy.json'
        Vault.values = {}
    def tearDown(self): self.temp.cleanup()
    def setup_config(self, port=1080, username=USER):
        value = config(port, username)
        Vault().put(value['account'], SECRET)
        proxy.save(value, self.path)
        return value
    def test_save_restart_mask_and_disable_without_secret_access(self):
        value = self.setup_config()
        self.assertEqual(proxy.load(self.path), value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, self.path.read_text())
        self.assertNotIn(USER, json.dumps(proxy.status(self.path)))
        self.assertTrue(proxy.disable(self.path)['configured'])
        self.assertFalse(proxy.status(self.path)['enabled'])
        with proxy.transport({'HTTPS_PROXY':'inherited'}, self.path, keychain_factory=lambda: self.fail('secret read on off')) as (env, bridge):
            self.assertIsNone(bridge)
            self.assertEqual(env['HTTPS_PROXY'], 'inherited')
    def test_interactive_edit_is_atomic_and_does_not_echo_password(self):
        old = self.setup_config()
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), patch('builtins.input', side_effect=['new.example','1234',USER]), patch('getpass.getpass', return_value=SECRET), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertTrue(proxy.interactive_set(self.path, Vault)['enabled'])
            self.assertNotIn(SECRET, output.getvalue())
        value = proxy.load(self.path)
        self.assertEqual(value['host'], 'new.example')
        self.assertNotIn(old['account'], Vault.values)
        self.assertEqual(Vault().get(value['account']), SECRET)
    def test_keychain_failure_preserves_existing_settings(self):
        old = self.setup_config()
        class Failing(Vault):
            def put(self, *args): raise proxy.ProxyError('Keychain unavailable')
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), patch('builtins.input', side_effect=['new.example','1234',USER]), patch('getpass.getpass', return_value=SECRET):
            with self.assertRaises(proxy.ProxyError): proxy.interactive_set(self.path, Failing)
        self.assertEqual(proxy.load(self.path), old)
    def test_no_pipe_or_getpass_plaintext_fallback(self):
        with patch('sys.stdin.isatty', return_value=False):
            with self.assertRaises(proxy.ProxyError): proxy.interactive_set(self.path, Vault)
        self.assertFalse(self.path.exists())
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), patch('builtins.input', side_effect=['host.example','1234',USER]), patch('getpass.getpass', side_effect=__import__('getpass').GetPassWarning('echo')):
            with self.assertRaises(proxy.ProxyError): proxy.interactive_set(self.path, Vault)
        self.assertFalse(self.path.exists())
    def test_invalid_config_never_prints_input(self):
        for field, value in [('host', 'socks5://secret@host'), ('port',0), ('username','bad\nsecret'), ('account','bad')]:
            invalid = config(); invalid[field] = value
            with self.assertRaises(proxy.ProxyError) as error: proxy.validate(invalid)
            self.assertNotIn(str(value), str(error.exception))
    def test_auth_and_remote_dns_echo_tunnel(self):
        with server() as mock:
            value = self.setup_config(mock.server_address[1])
            with proxy.socks_connect(value, SECRET, 'destination.invalid', 443) as sock:
                sock.sendall(b'hello')
                self.assertEqual(sock.recv(5), b'hello')
            self.assertEqual(mock.credentials, [(USER, SECRET)])
            self.assertEqual(mock.destinations, [('destination.invalid',443,3)])
    def test_no_auth(self):
        with server(method=0) as mock:
            value = self.setup_config(mock.server_address[1], '')
            with proxy.socks_connect(value, '', 'destination.invalid', 443): pass
            self.assertEqual(mock.credentials, [])
    def test_health_distinguishes_dns_tcp_negotiation_auth_destination_tls(self):
        self.setup_config()
        with patch('socket.getaddrinfo', side_effect=socket.gaierror('secret')):
            result = proxy.health(self.path, keychain_factory=Vault)
            self.assertIn('DNS', result['error'])
            self.assertEqual(result['dns_tcp'], 'failed')
        # Closed ephemeral local port, not a network probe.
        with socket.socket() as closed:
            closed.bind(('127.0.0.1',0)); port = closed.getsockname()[1]
        self.setup_config(port)
        result = proxy.health(self.path, timeout=.2, keychain_factory=Vault)
        self.assertIn('TCP', result['error'])
        for kwargs, stage, error in [({'method':255},'socks5','negotiation'), ({'auth':1},'socks5','auth'), ({'reply':5},'https','destination'), ({'echo':False},'https','TLS')]:
            with server(**kwargs) as mock:
                self.setup_config(mock.server_address[1])
                result = proxy.health(self.path, timeout=.3, keychain_factory=Vault)
                self.assertFalse(result['ok'])
                self.assertEqual(result[stage], 'failed')
                self.assertIn(error, result['error'])
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertFalse(result['model_called'])
    def test_bridge_overrides_only_child_env_and_never_passes_secret(self):
        with server() as mock:
            self.setup_config(mock.server_address[1])
            original = {'HTTPS_PROXY':'http://bad:secret@host', 'all_proxy':'socks5://other', 'NO_PROXY':'*', 'Grpc_Proxy':'other', 'PATH':'safe'}
            with proxy.transport(original, self.path, timeout=2, keychain_factory=Vault) as (env, bridge):
                self.assertEqual(env['NO_PROXY'], '')
                self.assertNotIn('Grpc_Proxy',env)
                self.assertNotIn(SECRET, json.dumps(env))
                self.assertEqual(original['NO_PROXY'], '*')
                with socket.create_connection(bridge.server_address) as sock:
                    sock.sendall(b'CONNECT destination.invalid:443 HTTP/1.1\r\nHost: destination.invalid\r\n\r\n')
                    self.assertIn(b'200', sock.recv(4096))
                    sock.sendall(b'echo')
                    self.assertEqual(sock.recv(4), b'echo')
                self.assertEqual(bridge.requests, 1)
            self.assertFalse(bridge.active)
    def test_bridge_failure_does_not_retry_direct_or_leak(self):
        with server(auth=1) as mock:
            self.setup_config(mock.server_address[1])
            with proxy.transport({}, self.path, timeout=1, keychain_factory=Vault) as (_, bridge):
                with socket.create_connection(bridge.server_address) as sock:
                    sock.sendall(b'CONNECT destination.invalid:443 HTTP/1.1\r\n\r\n')
                    answer = sock.recv(4096)
                    self.assertIn(b'502', answer)
                    self.assertNotIn(SECRET.encode(), answer)
                self.assertEqual(mock.destinations, [])
                self.assertEqual(bridge.failures, 1)
    @unittest.skipUnless(sys.platform == 'darwin', 'macOS child network guard')
    def test_child_guard_allows_bridge_blocks_other_tcp_and_keeps_secret_out_of_argv(self):
        from test_runtime import decision
        with server() as mock:
            self.setup_config(mock.server_address[1])
            binary = Path(self.temp.name) / 'synthetic-codex'
            binary.write_text('#!' + str(Path(sys.executable).resolve()) + '\n' + '''import json, os, pathlib, socket, sys
from urllib.parse import urlsplit
proxy = urlsplit(os.environ['HTTPS_PROXY'])
assert not proxy.username and not proxy.password
assert os.environ['NO_PROXY'] == ''
assert not any('synthetic-user' in value or 'synthetic:p@ss' in value for value in sys.argv)
# Another localhost port is disallowed, not just an unreachable remote address.
try:
    socket.create_connection(('127.0.0.1', 9), timeout=1)
except PermissionError:
    pass
else:
    raise AssertionError('Direct TCP was not blocked')
with socket.create_connection((proxy.hostname, proxy.port)) as sock:
    sock.sendall(b'CONNECT destination.invalid:443 HTTP/1.1\\r\\n\\r\\n')
    assert b'200' in sock.recv(4096)
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)
print(json.dumps({'type':'turn.completed','usage':{}}))
''' % json.dumps(decision()))
            binary.chmod(0o700)
            with patch('persona_runtime.codex.transport', side_effect=lambda env, **kw: proxy.transport(env, self.path, timeout=2, keychain_factory=Vault)), patch.dict(os.environ, {'NO_PROXY':'*','HTTPS_PROXY':'http://inherited:secret@host'}):
                backend = CodexBackend(str(binary), model='synthetic', timeout=5)
                result, _ = backend.decide('probe')
                self.assertEqual(result, decision())
                self.assertEqual(backend.last_proxy_report['connect_requests'],1)
            self.assertEqual(mock.credentials, [(USER,SECRET)])

    def test_https_health_accepts_http_error_as_reachable_and_rejects_untrusted_tls(self):
        self.setup_config()
        class TLS:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def sendall(self, value): self.request = value
            def recv(self, size): return b'HTTP/1.1 403 Forbidden\r\n'
            def close(self): pass
        tls = TLS()
        with patch('persona_runtime.proxy.connect_tcp', return_value=tls), patch('persona_runtime.proxy.negotiate'), patch('persona_runtime.proxy.destination'), patch('ssl.create_default_context') as context:
            context.return_value.wrap_socket.return_value = tls
            result = proxy.health(self.path, keychain_factory=Vault)
            self.assertTrue(result['ok'])
            self.assertEqual(result['http_status'],403)
            context.return_value.wrap_socket.assert_called_once_with(tls, server_hostname='chatgpt.com')
            self.assertIn(b'HEAD / HTTP/1.1',tls.request)
        with patch('persona_runtime.proxy.connect_tcp', return_value=tls), patch('persona_runtime.proxy.negotiate'), patch('persona_runtime.proxy.destination'), patch('ssl.create_default_context') as context:
            import ssl
            context.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError('synthetic secret')
            result = proxy.health(self.path, keychain_factory=Vault)
            self.assertFalse(result['ok'])
            self.assertNotIn('synthetic secret',json.dumps(result))

    def test_disabled_and_corrupt_health_do_not_start_model(self):
        self.assertFalse(proxy.status(self.path)['configured'])
        with self.assertRaises(proxy.ProxyError): proxy.health(self.path)
        self.path.write_text('{"password":"synthetic-secret"}')
        with self.assertRaises(proxy.ProxyError) as error: proxy.load(self.path)
        self.assertNotIn('synthetic-secret', str(error.exception))
    def test_cli_health_never_calls_model_without_explicit_flag(self):
        from persona_runtime.__main__ import main
        with patch('persona_runtime.proxy.health', return_value={'ok':True,'model_called':False}), patch('persona_runtime.__main__.CodexBackend') as backend, patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['proxy','health']),0)
            backend.assert_not_called()
            backend.return_value.last_proxy_report = {'enabled':True,'connect_requests':1}
            self.assertEqual(main(['proxy','health','--codex']),0)
            backend.return_value.decide.assert_called_once()
        with patch('persona_runtime.proxy.health',return_value={'ok':False,'model_called':False}), patch('persona_runtime.__main__.CodexBackend') as backend, patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['proxy','health','--codex']),1)
            backend.assert_not_called()

    def test_adapter_error_diagnostics_discard_raw_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake'
            binary.write_text('#!' + str(Path(sys.executable).resolve()) + '\nimport json\nprint(json.dumps({"type":"error","message":%r}))\n' % SECRET)
            binary.chmod(0o700)
            with patch('persona_runtime.codex.transport', side_effect=lambda env, **kw: proxy.transport(env,self.path)):
                with self.assertRaises(CodexError) as error: CodexBackend(str(binary), model='synthetic').decide('probe')
            self.assertNotIn(SECRET, str(error.exception))


if __name__ == '__main__': unittest.main()
