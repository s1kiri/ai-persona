"""The live-evaluation harness is checked here with a fake transport only."""
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.codex import CodexError
from persona_runtime.store import Store
from tools.evaluate_continuity import run_scenes,SuiteTransport,main
from test_app_server import Backend


class EvaluationHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.store=Store(self.path/'db');self.store.memory.dense=False
        self.backend=Backend(self.path/'db')
    def tearDown(self):
        self.backend.close();self.store.close();self.temp.cleanup()

    def test_three_scenes_use_one_thread_and_keep_private_exact_inputs(self):
        result=run_scenes(self.store,self.backend,self.path)
        self.assertEqual(result['usage']['attempts'],3)
        self.assertEqual([row['status'] for row in result['records']],['completed']*3)
        self.assertEqual(sum(m=='thread/start' for m,_ in self.backend.fake.calls),1)
        self.assertEqual(sum(m=='turn/start' for m,_ in self.backend.fake.calls),3)
        for path in [self.path/'results.json',*self.path.glob('prompt-*.txt')]:
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(len(list(self.path.glob('prompt-*.txt'))),3)

    def test_failure_keeps_usage_and_attempt_identity_without_retry(self):
        self.backend.fake.invalid='commandExecution'
        with self.assertRaises(CodexError):run_scenes(self.store,self.backend,self.path)
        result=json.loads((self.path/'results.json').read_text())
        self.assertEqual(result['usage']['attempts'],1)
        self.assertEqual(result['records'][0]['status'],'failed')
        self.assertEqual(result['records'][0]['attempt'],1)
        self.assertGreater(result['usage']['total_tokens'],0)
        self.assertEqual(sum(m=='turn/start' for m,_ in self.backend.fake.calls),1)

    def test_isolated_backends_borrow_one_bridge_without_reopening_keychain(self):
        events=[];bridge=object()
        @contextmanager
        def transport(environment,timeout):
            events.append('open')
            try:yield {'HTTPS_PROXY':'http://127.0.0.1:12345','NO_PROXY':''},bridge
            finally:events.append('close')
        connection=SuiteTransport()
        with patch('tools.evaluate_continuity.proxy.transport',transport):
            for _ in range(8):
                with connection.borrow({'PATH':'safe','https_proxy':'old','NO_PROXY':'*','CUSTOM_PROXY':'bad'}) as (env,current):
                    self.assertIs(current,bridge)
                    self.assertEqual(env,{'PATH':'safe','HTTPS_PROXY':'http://127.0.0.1:12345','NO_PROXY':''})
            self.assertEqual(events,['open'])
            connection.close()
            self.assertEqual(events,['open','close'])
            with self.assertRaises(RuntimeError):
                with connection.borrow({}):pass

    def test_suite_budget_stops_before_starting_another_case(self):
        destination=self.path/'budget';created=[]
        def backend(path,**kwargs):
            value=Backend(path);created.append(value);return value
        arguments=['evaluate_continuity','--run-live','--suite','extended','--out',str(destination),
                   '--case','agency-pressure','--case','agency-codex','--max-attempts','6']
        with patch('sys.argv',arguments),patch('tools.evaluate_continuity.artifact_path',return_value=destination),\
             patch('tools.evaluate_continuity.proxy.status',return_value={'enabled':True}),\
             patch('tools.evaluate_continuity.AppServerBackend',side_effect=backend):
            main()
        result=json.loads((destination/'suite.json').read_text())
        self.assertEqual(result['status'],'budget_exhausted')
        self.assertEqual(result['attempts'],5)
        self.assertEqual(len(created),1)
        self.assertFalse((destination/'agency-codex').exists())

if __name__=='__main__':unittest.main()
