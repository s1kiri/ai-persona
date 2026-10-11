"""Subscription-only Codex CLI adapter. No API fallback and no shell interpolation."""
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile

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
    return detail or 'Codex exit %d; run python3 -m persona_runtime doctor for diagnostics.' % returncode


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


class CodexBackend:
    def __init__(self, binary=None, model=None, timeout=180):
        bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
        self.binary = binary or shutil.which('codex') or (str(bundled) if bundled.is_file() else 'codex')
        self.model, self.timeout = model or configured_model(), timeout

    def doctor(self):
        """Explicit native health check, not a model call. Return only a narrow safe report."""
        try:
            result = subprocess.run([self.binary, 'doctor', '--json'], env=cli_environment(),
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
                'checks': {key: {field: checks[key].get(field) for field in ('status', 'summary')}
                           for key in selected if isinstance(checks.get(key), dict)},
                'note': 'HTTP/WebSocket reachability does not prove workspace routing or a successful model turn.'}

    def decide(self, prompt):
        # Deliberately outside the source tree. All needed context arrives on stdin.
        with tempfile.TemporaryDirectory(prefix='persona-turn-') as directory:
            workspace = Path(directory)
            schema = workspace / 'decision.schema.json'
            answer = workspace / 'answer.json'
            schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
            command = [self.binary, 'exec', '--ignore-user-config', '--ephemeral',
                       '--skip-git-repo-check', '--sandbox', 'read-only',
                       '-c', 'approval_policy="never"',
                       '-c', 'forced_login_method="chatgpt"',
                       '-c', 'project_doc_max_bytes=0',
                       '-c', 'web_search="disabled"',
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
                       '-c', 'features.code_mode_host=false',
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
            environment = cli_environment()
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                           env=environment, cwd=str(workspace), start_new_session=True)
                try:
                    stdout, stderr = process.communicate(prompt, timeout=self.timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    raise
            except FileNotFoundError:
                raise CodexError('Codex CLI not found; set --codex to its executable path')
            except subprocess.TimeoutExpired:
                raise CodexError('Codex timed out; event retained for explicit retry')
            usage = {}
            errors = []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'turn.completed':
                    usage = event.get('usage', {})
                elif event.get('type') in ('error', 'turn.failed'):
                    error = event.get('error')
                    nested = error.get('message') if isinstance(error, dict) else error
                    errors.append(event.get('message') or nested or 'Codex turn failed')
                elif event.get('type') == 'item.completed':
                    item = event.get('item', {})
                    if item.get('type') in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change'):
                        raise CodexError('unexpected tool use in the restricted local adapter')
            if process.returncode or errors:
                # Do not persist raw model output, environment, or diagnostic transcripts.
                raise CodexError(failure_detail(errors, stderr, process.returncode))
            if not answer.exists():
                raise CodexError('Codex returned no structured answer')
            try:
                decision = decode(answer.read_text(encoding='utf-8'))
            except (ValueError, TypeError) as exc:
                raise CodexError('invalid structured decision: %s' % exc)
            return decision, usage
