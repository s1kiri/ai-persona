"""Runtime guarantees, using a clearly synthetic backend; no paid/model calls."""
import copy
import json
import os
import pty
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from persona_runtime import proxy

from persona_runtime.codex import CodexBackend, CodexError, CodexCancelled, configured_model, failure_detail
from persona_runtime.engine import Engine
from persona_runtime.protocol import validate
from persona_runtime.store import Store, worker_lock


def decision():
    return {'reply': 'Synthetic test reply.', 'state': 'Local test, no physical actions.',
            'goals': 'OPEN: remember the conversation.', 'wake_after_minutes': 30,
            'memories': [{'key': 'likes', 'kind': 'person', 'basis': 'reported',
                          'text': 'Speaker reports liking hot sauce.'}]}


class SyntheticBackend:
    def __init__(self):
        self.prompts = []

    def decide(self, prompt):
        self.prompts.append(prompt)
        return decision(), {'input_tokens': 12, 'output_tokens': 8}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.backend = SyntheticBackend()
        self.engine = Engine(self.store, self.backend)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, **kwargs):
        return self.store.enqueue('I like hot sauce.', **kwargs)

    def test_stable_id_is_idempotent_and_conflicting_contents_rejected(self):
        self.enqueue(event_id='source-1')
        self.enqueue(event_id='source-1')
        with self.assertRaises(ValueError):
            self.store.enqueue('different', event_id='source-1')
        self.assertEqual(self.store.status()['events'], {'pending': 1})

    def test_memory_and_pending_reply_survive_restart_and_do_not_rerun_model(self):
        event_id = self.enqueue()
        self.assertEqual(self.engine.step(), event_id)
        self.store.close()
        self.store = Store(self.directory)
        self.engine = Engine(self.store, self.backend)
        self.assertIsNone(self.engine.step())
        self.assertEqual(len(self.backend.prompts), 1)
        self.assertEqual(self.store.status()['memories'], 1)
        self.assertEqual(self.store.status()['queued_replies'], 1)
        self.assertEqual(self.store.get('goals'), decision()['goals'])

    def test_delivery_is_explicit_and_context_does_not_claim_unsent_reply(self):
        self.enqueue()
        self.engine.step()
        event_id = self.enqueue()
        event = dict(self.store.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone())
        self.assertIsNone(self.store.context(event)['history'][0]['reply'])
        output_id = self.store.pending_output()[0]['id']
        self.store.acknowledge(output_id)
        self.assertEqual(self.store.context(event)['history'][0]['reply'], decision()['reply'])
        with self.assertRaises(ValueError):
            self.store.acknowledge(output_id)

    def test_invalid_decision_cannot_partially_change_state_memory_or_outbox(self):
        self.enqueue()
        event = self.store.claim()
        original = self.store.get('state')
        invalid = decision()
        invalid['memories'].append({'key': 'bad', 'kind': 'unknown', 'basis': 'observed', 'text': 'bad'})
        with self.assertRaises(ValueError):
            self.store.complete(event, invalid, {})
        self.assertEqual(self.store.get('state'), original)
        self.assertEqual(self.store.status()['memories'], 0)
        self.assertEqual(self.store.pending_output(), [])

    def test_sql_failure_rolls_back_all_actions(self):
        self.enqueue()
        event = self.store.claim()
        self.store.db.execute("CREATE TRIGGER fail_turn BEFORE INSERT ON turns BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.store.db.commit()
        original = self.store.get('state')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.complete(event, decision(), {})
        self.assertEqual(self.store.get('state'), original)
        self.assertEqual(self.store.status()['memories'], 0)
        self.assertEqual(self.store.pending_output(), [])

    def test_repeated_complete_does_not_duplicate_reply(self):
        self.enqueue()
        event = self.store.claim()
        self.store.complete(event, decision(), {})
        with self.assertRaises(ValueError):
            self.store.complete(event, decision(), {})
        self.assertEqual(len(self.store.pending_output()), 1)

    def test_interrupted_turn_recovered_but_not_after_attempt_limit(self):
        self.enqueue()
        for _ in range(3):
            self.assertIsNotNone(self.store.claim())
            self.store.recover()
        self.assertEqual(self.store.status()['events'], {'failed': 1})
        self.assertIsNone(self.store.claim())

    def test_failures_require_explicit_retry(self):
        class BrokenBackend:
            def decide(self, prompt):
                raise RuntimeError('synthetic limit exhausted')
        event_id = self.enqueue()
        broken = Engine(self.store, BrokenBackend())
        with self.assertRaises(RuntimeError):
            broken.step()
        self.assertIsNone(broken.step())
        self.assertEqual(self.store.status()['queued_replies'], 0)
        self.store.retry(event_id)
        self.assertEqual(self.engine.step(), event_id)

    def test_memories_follow_person_but_history_stays_in_conversation(self):
        self.enqueue(person='alice', conversation='one')
        self.engine.step()
        for person, conversation in (('bob', 'one'), ('alice', 'two')):
            event_id = self.enqueue(person=person, conversation=conversation)
            event = dict(self.store.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone())
            context = self.store.context(event)
            if person == 'bob':
                self.assertEqual(context['memories'], [])
            else:
                self.assertTrue(any('hot sauce' in note['text'] for note in context['memories']))
            self.assertEqual(context['history'], [])

    def test_memory_update_keeps_provenance(self):
        self.enqueue()
        self.engine.step()
        event_id = self.enqueue()
        self.engine.step()
        rows = list(self.store.db.execute('SELECT * FROM memories'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['basis'], 'reported')
        self.assertEqual(rows[0]['source_event'], event_id)

    def test_wake_polling_is_idempotent_and_respects_deadline(self):
        self.enqueue()
        self.engine.step()
        self.store.acknowledge(self.store.pending_output()[0]['id'])
        due = float(self.store.get('next_wake'))
        self.assertIsNone(self.engine.wake_if_due(now=due - 1))
        first = self.engine.wake_if_due(now=due)
        second = self.engine.wake_if_due(now=due + 1)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.store.status()['events']['pending'], 1)

    def test_due_wake_does_not_queue_behind_new_message(self):
        self.enqueue()
        self.engine.step()
        due = float(self.store.get('next_wake'))
        self.enqueue()
        self.assertIsNone(self.engine.wake_if_due(now=due + 1))
        self.assertEqual(self.store.status()['events']['pending'], 1)

    def test_process_lock_rejects_second_worker(self):
        with worker_lock(self.directory):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.directory):
                    pass

    def test_backup_restores_completed_turns_and_pending_delivery(self):
        self.enqueue()
        self.engine.step()
        backup = self.directory / 'backup.sqlite3'
        self.store.backup(backup)
        restored_dir = self.directory / 'restored'
        restored_dir.mkdir()
        backup.rename(restored_dir / 'runtime.sqlite3')
        restored = Store(restored_dir)
        try:
            self.assertEqual(restored.status(), self.store.status())
            self.assertEqual(restored.get('state'), self.store.get('state'))
            Engine(restored, self.backend)
        finally:
            restored.close()

    def test_prompt_uses_full_approved_lexicons_and_no_fake_chat_scene(self):
        self.enqueue(register='free')
        event = self.store.claim()
        prompt = self.engine.prompt(event)
        root = Path(__file__).resolve().parent.parent
        for name in ('obscene.md', 'non-obscene.md'):
            self.assertIn((root / 'resources/lexicon' / name).read_text().strip(), prompt)
        self.assertNotIn('# Текущая встреча', prompt)
        self.assertIn('Telegram sending, purchases and physical actions are NOT connected', prompt)

    def test_malformed_actions_and_boolean_wake_are_rejected(self):
        for mutate in (lambda d: d.update(wake_after_minutes=True),
                       lambda d: d.update(reply=''),
                       lambda d: d.update(shell='echo bad'),
                       lambda d: d.update(state='x' * 6001)):
            value = copy.deepcopy(decision())
            mutate(value)
            with self.assertRaises(ValueError):
                validate(value)

    def test_cli_queue_restart_and_status_without_model(self):
        root = Path(__file__).resolve().parent.parent
        command = [sys.executable, '-m', 'persona_runtime', '--data-dir', str(self.directory / 'cli')]
        queued = subprocess.run(command + ['send', 'Тест.', '--event-id', 'cli-1'],
                                cwd=str(root), text=True, capture_output=True)
        self.assertEqual(queued.returncode, 0, queued.stderr)
        status = subprocess.run(command + ['status'], cwd=str(root), text=True, capture_output=True)
        self.assertEqual(json.loads(status.stdout)['events'], {'pending': 1})


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "absent-proxy.json"
        guard = patch("persona_runtime.codex.transport", side_effect=lambda env, **kw: proxy.transport(env, path))
        guard.start()
        self.addCleanup(guard.stop)

    def test_failures_distinguish_routing_local_permissions_and_cli_flags(self):
        self.assertIn('before inference', failure_detail(['workspace routing discovery failed'], '', 1))
        self.assertIn('service files', failure_detail([], 'Operation not permitted (os error 1)', 1))
        self.assertIn('incompatible', failure_detail([], "unexpected argument '--ignore-user-config'", 2))
        self.assertNotIn('private transcript', failure_detail([], 'private transcript', 1))

    def test_doctor_returns_only_selected_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            report = {'codexVersion': 'synthetic', 'overallStatus': 'ok', 'checks': {
                'auth.credentials': {'status': 'ok', 'summary': 'configured', 'details': {'secret': 'not for output'}},
                'unrelated': {'status': 'ok', 'summary': 'not for output'}}}
            binary = self.fake_cli(directory, 'import sys\nassert sys.argv[1:] == ["doctor", "--json"]\nprint(%r)\n' % json.dumps(report))
            result = CodexBackend(binary, model='synthetic-model').doctor()
            self.assertEqual(result['checks'], {'auth.credentials': {'status': 'ok', 'summary': 'See status; raw diagnostics suppressed'}})
            self.assertEqual(result['adapter_model'], 'synthetic-model')
            self.assertFalse(result['model_called'])
            self.assertNotIn('not for output', json.dumps(result))

    def test_only_base_model_is_read_from_user_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('model = "test-model" # selected\n[profiles.other]\nmodel = "other-model"\n')
            self.assertEqual(configured_model(path), 'test-model')
            path.write_text('[profiles.other]\nmodel = "other-model"\n')
            self.assertIsNone(configured_model(path))
            path.write_text("model = 'test-model'\n")
            self.assertEqual(configured_model(path), 'test-model')

    def fake_cli(self, directory, body):
        path = Path(directory) / 'synthetic-codex'
        # Kernel shebang parsing cannot handle the spaces in this project's venv path.
        path.write_text('#!' + str(Path(sys.executable).resolve()) + '\n' + body, encoding='utf-8')
        path.chmod(0o700)
        return str(path)

    def test_adapter_stdin_schema_auth_and_tool_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.fake_cli(directory, '''import json, os, pathlib, sys
args = sys.argv
assert '--ignore-user-config' in args and '--ephemeral' in args
assert 'model_reasoning_effort="medium"' in args
assert args[args.index('--model')+1] == 'gpt-6.1-sol'
assert '--no-daemon' in args
assert 'forced_login_method="chatgpt"' in args
assert 'features.shell_tool=false' in args
assert args[args.index('--sandbox')+1] == 'read-only'
assert not any(os.environ.get(k) for k in ('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY'))
assert sys.stdin.read() == 'synthetic prompt'
schema = json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
assert schema['additionalProperties'] is False
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(%r)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3}}))
''' % json.dumps(decision()))
            result, usage = CodexBackend(binary).decide('synthetic prompt')
            self.assertEqual(result, decision())
            self.assertEqual(usage, {'input_tokens': 3})

    def test_missing_binary_and_invalid_response_are_not_successes(self):
        with self.assertRaises(CodexError):
            CodexBackend('/nonexistent/synthetic-codex').decide('test')
        with tempfile.TemporaryDirectory() as directory:
            binary = self.fake_cli(directory, '''import pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{}')
''')
            with self.assertRaises(CodexError):
                CodexBackend(binary).decide('test')

    def test_timeout_kills_process_and_retains_event_for_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self.fake_cli(directory, 'import time\ntime.sleep(10)\n')
            with self.assertRaisesRegex(CodexError, 'timed out'):
                CodexBackend(binary, timeout=0.1).decide('test')

    def test_native_cancellation_terminates_process_without_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            pid = Path(directory)/'pid'
            binary = self.fake_cli(directory, 'import os,time,pathlib\npathlib.Path(%r).write_text(str(os.getpid()))\ntime.sleep(30)\n' % str(pid))
            cancellation = threading.Event()
            def cancel_after_start():
                deadline=time.monotonic()+3
                while not pid.exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                cancellation.set()
            timer = threading.Thread(target=cancel_after_start)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(CodexCancelled):
                    CodexBackend(binary, timeout=10).decide('test',cancel=cancellation)
            finally:
                timer.join()
            self.assertLess(time.monotonic()-started,3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid.read_text()),0)

    def test_native_web_events_count_calls_and_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            body = '''import pathlib,sys,json
assert '--search' in sys.argv
assert 'web_search="live"' in sys.argv
for kind in ('item.started','item.completed'):
 print(json.dumps({'type':kind,'item':{'id':'web-1','type':'web_search','action':{'type':'search','query':'synthetic','unsafe':'not retained'}}}),flush=True)
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)
''' % json.dumps(decision())
            backend=CodexBackend(self.fake_cli(directory,body))
            backend.decide('test',web_search=True)
            self.assertEqual(backend.last_web['calls'],1)
            self.assertNotIn('unsafe',json.dumps(backend.last_web))
            with self.assertRaisesRegex(CodexError,'unexpected web search'):
                # Don't use the flag assertion when checking an unwanted tool.
                other=self.fake_cli(directory,body.replace("assert '--search' in sys.argv\n",'').replace("assert 'web_search=\"live\"' in sys.argv\n",''))
                CodexBackend(other).decide('test')

    def test_native_web_limit_stops_before_answer_and_unavailable_never_claims_success(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=self.fake_cli(directory,'''import json,time
for i in range(3):
 print(json.dumps({'type':'item.started','item':{'id':str(i),'type':'web_search'}}),flush=True)
time.sleep(30)
''')
            with self.assertRaisesRegex(CodexError,'call limit'):
                CodexBackend(binary,timeout=10).decide('test',web_search=True,max_web_calls=2)
            binary=self.fake_cli(directory,'''import pathlib,sys,json
print(json.dumps({'type':'item.completed','item':{'id':'err','type':'error','message':'web unavailable; private-secret'}}))
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)
'''%json.dumps(decision()))
            backend=CodexBackend(binary)
            with self.assertRaisesRegex(CodexError,'no result claimed'):
                backend.decide('test',web_search=True)
            self.assertEqual(backend.last_web['events'],[])
            self.assertNotIn('private-secret',json.dumps(backend.last_web))

    def test_failed_native_web_item_is_not_a_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=self.fake_cli(directory,'''import pathlib,sys,json
print(json.dumps({'type':'item.completed','item':{'id':'web-1','type':'web_search','status':'failed','error':'private detail'}}))
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)
'''%json.dumps(decision()))
            backend=CodexBackend(binary)
            with self.assertRaisesRegex(CodexError,'no result claimed'):
                backend.decide('test',web_search=True)
            self.assertEqual(backend.last_web['calls'],1)
            self.assertEqual(backend.last_web['events'],[])
            self.assertNotIn('private detail',json.dumps(backend.last_web))

    def test_stream_error_receipt_preserves_sequence_without_private_cli_text(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=self.fake_cli(directory,'''import pathlib,sys,json
print(json.dumps({'type':'error','message':'Reconnecting... 1/5 (stream disconnected before completion: https://user:private-secret@example.test)'}))
print(json.dumps({'type':'item.completed','item':{'type':'web_search','id':'search-after-reconnect','action':{'type':'search','query':'synthetic'}}}))
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':3}}))
'''%json.dumps(decision()))
            backend=CodexBackend(binary);observed=[]
            value,usage=backend.decide('test',observer=observed.append,web_search=True)
            self.assertEqual(value['reply'],decision()['reply'])
            receipt=backend.last_diagnostics
            self.assertTrue(receipt['turn_completed']);self.assertTrue(receipt['answer_valid'])
            self.assertFalse(receipt['turn_failed']);self.assertEqual(receipt['exit_code'],0)
            self.assertTrue(receipt['retry_recovered'])
            self.assertEqual(receipt['events'][0]['retry_attempt'],1)
            self.assertIn('stream_disconnected',receipt['events'][0]['categories'])
            self.assertNotIn('private-secret',json.dumps([receipt,observed]))
            self.assertEqual(backend.last_usage['input_tokens'],11)

    def test_unknown_failed_and_late_errors_are_not_recovered_from_exit_zero(self):
        cases=[
            [{'type':'error','message':'unknown private-secret'}],
            [{'type':'error','message':'fatal unrelated error mentions Reconnecting... 1/5'}],
            [{'type':'turn.failed','error':{'message':'Selected model is at capacity. private-secret'}}],
            [{'type':'error','message':'Reconnecting... 1/5 (stream disconnected)'},
             {'type':'turn.failed','error':{'message':'failed private-secret'}}],
        ]
        for events in cases:
            with self.subTest(events=events),tempfile.TemporaryDirectory() as directory:
                body='import pathlib,sys,json\n'
                body+='\n'.join('print(%r)'%json.dumps(event) for event in events)+'\n'
                body+="pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)\n"%json.dumps(decision())
                body+="print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11}}))\n"
                backend=CodexBackend(self.fake_cli(directory,body))
                with self.assertRaises(CodexError):backend.decide('test')
                self.assertFalse(backend.last_diagnostics['retry_recovered'])
                self.assertNotIn('private-secret',json.dumps(backend.last_diagnostics))
        with tempfile.TemporaryDirectory() as directory:
            body="import pathlib,sys,json\npathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)\n"%json.dumps(decision())
            body+="print(json.dumps({'type':'turn.completed','usage':{}}))\nprint(json.dumps({'type':'error','message':'Reconnecting... 1/5'}))\n"
            with self.assertRaises(CodexError):CodexBackend(self.fake_cli(directory,body)).decide('test')

    def test_reconnect_needs_valid_answer_completion_and_zero_exit(self):
        for completed,valid,code in ((False,True,0),(True,False,0),(True,True,1)):
            with self.subTest(completed=completed,valid=valid,code=code),tempfile.TemporaryDirectory() as directory:
                body="import pathlib,sys,json\nprint(json.dumps({'type':'error','message':'Reconnecting... 1/5'}))\n"
                body+="pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(%r)\n"%json.dumps(decision() if valid else {})
                if completed:body+="print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}))\n"
                body+='sys.exit(%d)\n'%code
                backend=CodexBackend(self.fake_cli(directory,body))
                with self.assertRaises(CodexError):backend.decide('test')
                self.assertFalse(backend.last_diagnostics['retry_recovered'])

    def test_real_tty_loop_accepts_input_during_native_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)/'data';started=Path(directory)/'started'
            from fake_app_server import make_cli
            binary=make_cli(directory,decision(),started=started,delay=1)
            code='''from unittest.mock import patch
from persona_runtime import proxy
from persona_runtime.__main__ import main
with patch('persona_runtime.app_server.transport',side_effect=lambda env,**kw:proxy.transport(env,%r)):
 raise SystemExit(main(%r))
'''%(str(Path(directory)/'absent.json'),['--data-dir',str(data),'--codex',binary,'--no-web','chat','--no-autonomy','--max-calls','2'])
            master,slave=pty.openpty()
            process=subprocess.Popen([sys.executable,'-c',code],stdin=slave,stdout=slave,stderr=slave)
            os.close(slave)
            try:
                os.write(master,b'old input\n')
                deadline=time.monotonic()+5
                while not started.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(started.exists(),'native generation did not start')
                os.write(master,b'new input\n')
                process.wait(timeout=8)
                self.assertEqual(process.returncode,0)
                with sqlite3.connect(data/'runtime.sqlite3') as database:
                    rows=database.execute('SELECT text,status FROM events ORDER BY rowid').fetchall()
                    self.assertEqual(rows,[('old input','superseded'),('new input','done')])
                    self.assertEqual(database.execute('SELECT count(*) FROM turns').fetchone()[0],1)
                    self.assertEqual(database.execute("SELECT count(*) FROM outbox WHERE status='delivered'").fetchone()[0],1)
            finally:
                if process.poll() is None:process.kill();process.wait()
                os.close(master)

    def test_watch_delivers_delayed_parts_after_call_budget_without_an_extra_model(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)/'data'
            store=Store(data);store.enqueue('Synthetic user message');store.close()
            value=decision();value['reply']=None
            value['messages']=[{'text':'one','delay_seconds':0},{'text':'two','delay_seconds':1}]
            from fake_app_server import make_cli
            binary=make_cli(directory,value)
            from persona_runtime.__main__ import main
            import io
            from contextlib import redirect_stdout
            started=time.monotonic()
            with redirect_stdout(io.StringIO()),patch('persona_runtime.app_server.transport',side_effect=lambda env,**kw:proxy.transport(env,Path(directory)/'absent.json')):
                result=main(['--data-dir',str(data),'--codex',binary,'--no-web','watch','--max-turns','1','--max-minutes','1'])
            self.assertEqual(result,0)
            self.assertLess(time.monotonic()-started,6)
            with sqlite3.connect(data/'runtime.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM model_attempts').fetchone()[0],1)
                delivered=db.execute('SELECT delivered FROM outbox ORDER BY ordinal').fetchall()
                self.assertEqual(len(delivered),2)
                self.assertGreaterEqual(delivered[1][0]-delivered[0][0],1)


if __name__ == '__main__':
    unittest.main()
