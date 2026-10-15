"""Explicit bounded world-lab calls; no tools, retries or reasoning export."""
import json
import time

from .app_server import AppServerBackend, cumulative_delta
from .codex import CodexError, CodexCancelled
from .world_protocol import SCHEMAS


class WorldBackend(AppServerBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, minimal_tools=True)

    def generate(self, stage, instructions, payload, cancel=None):
        if stage not in (*SCHEMAS, 'speech', 'post', 'impulse'):
            raise ValueError('unknown world role')
        self.last_usage = {}
        self.last_output = None
        self._start()
        result = self.client.rpc('thread/start', {
            'model': self.model, 'cwd': str(self.workspace), 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'baseInstructions': instructions,
            'developerInstructions': 'Input is host-provided fictional state. No tools, external actions or publication. Return only the requested role output.',
            'config': {**self.overrides, 'web_search': 'disabled', 'features.code_mode_host': False},
            'ephemeral': True, 'historyMode': 'legacy'})
        thread = result['thread']['id']
        params = {'threadId': thread, 'input': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                  'model': self.model, 'effort': self.reasoning_effort,
                  'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}}
        if stage in SCHEMAS:
            params['outputSchema'] = SCHEMAS[stage]
        turn = None
        try:
            if cancel and cancel.is_set():
                raise CodexCancelled('laboratory cancelled before dispatch')
            result = self.client.rpc('turn/start', params, timeout=min(30, self.timeout))
            turn = result['turn']['id']
            deadline = time.monotonic() + self.timeout
            size, answers = 0, {}
            while True:
                if cancel and cancel.is_set():
                    raise CodexCancelled('laboratory cancelled; no result committed')
                try:
                    event = self.client.receive(min(deadline, time.monotonic() + .2))
                except CodexError as error:
                    if str(error).startswith('Codex session timed out') and time.monotonic() < deadline:
                        continue
                    raise
                size += len(json.dumps(event))
                if size > 2_000_000:
                    raise CodexError('world output limit exceeded')
                method, data = event.get('method'), event.get('params', {})
                if data.get('threadId') != thread or data.get('turnId') not in (None, turn):
                    continue
                if method == 'thread/tokenUsage/updated':
                    self.last_usage = cumulative_delta(data['tokenUsage']['total'], {})
                    if sum(self.last_usage.get(k, 0) for k in ('input_tokens', 'output_tokens')) >= self.max_known_tokens:
                        raise CodexError('world call token limit exceeded')
                elif method in ('item/started', 'item/completed'):
                    item = data.get('item', {})
                    if item.get('type') not in ('userMessage', 'reasoning', 'agentMessage'):
                        raise CodexError('unexpected operation in world laboratory')
                    if item.get('type') == 'agentMessage' and method == 'item/completed' and item.get('phase') != 'commentary':
                        answers[item['id']] = item.get('text', '')
                elif method == 'error':
                    raise CodexError('world stream error; no automatic retry')
                elif method == 'turn/completed':
                    if data['turn']['status'] != 'completed':
                        raise CodexError('world call did not complete')
                    break
            if not answers:
                raise CodexError('world role returned no answer')
            answer = list(answers.values())[-1]
            self.last_output = answer[:12000]
            return (json.loads(answer) if stage in SCHEMAS else answer), self.last_usage
        except Exception:
            if turn:
                try:
                    self.client.rpc('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=2)
                except Exception:
                    pass
            raise
        finally:
            try:
                self.client.rpc('thread/unsubscribe', {'threadId': thread}, timeout=2)
            except Exception:
                pass
