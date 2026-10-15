"""Synthetic Codex stream: bounded schemas, plain speech and no hidden reasoning."""
from collections import deque
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from persona_runtime.world_backend import WorldBackend
from persona_runtime.codex import CodexError, CodexCancelled
from persona_runtime.app_server import DISABLED, PINNED_VERSION
from test_world import event


class Client:
    def __init__(self, answer, unexpected=False):
        self.answer, self.unexpected = answer, unexpected
        self.events, self.calls = deque(), []
        self.serial = 0

    def rpc(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == 'thread/start':
            self.serial += 1
            return {'thread': {'id': 'synthetic-'+str(self.serial)}}
        if method == 'turn/start':
            common = {'threadId': params['threadId'], 'turnId': 'synthetic-turn'}
            self.events.append({'method': 'thread/tokenUsage/updated', 'params': {
                **common, 'tokenUsage': {'total': {'inputTokens': 100, 'outputTokens': 20, 'cachedInputTokens': 50}}}})
            self.events.append({'method': 'item/completed', 'params': {**common, 'item': {
                'type': 'reasoning', 'id': 'reasoning', 'text': 'PRIVATE-CHAIN-MUST-NOT-BE-EXPORTED'}}})
            if self.unexpected:
                self.events.append({'method': 'item/started', 'params': {**common, 'item': {'type': 'functionCall'}}})
            text = json.dumps(self.answer) if isinstance(self.answer, dict) else self.answer
            self.events.append({'method': 'item/completed', 'params': {**common, 'item': {'type': 'agentMessage', 'id': 'answer', 'text': text}}})
            self.events.append({'method': 'turn/completed', 'params': {**common, 'turn': {'id': 'synthetic-turn', 'status': 'completed'}}})
            return {'turn': {'id': 'synthetic-turn'}}
        return {}

    def receive(self, deadline):
        return self.events.popleft()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = WorldBackend(self.temp.name)
        self.backend._start = lambda: 0
        self.backend.workspace = Path(self.temp.name)
        self.backend.overrides = {}

    def tearDown(self):
        self.temp.cleanup()

    def test_structured_world_and_fresh_threads_with_nonduplicated_usage(self):
        self.backend.client = Client(event())
        for _ in range(2):
            value, usage = self.backend.generate('world', 'Synthetic role', {'synthetic': True})
            self.assertEqual(value, event())
            self.assertEqual(usage['input_tokens'], 100)
            self.assertNotIn('PRIVATE-CHAIN', json.dumps(value))
        starts = [p for m, p in self.backend.client.calls if m == 'turn/start']
        self.assertNotEqual(starts[0]['threadId'], starts[1]['threadId'])
        self.assertIn('outputSchema', starts[0])
        self.assertFalse(starts[0]['sandboxPolicy']['networkAccess'])
        self.assertTrue(self.backend.minimal_tools)

    def test_speech_has_no_service_schema(self):
        for stage in ('speech', 'post', 'impulse'):
            self.backend.client = Client('Synthetic character speech.')
            answer, _ = self.backend.generate(stage, 'Synthetic personality', {})
            self.assertEqual(answer, 'Synthetic character speech.')
            start = next(p for m, p in self.backend.client.calls if m == 'turn/start')
            self.assertNotIn('outputSchema', start)

    def test_tool_rejected_without_retry_and_usage_survives(self):
        self.backend.client = Client(event(), unexpected=True)
        with self.assertRaises(CodexError):
            self.backend.generate('world', 'Synthetic role', {})
        self.assertEqual(self.backend.last_usage['input_tokens'], 100)
        self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)

    def test_cancel_before_dispatch_does_not_accept_turn(self):
        self.backend.client = Client(event())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CodexCancelled):
            self.backend.generate('world', 'Synthetic role', {}, cancel)
        self.assertFalse(any(m == 'turn/start' for m, _ in self.backend.client.calls))

    def test_malformed_json_is_not_repaired_by_another_call(self):
        self.backend.client = Client('not json')
        with self.assertRaises(ValueError):
            self.backend.generate('world', 'Synthetic role', {})
        self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)

    def test_minimal_startup_flags_are_opt_in_without_changing_production_default(self):
        from persona_runtime.app_server import AppServerBackend
        commands = []
        class Startup:
            def __init__(self, command, environment, directory):
                commands.append(command)
            def rpc(self, method, params):
                return {'config': {'features': {name: False for name in DISABLED}}} if method == 'config/read' else {}
            def send(self, value): pass
            def close(self): pass
        @contextmanager
        def local_only(environment, **kwargs):
            yield environment, None
        with patch('persona_runtime.app_server.StdioClient', Startup), patch('persona_runtime.app_server.transport', local_only), patch('persona_runtime.app_server.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='codex-cli '+PINNED_VERSION)):
            for cls, suffix in [(WorldBackend, 'minimal'), (AppServerBackend, 'production')]:
                backend = cls(Path(self.temp.name)/suffix)
                backend._start()
                backend.close()
        self.assertIn('features.code_mode_host=false', commands[0])
        self.assertTrue(any(v.startswith('skills.config=') for v in commands[0]))
        self.assertIn('features.code_mode_host=true', commands[1])
        self.assertFalse(any(v.startswith('skills.config=') for v in commands[1]))


if __name__ == '__main__':
    unittest.main()
