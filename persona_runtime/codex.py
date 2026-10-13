"""Subscription-only Codex CLI adapter. No API fallback and no shell interpolation."""
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile
import queue
import threading
import time
import sys
from contextlib import contextmanager

from .proxy import transport, child_command

from .protocol import SCHEMA, decode


class CodexError(RuntimeError):
    pass


def failure_detail(errors, stderr, returncode):
    """Actionable diagnostics without copying a raw CLI transcript into memory."""
    detail = str(errors[-1])[:500] if errors else ''
    text = (detail + '\n' + stderr).lower()
    if 'workspace routing' in text:
        return ('workspace routing discovery failed: Codex could not resolve the ChatGPT '
                'workspace before inference; run the local doctor command and retry explicitly. '
                'Logged-in status alone does not verify this network step.')
    if any(fragment in text for fragment in ('operation not permitted', 'unable to open database',
                                              'failed to initialize state runtime')):
        return ('Codex cannot access its own service files or start its runtime in this environment. '
                'Try from a regular terminal; character data has not been committed by this turn.')
    if 'unexpected argument' in text or 'unrecognized option' in text:
        return 'Codex CLI is incompatible with the adapter flags; check its version and exec --help.'
    return 'Codex exit %d; run python3 -m persona_runtime doctor for diagnostics.' % returncode


def cli_environment():
    environment = os.environ.copy()
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY'):
        environment.pop(name, None)
    return environment


def configured_model(path=None):
    """Read only a scalar top-level model, never load hooks/plugins or credentials."""
    path = path or Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    for line in lines:
        if line.lstrip().startswith('['):
            break
        match = re.match(r'''^model\s*=\s*("(?:\\.|[^"\\])*"|'[^']*')\s*(?:#.*)?$''', line.strip())
        if match:
            raw = match.group(1)
            try:
                value = json.loads(raw) if raw.startswith('"') else raw[1:-1]
            except ValueError:
                return None
            return value if re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', value) else None
    return None


class CodexCancelled(CodexError):
    pass


class CodexBackend:
    supports_control = True
    def __init__(self, binary=None, model=None, timeout=180):
        bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
        self.binary = binary or shutil.which('codex') or (str(bundled) if bundled.is_file() else 'codex')
        self.model, self.timeout = model or configured_model(), timeout

    @contextmanager
    def connection(self):
        with transport(cli_environment(), timeout=self.timeout) as (env, bridge):
            try:
                self._proxy_bridge = bridge
                yield env
            finally:
                self.last_proxy_report = ({'enabled': True, 'connect_requests': bridge.requests,
                                           'failed_connections': bridge.failures} if bridge else {'enabled': False})

    def doctor(self):
        """Explicit native health check, not a model call. Return only a narrow safe report."""
        try:
            with self.connection() as environment:
                result = subprocess.run(child_command([self.binary, 'doctor', '--json'], self._proxy_bridge), env=environment,
                                        text=True, encoding='utf-8', capture_output=True,
                                        timeout=self.timeout)
        except FileNotFoundError:
            raise CodexError('Codex CLI not found; set --codex to its executable path')
        except subprocess.TimeoutExpired:
            raise CodexError('Codex doctor timed out; no character turn was started')
        try:
            report = json.loads(result.stdout)
            checks = report['checks']
            if not isinstance(checks, dict):
                raise ValueError('invalid checks')
        except (ValueError, KeyError, TypeError):
            raise CodexError(failure_detail([], result.stderr, result.returncode))
        selected = ('installation', 'auth.credentials', 'config.load', 'state.paths',
                    'network.env', 'network.provider_reachability',
                    'network.websocket_reachability', 'desktop.app_server.handshake')
        return {'cli_version': report.get('codexVersion'), 'adapter_model': self.model,
                'model_called': False, 'overall_status': report.get('overallStatus'),
                'proxy': self.last_proxy_report,
                'checks': {key: {'status': checks[key].get('status'),
                                  'summary': 'See status; raw diagnostics suppressed'}
                           for key in selected if isinstance(checks.get(key), dict)},
                'note': 'HTTP/WebSocket reachability does not prove workspace routing or a successful model turn.'}

    def decide(self, prompt, cancel=None, web_search=False, max_web_calls=2, observer=None):
        self.last_usage={}
        # Deliberately outside the source tree. All needed context arrives on stdin.
        with tempfile.TemporaryDirectory(prefix='persona-turn-') as directory:
            workspace = Path(directory)
            schema = workspace / 'decision.schema.json'
            answer = workspace / 'answer.json'
            schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
            command = [self.binary, '--no-daemon'] + (['--search'] if web_search else []) + ['exec', '--ignore-user-config', '--ephemeral',
                       '--skip-git-repo-check', '--sandbox', 'read-only',
                       '-c', 'approval_policy="never"',
                       '-c', 'forced_login_method="chatgpt"',
                       '-c', 'project_doc_max_bytes=0',
                       '-c', 'web_search="live"' if web_search else 'web_search="disabled"',
                       '-c', 'features.shell_tool=false',
                       '-c', 'features.unified_exec=false',
                       '-c', 'features.shell_snapshot=false',
                       '-c', 'features.multi_agent=false',
                       '-c', 'features.apps=false',
                       '-c', 'features.plugins=false',
                       '-c', 'features.hooks=false',
                       '-c', 'features.remote_plugin=false',
                       '-c', 'features.skill_search=false',
                       '-c', 'features.skill_mcp_dependency_install=false',
                       '-c', 'features.computer_use=false',
                       '-c', 'features.browser_use=false',
                       '-c', 'features.image_generation=false',
                       '-c', 'features.code_mode_host=true' if web_search else 'features.code_mode_host=false',
                       '-c', 'features.workspace_dependencies=false',
                       '-c', 'features.sleep_tool=false',
                       '-c', 'features.view_image=false',
                       '-c', 'features.unbounded_connection_retries=false',
                       '-c', 'model_reasoning_effort="low"',
                       '--color', 'never', '--json', '--cd', str(workspace),
                       '--output-schema', str(schema), '--output-last-message', str(answer)]
            if self.model:
                command.extend(['--model', self.model])
            command.append('-')
            try:
                with self.connection() as environment:
                    return self._execute(child_command(command, self._proxy_bridge), prompt, workspace, answer, environment, cancel, web_search, max_web_calls,observer)
            except FileNotFoundError:
                raise CodexError('Codex CLI not found; set --codex to its executable path')

    def _execute(self, command, prompt, workspace, answer, environment, cancel=None, web_search=False, max_web_calls=2,observer=None):
        self.last_usage={}
        self.last_web = {'enabled': web_search, 'calls': 0, 'events': [], 'seen_item_types': []}
        if cancel is not None and cancel.is_set():
            raise CodexCancelled('generation cancelled before start')
        if not 1 <= max_web_calls <= 3:
            raise CodexError('web search limit must be 1..3')
        try:
            supervised=[sys.executable,str(Path(__file__).with_name('child_guard.py')),str(os.getpid()),*command]
            process = subprocess.Popen(supervised, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                       env=environment, cwd=str(workspace), start_new_session=True)
        except FileNotFoundError:
            raise CodexError('Codex CLI not found; set --codex to its executable path')
        messages = queue.Queue()
        def feed():
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (OSError, ValueError):
                pass
        def read(stream, channel):
            try:
                for line in stream:
                    messages.put((channel,line))
            except (OSError, UnicodeError):
                messages.put(('reader_error',''))
            finally:
                messages.put(('eof',channel))
        threads = [threading.Thread(target=feed,daemon=True),
                   threading.Thread(target=read,args=(process.stdout,'stdout'),daemon=True),
                   threading.Thread(target=read,args=(process.stderr,'stderr'),daemon=True)]
        for thread in threads: thread.start()
        usage, errors, diagnostics, ended, output_bytes = {}, [], [], 0, 0
        web_ids = set()
        started = time.monotonic()
        try:
            if observer:observer({'type':'process.started','pid':process.pid})
            while ended < 2 or process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise CodexCancelled('generation cancelled; no decision committed')
                if time.monotonic()-started > self.timeout:
                    raise CodexError('Codex timed out; event retained for explicit retry')
                try:
                    channel,line = messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if channel == 'eof':
                    ended += 1
                    continue
                if channel == 'reader_error':
                    raise CodexError('invalid CLI output encoding; transcript suppressed')
                output_bytes += len(line.encode())
                if output_bytes > 2000000:
                    raise CodexError('CLI output limit exceeded; transcript suppressed')
                if channel == 'stderr':
                    diagnostics.append(line[:1000])
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event,dict):
                    continue
                if event.get('type') == 'turn.completed':
                    usage = event.get('usage',{})
                    self.last_usage=usage
                    if observer:observer({'type':'usage','usage':usage})
                elif event.get('type') in ('error','turn.failed'):
                    error = event.get('error')
                    nested = error.get('message') if isinstance(error,dict) else error
                    errors.append(event.get('message') or nested or 'Codex turn failed')
                elif event.get('type') in ('item.started','item.updated','item.completed'):
                    item = event.get('item',{})
                    kind = item.get('type')
                    if kind and kind not in self.last_web['seen_item_types']:
                        self.last_web['seen_item_types'].append(kind)
                    if kind == 'error':
                        detail = str(item).lower()
                        classifications = ('not available', 'unavailable', 'disabled',
                                           'not supported', 'failed', 'timeout', 'tool')
                        self.last_web.setdefault('error_categories', []).append(
                            [word for word in classifications if word in detail])
                    if kind not in ('reasoning','agent_message','error','web_search','web_search_call'):
                        raise CodexError('unexpected tool use in the restricted local adapter')
                    if kind in ('web_search','web_search_call'):
                        if not web_search:
                            raise CodexError('unexpected web search in a non-search turn')
                        item_id = item.get('id') or 'missing-id'
                        action=item.get('action',{})
                        safe_action={key:value for key,value in action.items() if key in ('type','query','queries','url','pattern','domains')}
                        if observer:observer({'type':'web.tool','id':item_id,'stage':event['type'].split('.')[-1],
                                              'failed':item.get('status') in ('failed','error') or bool(item.get('error')),'action':safe_action})
                        if item_id not in web_ids:
                            web_ids.add(item_id)
                            self.last_web['calls'] = len(web_ids)
                            if len(web_ids) > max_web_calls:
                                raise CodexError('native web search call limit exceeded')
                        if event['type'] == 'item.completed':
                            if item.get('status') in ('failed','error') or item.get('error'):
                                self.last_web.setdefault('error_categories', []).append(['native tool failed'])
                                continue
                            action = item.get('action',{})
                            safe_action = {key: value for key,value in action.items()
                                           if key in ('type','query','queries','url','pattern','domains')}
                            self.last_web['events'].append({'id':item_id,'action':safe_action,'status':'completed'})
            process.wait(timeout=1)
            if process.returncode or errors:
                raise CodexError(failure_detail([], ''.join(diagnostics)+'\n'+'\n'.join(map(str,errors)), process.returncode))
            if web_search and not self.last_web['events']:
                if answer.exists():
                    try:
                        self.last_web['unconfirmed_decision'] = decode(answer.read_text(encoding='utf-8'))
                    except (ValueError,TypeError,OSError):
                        pass
                raise CodexError('requested native search did not complete; no result claimed')
            if self._proxy_bridge and not self._proxy_bridge.requests:
                raise CodexError('Codex returned without using the proxy bridge; result rejected')
            if not answer.exists():
                raise CodexError('Codex returned no structured answer')
            try:
                decision = decode(answer.read_text(encoding='utf-8'))
            except (ValueError,TypeError):
                raise CodexError('invalid structured decision; raw response suppressed')
            return decision,usage
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
            for stream in (process.stdout,process.stderr):
                stream.close()
            for thread in threads:
                thread.join(timeout=.2)
