"""Loopback-only Telegram setup. No model calls, automatic posts or credential echo."""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

from .daemon import same_secret
from .telegram import TelegramConnection, TelegramError, private_json

ASSETS = Path(__file__).parent / 'telegram_panel'
TEST_POST = 'Проверка подключения Гарика. Напиши комментарий под этим постом — проверим, что он доходит. Это технический тест, не самостоятельный пост персонажа.'
TEST_REPLY = 'Комментарий дошёл, ответ прикреплён к нему. Это техническая проверка связи; сам Гарик пока на паузе.'


class Setup:
    def __init__(self, connection):
        self.connection = connection
        self.cookie, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    def state(self):
        return {**self.connection.state(), 'csrf': self.csrf}


def handler(setup):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, mime='application/json', cookie=False):
            body = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', f'garik_telegram_{self.server.server_port}={setup.cookie}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def origin_ok(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            return (self.headers.get('Host') == origin[7:] and
                    self.headers.get('Origin', origin) == origin and
                    self.headers.get('Sec-Fetch-Site', 'none') not in ('cross-site', 'same-site'))

        def authenticated(self):
            try:
                jar = SimpleCookie(self.headers.get('Cookie', ''))
                return same_secret(jar[f'garik_telegram_{self.server.server_port}'].value, setup.cookie)
            except (KeyError, ValueError):
                return False

        def do_GET(self):
            if not self.origin_ok():
                return self.reply(403, {'error': 'Локальное подключение обязательно.'})
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/telegram.js': ('telegram.js', 'text/javascript; charset=utf-8'),
                      '/telegram.css': ('telegram.css', 'text/css; charset=utf-8')}
            if self.path in assets:
                file, mime = assets[self.path]
                return self.reply(200, (ASSETS / file).read_text(), mime, self.path == '/')
            if not self.authenticated():
                return self.reply(401, {'error': 'Сначала открой страницу подключения.'})
            if self.path == '/api/state':
                return self.reply(200, setup.state())
            return self.reply(404, {'error': 'Страница не найдена.'})

        def do_POST(self):
            if not self.origin_ok() or not self.authenticated() or not same_secret(self.headers.get('X-Telegram-CSRF', ''), setup.csrf):
                return self.reply(403, {'error': 'Открой локальную страницу подключения заново.'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if self.headers.get('Content-Type') != 'application/json' or not 0 < size <= 8000:
                    raise ValueError()
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError()
                if self.path == '/api/token' and set(value) == {'token'}:
                    setup.connection.configure(value['token'])
                elif self.path == '/api/bind' and set(value) == {'channel'}:
                    setup.connection.bind(value['channel'])
                elif self.path == '/api/test-post' and value == {}:
                    setup.connection.send('setup-text-v1', TEST_POST)
                elif self.path == '/api/test-reply' and set(value) == {'comment_id'}:
                    setup.connection.send('setup-reply-' + str(value['comment_id']), TEST_REPLY, reply_to=value['comment_id'])
                else:
                    return self.reply(404, {'error': 'Действие не найдено.'})
                return self.reply(200, setup.state())
            except TelegramError as error:
                return self.reply(400, {'error': str(error)})
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {'error': 'Некорректный запрос.'})
            except Exception:
                return self.reply(500, {'error': 'Локальная ошибка подключения. Секреты скрыты; автоматического повтора нет.'})
    return Handler


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Never let request bodies or secret-bearing exception strings reach logs.
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    destination = args.data_dir.expanduser().resolve()
    if (destination == root or
            (root in destination.parents and not any(p == destination or p in destination.parents for p in (root / '.local', root / '.runtime')))):
        parser.error('repository connection data must be under .local/ or .runtime/')
    if not 0 <= args.port <= 65535:
        parser.error('invalid port')
    if args.background:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination / 'panel.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'persona_runtime.telegram_setup', '--data-dir', str(destination), '--port', str(args.port)],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        from urllib.request import ProxyHandler, build_opener
        client = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise TelegramError('Экран подключения не запустился. Проверь, свободен ли порт.')
                try:
                    descriptor = json.loads((destination / 'panel.json').read_text())
                    if descriptor['pid'] == process.pid:
                        url = 'http://127.0.0.1:' + str(descriptor['port'])
                        client.open(url, timeout=1).read()
                        print(json.dumps({'url': url, 'pid': process.pid}), flush=True)
                        return
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(.1)
            raise TelegramError('Готовность страницы не подтверждена.')
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            raise
    connection = TelegramConnection(destination)
    server = None
    try:
        server = LocalServer(('127.0.0.1', args.port), handler(Setup(connection)))
        private_json(destination / 'panel.json', {'pid': os.getpid(), 'port': server.server_port})
        connection.start()
        def stop(*unused):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=.2)
    finally:
        if server:
            server.server_close()
        connection.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Экран подключения остановлен. Подробности ошибок скрыты для защиты токена.', file=sys.stderr)
        sys.exit(1)
