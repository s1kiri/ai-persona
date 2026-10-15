"""Local review console. Default mode never constructs a model connection."""
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
from .world import WorldStore, WorldSession, WorldConflict

ASSETS = Path(__file__).parent / 'world_panel'


class Laboratory:
    def __init__(self, store, session=None):
        self.store, self.session = store, session
        self.cookie, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.busy = False
        self.failure = None
        self.lock = threading.Lock()
        self.worker = None
        from .world_night import Night
        self.night = Night(self)

    def state(self):
        value = self.store.monitor()
        value.update(csrf=self.csrf, model_enabled=self.session is not None,
                     pid=os.getpid(),
                     busy=self.busy, failure=self.failure or self.store.get('review_notice'),
                     max_calls=self.session.max_calls if self.session else 24,
                     mode='model-review' if self.session else 'preparation',
                     model=getattr(self.session.backend, 'model', None) if self.session else None)
        value['autonomous_posts'] = bool(self.session and self.session.autonomous_posts)
        value['direct_channel'] = bool(self.session.direct_channel if self.session else
                                       value['cycles'] and value['cycles'][0]['value'].get('direct_channel'))
        value['free_intentions'] = bool(self.session and self.session.free_intentions)
        value['personal_life'] = bool(self.session.personal_life if self.session else
                                      value['cycles'] and value['cycles'][0]['value'].get('personal_life'))
        value['research_enabled'] = bool(self.session and self.session.research_enabled)
        value['lossless_intentions'] = bool(self.session and self.session.lossless_intentions)
        value['simulated_audience'] = bool(self.session and self.session.simulated_audience)
        value['active_world'] = bool(self.session.active_world if self.session else
                                    value['cycles'] and value['cycles'][0]['value'].get('active_world'))
        value['night'] = self.store.get('night')
        return value

    def prepare(self, actor_id, minutes):
        if self.session and (self.session.personal_life or self.session.lossless_intentions):
            return self.session.prepare(actor_id, minutes)
        return self.store.prepare(actor_id, minutes)

    def step(self, cycle_id, phase, revision):
        with self.lock:
            if self.session is None:
                raise WorldConflict('model tests have not been enabled')
            cycle = self.store.cycle(cycle_id)
            if self.busy or self.store.get('paused') or cycle['status'] != 'ready' or cycle['phase'] != phase or cycle['revision'] != revision:
                raise WorldConflict('cycle cannot run now')
            self.busy, self.failure = True, None
            def run():
                try:
                    self.session.step(cycle_id)
                except Exception:
                    self.failure = 'Этап остановлен. Автоматического повтора нет; результат требует просмотра.'
                finally:
                    self.busy = False
            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()


def handler(lab):
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
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            if cookie:
                self.send_header('Set-Cookie', 'garik_world_' + str(self.server.server_port) + '=' + lab.cookie + '; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def origin_ok(self):
            host = '127.0.0.1:' + str(self.server.server_port)
            return self.headers.get('Host') == host and self.headers.get('Origin', 'http://' + host) == 'http://' + host and self.headers.get('Sec-Fetch-Site', 'none') not in ('cross-site', 'same-site')

        def authenticated(self):
            try:
                jar = SimpleCookie(self.headers.get('Cookie', ''))
                return same_secret(jar['garik_world_' + str(self.server.server_port)].value, lab.cookie)
            except (KeyError, ValueError):
                return False

        def do_GET(self):
            if not self.origin_ok():
                return self.reply(403, {'error': 'origin rejected'})
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/world.css': ('world.css', 'text/css; charset=utf-8'),
                      '/world.js': ('world.js', 'text/javascript; charset=utf-8')}
            if self.path in assets:
                file, mime = assets[self.path]
                return self.reply(200, (ASSETS / file).read_text(), mime, self.path == '/')
            if not self.authenticated():
                return self.reply(401, {'error': 'local session required'})
            if self.path == '/api/state':
                return self.reply(200, lab.state())
            return self.reply(404, {'error': 'not found'})

        def do_POST(self):
            if not self.origin_ok() or not self.authenticated() or not same_secret(self.headers.get('X-World-CSRF', ''), lab.csrf):
                return self.reply(403, {'error': 'local session required'})
            try:
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('JSON required')
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 16000:
                    raise ValueError('invalid request size')
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError('object required')
                if self.path == '/api/settings' and set(value) == {'settings', 'revision'}:
                    lab.store.set_settings(value['settings'], value['revision'])
                elif self.path == '/api/prepare' and set(value) == {'actor_id', 'minutes'}:
                    lab.prepare(value['actor_id'], value['minutes'])
                elif self.path == '/api/action' and set(value) == {'paused'}:
                    lab.store.pause(value['paused'])
                elif self.path == '/api/step' and set(value) == {'cycle_id', 'phase', 'revision'}:
                    lab.step(value['cycle_id'], value['phase'], value['revision'])
                elif self.path == '/api/abandon' and set(value) == {'cycle_id'}:
                    lab.store.abandon(value['cycle_id'])
                elif self.path == '/api/speech' and set(value) == {'cycle_id', 'surface', 'message'}:
                    lab.store.request_speech(value['cycle_id'], value['surface'], value['message'])
                elif self.path == '/api/night':
                    if value == {'action': 'stop'}:
                        lab.night.stop()
                    else:
                        lab.night.start(value)
                else:
                    return self.reply(404, {'error': 'unknown action'})
                return self.reply(200, lab.state())
            except WorldConflict:
                return self.reply(409, {'error': 'Состояние изменилось или действие сейчас недоступно. Обнови панель.'})
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {'error': 'Некорректный запрос.'})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--live', action='store_true', help='explicitly enable manual model-review steps')
    parser.add_argument('--persona', type=Path)
    parser.add_argument('--speech-contract', type=Path)
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--background', action='store_true', help='detach the laboratory from the terminal and wait for readiness')
    parser.add_argument('--autonomous-posts', action='store_true', help='let the character choose local channel drafts after outcomes')
    parser.add_argument('--direct-channel', action='store_true', help='save the character channel text directly, without a separate editorial step')
    parser.add_argument('--free-intentions', action='store_true', help='write a private intention before extracting world action metadata')
    parser.add_argument('--personal-life', action='store_true', help='choose before the world event and update lasting personal matters after the outcome')
    parser.add_argument('--research', action='store_true', help='allow character-chosen bounded native web research')
    parser.add_argument('--lossless-intentions', action='store_true', help='account for every intent fragment and explicit recovery decisions')
    parser.add_argument('--simulated-audience', action='store_true', help='deliver explicitly fictional local reader comments before the next free choice')
    parser.add_argument('--active-world', action='store_true', help='plan resident openings and observe the world before choosing')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or not 1 <= args.max_calls <= 200:
        parser.error('invalid port or call limit')
    root = Path(__file__).resolve().parent.parent
    destination = args.data_dir.expanduser().resolve()
    if destination == root or (root in destination.parents and not any(p == destination or p in destination.parents for p in (root / '.local', root / '.runtime'))):
        parser.error('repository data must live under .local/ or .runtime/')
    if args.live and (not args.persona or not args.speech_contract):
        parser.error('live review requires explicit persona and speech contract files')
    if args.lossless_intentions and not (args.personal_life or args.free_intentions):
        parser.error('lossless transfer requires written intentions')
    if args.simulated_audience and not args.personal_life:
        parser.error('simulated readers require personal life')
    if args.active_world and not args.personal_life:
        parser.error('active world requires personal life')
    if args.direct_channel and not args.autonomous_posts:
        parser.error('direct channel requires autonomous posts')
    if args.background:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = [sys.executable, '-m', 'persona_runtime.world_lab', '--data-dir', str(destination),
                   '--port', str(args.port), '--max-calls', str(args.max_calls)]
        if args.live:
            command += ['--live', '--persona', str(args.persona.resolve()), '--speech-contract', str(args.speech_contract.resolve())]
        if args.autonomous_posts:
            command += ['--autonomous-posts']
        if args.direct_channel:
            command += ['--direct-channel']
        if args.free_intentions:
            command += ['--free-intentions']
        if args.personal_life:
            command += ['--personal-life']
        if args.research:
            command += ['--research']
        if args.lossless_intentions:
            command += ['--lossless-intentions']
        if args.simulated_audience:
            command += ['--simulated-audience']
        if args.active_world:
            command += ['--active-world']
        fd = os.open(destination / 'lab.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True)
        try:
            from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor
            from http.cookiejar import CookieJar
            client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('background laboratory startup failed')
                try:
                    descriptor = json.loads((destination / 'lab.json').read_text())
                    if descriptor['pid'] == process.pid:
                        origin = 'http://127.0.0.1:' + str(descriptor['port'])
                        client.open(origin, timeout=1).read()
                        state = json.loads(client.open(origin + '/api/state', timeout=1).read())
                        if state['pid'] == process.pid:
                            print(json.dumps({'url': origin, 'pid': process.pid, 'background': True,
                                              'model_enabled': state['model_enabled']}, ensure_ascii=False), flush=True)
                            return
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(.1)
            raise RuntimeError('background laboratory readiness not confirmed')
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            raise
    store = WorldStore(destination)
    backend = None
    research_backend = None
    transport_owner = None
    server = None
    try:
        session = None
        if args.live:
            from .proxy import status, SharedTransport
            from .world_backend import WorldBackend
            if not status()['enabled']:
                raise ValueError('configured proxy required for model review')
            persona, contract = args.persona.read_text(), args.speech_contract.read_text()
            if not persona.strip() or not contract.strip():
                raise ValueError('empty personality/contract')
            transport_owner = SharedTransport()
            backend = WorldBackend(destination / 'transport', transport_factory=transport_owner.borrow)
            if args.research:
                research_backend = WorldBackend(destination / 'research-transport', research_host=True,
                                                transport_factory=transport_owner.borrow, max_known_tokens=50000)
            session = WorldSession(store, backend, {'garik': persona}, contract, args.max_calls,
                                   autonomous_posts=args.autonomous_posts, free_intentions=args.free_intentions,
                                   personal_life=args.personal_life, research_enabled=args.research,
                                   research_backend=research_backend, lossless_intentions=args.lossless_intentions,
                                   simulated_audience=args.simulated_audience, active_world=args.active_world,
                                   direct_channel=args.direct_channel)
        lab = Laboratory(store, session)
        stopping = threading.Event()
        night_worker = threading.Thread(target=lab.night.loop, args=(stopping,), daemon=True)
        night_worker.start()
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(lab))
        # Do not change an existing real daemon or spawn a hidden background worker.
        print(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port),
                          'model_enabled': args.live, 'pid': os.getpid(), 'data': str(destination)}, ensure_ascii=False), flush=True)
        from .app_server import save_private
        save_private(destination / 'lab.json', {'pid': os.getpid(), 'port': server.server_port, 'model_enabled': args.live})
        def stop(*_):
            stopping.set()
            store.pause(True)
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=.2)
        stopping.set()
        night_worker.join(3)
        if lab.worker:
            lab.worker.join(190)
    finally:
        if server:
            server.server_close()
        if backend:
            backend.close()
        if research_backend:
            research_backend.close()
        if transport_owner:
            transport_owner.close()
        store.close()


if __name__ == '__main__':
    main()
