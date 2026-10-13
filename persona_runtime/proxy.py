"""Local, explicit SOCKS5 transport. Secrets never enter CLI argv/environment.

A loopback HTTP CONNECT bridge gives Codex one uniform HTTP proxy transport,
including HTTPS upgrades. Only the bridge speaks authenticated SOCKS5.
"""
from contextlib import contextmanager
import ctypes
import getpass
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import struct
import sys
import tempfile
import threading
import uuid

CONFIG = Path(__file__).resolve().parent.parent / '.local' / 'proxy.json'
SERVICE = b'PersonaLab.SOCKS5'
PROXY_NAMES = {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'grpc_proxy'}


class ProxyError(RuntimeError):
    """Only fixed, safe messages may cross the diagnostic boundary."""


class Keychain:
    """macOS Security framework, without security(1) argv or plaintext fallback."""
    def __init__(self):
        if sys.platform != 'darwin':
            raise ProxyError('Нужен macOS Keychain; небезопасного файлового fallback нет.')
        try:
            self.api = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        except OSError:
            raise ProxyError('macOS Keychain недоступен; настройки не сохранены.') from None
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint32
        self.api.SecKeychainAddGenericPassword.argtypes = [pointer, uint, ctypes.c_char_p, uint, ctypes.c_char_p, uint, ctypes.c_char_p, ctypes.POINTER(pointer)]
        self.api.SecKeychainFindGenericPassword.argtypes = [pointer, uint, ctypes.c_char_p, uint, ctypes.c_char_p, ctypes.POINTER(uint), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        self.api.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.api.SecKeychainItemDelete.argtypes = [pointer]
        self.api.CFRelease = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation').CFRelease
        self.api.CFRelease.argtypes = [pointer]
        for name in ('SecKeychainAddGenericPassword', 'SecKeychainFindGenericPassword', 'SecKeychainItemFreeContent', 'SecKeychainItemDelete'):
            getattr(self.api, name).restype = ctypes.c_int32

    def put(self, account, password):
        account, password = account.encode(), password.encode()
        code = self.api.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(account), account, len(password), password, None)
        if code:
            raise ProxyError('Keychain отказал в сохранении. Проверь доступ к связке ключей; файлового fallback нет.')

    def get(self, account):
        account = account.encode()
        length, data = ctypes.c_uint32(), ctypes.c_void_p()
        code = self.api.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(account), account, ctypes.byref(length), ctypes.byref(data), None)
        if code:
            raise ProxyError('Пароль прокси недоступен в Keychain. Разреши локальный доступ или повтори proxy set.')
        try:
            return ctypes.string_at(data, length.value).decode('utf-8')
        except UnicodeError:
            raise ProxyError('Некорректная запись Keychain; повтори proxy set.') from None
        finally:
            self.api.SecKeychainItemFreeContent(None, data)

    def delete(self, account):
        account = account.encode()
        item = ctypes.c_void_p()
        code = self.api.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(account), account, None, None, ctypes.byref(item))
        if code == -25300:  # not found
            return
        if code:
            raise ProxyError('Не удалось удалить прежний пароль из Keychain.')
        try:
            if self.api.SecKeychainItemDelete(item):
                raise ProxyError('Не удалось удалить прежний пароль из Keychain.')
        finally:
            self.api.CFRelease(item)


def validate(config):
    try:
        if set(config) != {'version', 'enabled', 'host', 'port', 'username', 'account'}:
            raise ValueError()
        if config.get('version') != 1 or type(config['enabled']) is not bool:
            raise ValueError()
        host = config['host']
        if not isinstance(host, str) or not host or len(host) > 253 or any(c in host for c in '/@:#?\\ \t\r\n'):
            # IPv6 is accepted below independently of the URI syntax.
            ipaddress.IPv6Address(host)
        host.encode('idna')
        if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
            raise ValueError()
        user = config['username']
        if not isinstance(user, str) or len(user.encode()) > 255 or any(ord(c) < 32 for c in user):
            raise ValueError()
        if not isinstance(config['account'], str) or not uuid.UUID(config['account']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError):
        raise ProxyError('Некорректная настройка SOCKS5; повтори proxy set.') from None
    return config


def load(path=CONFIG):
    try:
        return validate(json.loads(Path(path).read_text(encoding='utf-8')))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ProxyError('Не удалось прочитать настройки прокси; повтори proxy set.') from None


def save(config, path=CONFIG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.proxy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def status(path=CONFIG):
    config = load(path)
    if not config:
        return {'configured': False, 'enabled': False, 'model_called': False}
    return {'configured': True, 'enabled': config['enabled'], 'endpoint': 'socks5://***:***',
            'authentication': 'username/password' if config['username'] else 'none',
            'password': 'Keychain (hidden)' if config['username'] else 'not needed', 'model_called': False}


def interactive_set(path=CONFIG, keychain_factory=Keychain):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ProxyError('proxy set требует локальный интерактивный терминал; секреты не передавай через pipe или чат.')
    print('SOCKS5: адрес и логин вводятся здесь; пароль скрыт. Старые настройки заменятся после сохранения.')
    host = input('Адрес сервера (без socks5:// и порта): ').strip()
    try:
        port = int(input('Порт: ').strip())
    except ValueError:
        raise ProxyError('Порт должен быть числом 1..65535.') from None
    username = input('Логин (Enter, если без авторизации): ').strip()
    config = validate({'version': 1, 'enabled': True, 'host': host, 'port': port,
                       'username': username, 'account': str(uuid.uuid4())})
    # getpass must not degrade to echoing input when the controlling TTY fails.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            password = getpass.getpass('Пароль: ') if username else ''
        except getpass.GetPassWarning:
            raise ProxyError('Скрытый ввод недоступен; настройка отменена.') from None
    try:
        valid_password = not username or 1 <= len(password.encode()) <= 255
    except UnicodeError:
        valid_password = False
    if not valid_password:
        raise ProxyError('Пароль SOCKS5 должен занимать 1..255 байт UTF-8.')
    old = load(path)
    vault = keychain_factory() if username or (old and old['username']) else None
    if username:
        vault.put(config['account'], password)
    try:
        save(config, path)
    except OSError:
        if username:
            vault.delete(config['account'])
        raise ProxyError('Не удалось сохранить настройки; прежняя конфигурация остаётся.') from None
    if old and old['username']:
        try:
            vault.delete(old['account'])
        except ProxyError:
            print('Новая настройка сохранена, но старую запись PersonaLab.SOCKS5 нужно удалить в Keychain Access.', file=sys.stderr)
    return status(path)


def disable(path=CONFIG):
    config = load(path)
    if config:
        config['enabled'] = False
        save(config, path)
    return status(path)


def receive(sock, size, stage):
    value = b''
    while len(value) < size:
        chunk = sock.recv(size - len(value))
        if not chunk:
            raise ProxyError(stage + ': сервер закрыл соединение.')
        value += chunk
    return value


def connect_tcp(config, timeout):
    try:
        addresses = socket.getaddrinfo(config['host'], config['port'], type=socket.SOCK_STREAM)
    except OSError:
        raise ProxyError('DNS: не удалось разрешить адрес SOCKS5 сервера.') from None
    for family, kind, proto, _, address in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError:
            sock.close()
    raise ProxyError('TCP: SOCKS5 сервер недоступен или истёк таймаут.')


def negotiate(sock, config, password):
    method = 2 if config['username'] else 0
    sock.sendall(bytes((5, 1, method)))
    response = receive(sock, 2, 'SOCKS5 negotiation')
    if response != bytes((5, method)):
        raise ProxyError('SOCKS5 negotiation: сервер не принял выбранный метод авторизации.')
    if method == 2:
        user, secret = config['username'].encode(), password.encode()
        if not 1 <= len(secret) <= 255:
            raise ProxyError('SOCKS5 auth: некорректный пароль в Keychain; повтори proxy set.')
        sock.sendall(b'\x01' + bytes((len(user),)) + user + bytes((len(secret),)) + secret)
        if receive(sock, 2, 'SOCKS5 auth') != b'\x01\x00':
            raise ProxyError('SOCKS5 auth: сервер отклонил логин или пароль.')


def destination(sock, host, port):
    # Always remote DNS, including hostnames. Never resolve the destination locally.
    encoded = host.encode('idna')
    if not 1 <= len(encoded) <= 255 or not 1 <= port <= 65535:
        raise ProxyError('SOCKS5 destination: некорректное назначение.')
    sock.sendall(b'\x05\x01\x00\x03' + bytes((len(encoded),)) + encoded + struct.pack('!H', port))
    reply = receive(sock, 4, 'SOCKS5 destination')
    if reply[0] != 5 or reply[1] != 0 or reply[2] != 0:
        raise ProxyError('SOCKS5 destination: сервер не смог открыть соединение с назначением.')
    sizes = {1: 4, 4: 16}
    size = receive(sock, 1, 'SOCKS5 destination')[0] if reply[3] == 3 else sizes.get(reply[3])
    if size is None:
        raise ProxyError('SOCKS5 destination: некорректный ответ сервера.')
    receive(sock, size + 2, 'SOCKS5 destination')


def socks_connect(config, password, host, port, timeout=10):
    sock = connect_tcp(config, timeout)
    try:
        negotiate(sock, config, password)
        destination(sock, host, port)
        return sock
    except (OSError, ProxyError, UnicodeError):
        sock.close()
        raise


def credential(config, keychain_factory=Keychain):
    return keychain_factory().get(config['account']) if config['username'] else ''


def health(path=CONFIG, timeout=10, host='chatgpt.com', keychain_factory=Keychain):
    config = load(path)
    if not config or not config['enabled']:
        raise ProxyError('Прокси выключен или не настроен; сначала proxy set.')
    report = {'model_called': False, 'destination': 'https://' + host,
              'credentials': 'not checked', 'dns_tcp': 'not checked', 'socks5': 'not checked', 'https': 'not checked'}
    stage = 'credentials'
    sock = None
    try:
        password = credential(config, keychain_factory)
        report[stage] = 'ok'
        stage = 'dns_tcp'
        sock = connect_tcp(config, timeout)
        report[stage] = 'ok'
        stage = 'socks5'
        negotiate(sock, config, password)
        report[stage] = 'ok'
        stage = 'https'
        destination(sock, host, 443)
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            sock = tls
            tls.sendall(('HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n' % host).encode())
            line = tls.recv(4096).split(b'\r\n', 1)[0]
            parts = line.split()
            if len(parts) < 2 or not parts[0].startswith(b'HTTP/') or not parts[1].isdigit():
                raise ProxyError('HTTPS: назначение не вернуло HTTP ответ.')
            report[stage] = 'ok'
            report['http_status'] = int(parts[1])
        report['transport_ok'] = True
        report['http_response_ok'] = 200 <= report['http_status'] < 400
        report['note'] = ('HTTPS reached and certificate verified; HTTP status is the actual site response, '
                          'not a Codex authentication/model result.')
        report['ok'] = True
    except ProxyError as exc:
        report[stage] = 'failed'
        report.update(ok=False, error=str(exc))
    except (OSError, UnicodeError, ValueError):
        report[stage] = 'failed'
        report.update(ok=False, error={'credentials': 'Keychain: пароль недоступен.', 'dns_tcp': 'DNS/TCP: соединение не удалось.',
                                      'socks5': 'SOCKS5: таймаут или ошибка протокола.',
                                      'https': 'HTTPS: ошибка подключения, TLS или таймаут.'}[stage])
    finally:
        if sock:
            sock.close()
    report.setdefault('transport_ok', False)
    return report


class Bridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    def __init__(self, config, password, timeout):
        self.config, self.password, self.timeout = config, password, timeout
        self.active = set()
        self.lock = threading.Lock()
        self.requests = 0
        self.failures = 0
        self.stopping = False
        super().__init__(('127.0.0.1', 0), Tunnel)

    def server_close(self):
        with self.lock:
            self.stopping = True
            for sock in self.active:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self.active.clear()
        super().server_close()

    def handle_error(self, request, client_address):
        # socketserver's default traceback could contain sensitive exception text.
        with self.lock:
            self.failures += 1


class Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        client, remote = self.request, None
        client.settimeout(self.server.timeout)
        with self.server.lock:
            if self.server.stopping:
                return
            self.server.active.add(client)
        try:
            header = b''
            while b'\r\n\r\n' not in header and len(header) < 16384:
                chunk = client.recv(1)
                if not chunk:
                    return
                header += chunk
            if not header.endswith(b'\r\n\r\n'):
                raise ProxyError('Invalid CONNECT')
            parts = header.split(b'\r\n', 1)[0].decode('ascii').split()
            if len(parts) != 3 or parts[0] != 'CONNECT':
                raise ProxyError('Only CONNECT supported')
            authority = parts[1]
            host, port = authority.rsplit(':', 1)
            host = host.strip('[]')
            with self.server.lock:
                self.server.requests += 1
            remote = socks_connect(self.server.config, self.server.password, host, int(port), min(self.server.timeout, 10))
            with self.server.lock:
                if self.server.stopping:
                    remote.close()
                    return
                self.server.active.add(remote)
            client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            client.settimeout(None)
            remote.settimeout(None)
            while True:
                ready, _, _ = select.select((client, remote), (), (), self.server.timeout)
                if not ready:
                    # A persistent Codex connection may be idle between messages.
                    # Shutdown closes active sockets; setup still has a deadline.
                    if self.server.stopping:return
                    continue
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is client else client).sendall(data)
        except (OSError, ValueError, UnicodeError, ProxyError):
            with self.server.lock:
                self.server.failures += 1
            try:
                client.sendall(b'HTTP/1.1 502 Proxy transport failed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
        finally:
            with self.server.lock:
                self.server.active.discard(client)
                if remote:
                    self.server.active.discard(remote)
            if remote:
                remote.close()


@contextmanager
def transport(environment, path=CONFIG, timeout=30, keychain_factory=Keychain):
    config = load(path)
    if not config or not config['enabled']:
        yield environment, None
        return
    password = credential(config, keychain_factory)
    try:
        bridge = Bridge(config, password, timeout)
    except OSError:
        raise ProxyError('Не удалось запустить локальный мост SOCKS5; прямой повтор отключён.') from None
    thread = threading.Thread(target=bridge.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()
    env = {key: value for key, value in environment.items() if key.lower() not in PROXY_NAMES and not key.lower().endswith('_proxy')}
    address = 'http://127.0.0.1:%d' % bridge.server_address[1]
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env[key] = address
    env['NO_PROXY'] = env['no_proxy'] = ''
    try:
        yield env, bridge
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)
        bridge.password = ''


def child_command(command, bridge):
    """Fail closed on this macOS prototype: child can only reach the loopback bridge.

    Seatbelt also limits descendants. Unix sockets remain available for local CLI
    service IPC. No global network settings or parent environment are modified.
    """
    if bridge is None:
        return command
    binary = '/usr/bin/sandbox-exec'
    if sys.platform != 'darwin' or not os.path.isfile(binary):
        raise ProxyError('Защита от прямого трафика требует macOS sandbox-exec; прокси-вызов отменён.')
    port = bridge.server_address[1]
    profile = ('(version 1)(allow default)(deny network-outbound)'
               '(allow network-outbound (remote ip "localhost:%d"))'
               '(allow network-outbound (remote unix-socket))') % port
    return [binary, '-p', profile] + command
