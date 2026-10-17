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
    def __init__(self, answer, unexpected=False, web_calls=0):
        self.answer, self.unexpected = answer, unexpected
        self.web_calls = web_calls
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
            for i in range(self.web_calls):
                self.events.append({'method': 'item/completed', 'params': {**common, 'item': {
                    'type': 'webSearch', 'id': 'web-'+str(i), 'action': {'type': 'search', 'query': 'Synthetic'}}}})
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

    def test_failed_research_outcome_wire_cannot_select_success(self):
        from test_world import outcome
        from persona_runtime.world_protocol import SCHEMAS
        self.backend.client=Client(outcome(status='failed'))
        self.backend.generate('outcome','Synthetic role',{'research_failure':{'status':'failed','facts':[]}})
        start=next(p for m,p in self.backend.client.calls if m=='turn/start')
        self.assertEqual(start['outputSchema']['properties']['status']['enum'],['failed'])
        self.assertIn('succeeded',SCHEMAS['outcome']['properties']['status']['enum'])

    def test_lossless_metadata_labels_host_units_instead_of_retyping_the_text(self):
        self.backend.client = Client({'unit_roles': {'u001':'attempt','u002':'attempt'}})
        result,_=self.backend.generate('intent', 'Technical routing', {'written_intent':'Пойду  домой.'})
        start = next(p for m, p in self.backend.client.calls if m == 'turn/start')
        self.assertIn('unit_roles', start['outputSchema']['properties'])
        self.assertEqual(result['parts'],[{'role':'attempt','text':'Пойду  домой.'}])
        self.assertNotIn('action', start['outputSchema']['properties'])

    def test_resident_plans_cannot_select_empty_or_invented_participants(self):
        self.backend.client=Client({'hooks':[]})
        self.backend.generate('horizon','Synthetic world',{'world':{'actors':[{'id':'hero'}],'entities':[{'id':'street','kind':'place'}]}})
        start=next(p for m,p in self.backend.client.calls if m=='turn/start')
        props=start['outputSchema']['properties']['hooks']['items']['properties']
        self.assertEqual(props['actor_ids']['minItems'],1)
        self.assertEqual(props['actor_ids']['items']['enum'],['hero'])
        self.assertEqual(props['anchor_ids']['minItems'],1)
        self.assertEqual(props['anchor_ids']['items']['enum'],['street'])
        self.assertEqual(props['view_from']['items']['enum'],['street'])
        self.assertNotIn('enum',__import__('persona_runtime.world_protocol',fromlist=['HOOK']).HOOK['properties']['actor_ids']['items'])

    def test_native_plan_id_is_host_assigned_without_changing_scene(self):
        hook={'anchor_ids':['street'],'actor_ids':['hero'],'domain':'city','opens_minute':10,'closes_minute':20,
              'route':'На улице.','npc_want':'Пронести коробку.','friction':'Тесно.','temptation':'Поглядеть.','view_from':['street']}
        self.backend.client=Client({'hooks':[hook]})
        answer,_=self.backend.generate('horizon','Synthetic world',{'world':{'actors':[{'id':'hero'}],'entities':[{'id':'street','kind':'place'}]}})
        wire=next(p['outputSchema'] for m,p in self.backend.client.calls if m=='turn/start')['properties']['hooks']['items']
        self.assertNotIn('id',wire['properties']);self.assertNotIn('id',wire['required'])
        self.assertRegex(answer['hooks'][0]['id'],r'^hook_[0-9a-f]{24}$')
        self.assertEqual({k:v for k,v in answer['hooks'][0].items() if k!='id'},hook)

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

    def reconnecting(self, errors, status='completed'):
        class Reconnecting(Client):
            def rpc(client, method, params, timeout=30):
                value = super().rpc(method, params, timeout)
                if method == 'turn/start':
                    client.events[-1]['params']['turn']['status'] = status
                    for retry in reversed(errors):
                        client.events.appendleft({'method': 'error', 'params': {
                            'threadId': params['threadId'], 'turnId': 'synthetic-turn',
                            'willRetry': retry, 'error': {'message': 'stream disconnected; SECRET-URL-AND-TOKEN'}}})
                return value
        self.backend.client = Reconnecting(event())

    def test_server_reconnect_keeps_same_turn_and_accepts_only_completed_result(self):
        self.reconnecting([True, True])
        answer, _ = self.backend.generate('world', 'Synthetic', {})
        self.assertEqual(answer, event())
        calls = self.backend.client.calls
        self.assertEqual(sum(m == 'turn/start' for m, _ in calls), 1)
        self.assertFalse(any(m == 'turn/interrupt' for m, _ in calls))
        diagnostic = self.backend.last_diagnostics
        self.assertTrue(diagnostic['turn_completed'])
        self.assertEqual(len(diagnostic['events']), 2)
        self.assertNotIn('SECRET-', json.dumps(diagnostic))

    def test_fatal_error_and_non_boolean_retry_flag_do_not_accept_later_answer(self):
        for retry in (False, None, 'true'):
            self.reconnecting([retry])
            with self.assertRaises(CodexError):
                self.backend.generate('world', 'Synthetic', {})
            self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)
            self.assertIsNone(self.backend.last_output)

    def test_failed_completion_or_excess_reconnections_never_commits_answer(self):
        for retries, status in (([True], 'failed'), ([True], 'interrupted'), ([True]*9, 'completed')):
            self.reconnecting(retries, status)
            with self.assertRaises(CodexError):
                self.backend.generate('world', 'Synthetic', {})
            self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)
            self.assertIsNone(self.backend.last_output)

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
            for cls, suffix in [(WorldBackend, 'minimal'), (AppServerBackend, 'production'), (WorldBackend, 'search')]:
                backend = cls(Path(self.temp.name)/suffix, **({'research_host': True} if suffix == 'search' else {}))
                backend._start()
                backend.close()
        self.assertIn('features.code_mode_host=false', commands[0])
        self.assertTrue(any(v.startswith('skills.config=') for v in commands[0]))
        self.assertIn('features.code_mode_host=true', commands[1])
        self.assertFalse(any(v.startswith('skills.config=') for v in commands[1]))
        self.assertIn('features.code_mode_host=true', commands[2])
        self.assertTrue(any(v.startswith('skills.config=') for v in commands[2]))

    def test_research_enables_only_native_web_and_requires_completed_receipts(self):
        self.backend.research_host = True
        value = {'task_id': 'synthetic', 'facts': [], 'caveats': ['Synthetic inconclusive search.']}
        self.backend.client = Client(value, web_calls=1)
        answer, _ = self.backend.generate('research', 'Synthetic research', {'task_id': 'synthetic', 'question': 'Synthetic'})
        self.assertEqual(len(answer['tool_events']), 1)
        start = next(p for m, p in self.backend.client.calls if m == 'thread/start')
        self.assertEqual(start['config']['web_search'], 'live')
        self.assertTrue(start['config']['features.code_mode_host'])
        self.assertNotIn('PRIVATE-CHAIN', json.dumps(answer))
        for count in (0, 3):
            self.backend.client = Client(value, web_calls=count)
            with self.assertRaises(CodexError):
                self.backend.generate('research', 'Synthetic research', {'task_id': 'synthetic', 'question': 'Synthetic'})
            self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)
            self.assertEqual(self.backend.last_diagnostics['failure_reason'],'missing_web_receipt' if count==0 else 'web_call_limit')
            if count==3:
                self.assertEqual(self.backend.last_diagnostics['web_calls_seen'],3)
                self.assertEqual(len(self.backend.last_diagnostics['web_receipts']),2)

    def test_search_cannot_accidentally_run_in_the_character_process(self):
        self.backend.client = Client({'task_id': 'synthetic', 'facts': [], 'caveats': []}, web_calls=1)
        with self.assertRaises(ValueError):
            self.backend.generate('research', 'Research', {'task_id': 'synthetic'})
        self.assertFalse(self.backend.client.calls)

    def test_character_and_search_borrow_one_transport_without_reopening_keychain(self):
        from persona_runtime.proxy import SharedTransport, ProxyError
        opened, closed = [], []
        @contextmanager
        def transport(environment, timeout):
            opened.append(True)
            try: yield {'HTTPS_PROXY': 'http://127.0.0.1:1'}, object()
            finally: closed.append(True)
        owner = SharedTransport()
        a = WorldBackend(Path(self.temp.name)/'voice', transport_factory=owner.borrow)
        b = WorldBackend(Path(self.temp.name)/'web', research_host=True, transport_factory=owner.borrow)
        with patch('persona_runtime.proxy.transport', transport):
            with a.transport_factory({}, timeout=10) as first:
                with b.transport_factory({}, timeout=10) as second:
                    self.assertIs(first[1], second[1])
            self.assertEqual(len(opened), 1)
            self.assertEqual(closed, [])
            owner.close()
            self.assertEqual(len(closed), 1)
            with self.assertRaises(ProxyError):
                with owner.borrow({}): pass


if __name__ == '__main__':
    unittest.main()
