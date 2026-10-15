"""Native image receipt, narrow permissions and no parent-chat dependency."""
import base64
from collections import deque
from pathlib import Path
import tempfile
import threading
import unittest
import sys
import time

from persona_runtime.app_server import AppServerBackend, StdioClient
from persona_runtime.codex import CodexError, CodexCancelled
from persona_runtime.world_image_backend import CodexImageBackend
from test_world_backend import Client

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZkAAAAASUVORK5CYII=')


class ImageClient(Client):
    def __init__(self, items):
        super().__init__('I generated it.')
        self.items = items

    def rpc(self, method, params, timeout=30):
        result = super().rpc(method, params, timeout)
        if method == 'turn/start':
            common = {'threadId': params['threadId'], 'turnId': 'synthetic-turn'}
            prior = list(self.events)
            self.events = deque(prior[:2]+[{'method': 'item/completed', 'params': {**common, 'item': item}}
                                          for item in self.items]+prior[2:])
        return result


class NativeImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = CodexImageBackend(self.root/'transport')
        self.backend._start = lambda: 0
        self.backend.workspace = self.root
        self.backend.overrides = {}
        self.request = {'kind': 'fictional_photo', 'status': 'requested', 'prompt': 'Synthetic photo.',
                        'subject_ids': ['map'], 'source_ids': ['outcome'], 'caption': 'My text.'}
        self.receipt = {'type': 'imageGeneration', 'id': 'img-1', 'status': 'completed',
                        'result': base64.b64encode(PNG).decode(), 'savedPath': None, 'revisedPrompt': 'Synthetic prompt.'}
        self.destination = self.root/'media/image.png'

    def tearDown(self):
        self.temp.cleanup()

    def test_only_image_worker_enables_image_generation(self):
        ordinary = AppServerBackend(self.root/'ordinary')
        self.assertFalse(any(ordinary._feature_permissions().values()))
        allowed = self.backend._feature_permissions()
        self.assertEqual([k for k, v in allowed.items() if v], ['image_generation'])

    def test_native_receipt_produces_real_file_and_usage_without_parent_chat(self):
        self.backend.client = ImageClient([self.receipt])
        result = self.backend.render(self.request, self.destination)
        self.assertEqual(self.destination.read_bytes(), PNG)
        self.assertEqual(result['source_ids'], ['outcome'])
        self.assertEqual(result['width'], 1)
        self.assertEqual(result['usage']['input_tokens'], 100)
        self.assertFalse(result['published'])
        starts = [p for m, p in self.backend.client.calls if m == 'thread/start']
        self.assertTrue(starts[0]['config']['features.image_generation'])
        self.assertNotIn('PRIVATE-CHAIN', str(result))

    def test_saved_path_receipt_is_copied_without_deleting_original(self):
        original = self.root/'native.png'; original.write_bytes(PNG)
        result = self.backend.save_result({**self.receipt, 'savedPath': str(original), 'result': ''},
                                         self.request, self.destination, {})
        self.assertTrue(result['generated'])
        self.assertEqual(original.read_bytes(), PNG)
        with self.assertRaises(FileExistsError):
            self.backend.save_result(self.receipt, self.request, self.destination, {})

    def test_text_claim_without_image_receipt_is_not_success(self):
        self.backend.client = ImageClient([])
        with self.assertRaises(CodexError): self.backend.render(self.request, self.destination)
        self.assertFalse(self.destination.exists())

    def test_other_tool_and_second_image_are_rejected_without_retry(self):
        for index, items in enumerate(([{'type': 'webSearch'}], [self.receipt, {**self.receipt, 'id': 'img-2'}])):
            self.backend.client = ImageClient(items)
            destination = self.root/f'bad-{index}.png'
            with self.assertRaises(CodexError): self.backend.render(self.request, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(sum(m == 'turn/start' for m, _ in self.backend.client.calls), 1)

    def test_failed_and_fake_pixels_never_create_a_ready_asset(self):
        for receipt in ({**self.receipt, 'status': 'failed'},
                        {**self.receipt, 'failure': {'message': 'failed'}},
                        {**self.receipt, 'result': base64.b64encode(b'not a photograph').decode()}):
            with self.assertRaises(CodexError):
                self.backend.save_result(receipt, self.request, self.destination, {})
            self.assertFalse(self.destination.exists())

    def test_cancellation_before_dispatch_does_not_make_an_image(self):
        self.backend.client = ImageClient([self.receipt])
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(CodexCancelled): self.backend.render(self.request, self.destination, cancel=cancel)
        self.assertFalse(any(m == 'turn/start' for m, _ in self.backend.client.calls))

    def test_restart_does_not_dispatch_a_recorded_image_again(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.with_suffix('.attempt.json').write_text('{}')
        self.backend.client = ImageClient([self.receipt])
        with self.assertRaises(CodexError): self.backend.render(self.request, self.destination)
        self.assertEqual(self.backend.client.calls, [])

    def test_recovery_uses_only_own_thread_and_preserves_missing_receipt(self):
        tid = '11111111-1111-4111-8111-111111111111'
        generated = self.root/'generated'; (generated/tid).mkdir(parents=True)
        (generated/tid/'native.png').write_bytes(PNG)
        result = self.backend.recover_result(tid, self.request, self.destination, {}, generated)
        self.assertEqual(result['status'], 'recovered')
        self.assertFalse(result['native_receipt_received'])
        self.assertEqual(result['turn_completion'], 'unknown')
        self.assertTrue((generated/tid/'native.png').exists())
        other = '22222222-2222-4222-8222-222222222222'
        self.assertIsNone(self.backend.recover_result(other, self.request, self.root/'absent.png', {}, generated))
        with self.assertRaises(ValueError):
            self.backend.recover_result('../elsewhere', self.request, self.root/'bad.png', {}, generated)

    def test_ambiguous_native_assets_cannot_be_guessed(self):
        tid = '11111111-1111-4111-8111-111111111111'
        generated = self.root/'generated'; (generated/tid).mkdir(parents=True)
        for name in ('one.png', 'two.png'): (generated/tid/name).write_bytes(PNG)
        self.assertIsNone(self.backend.recover_result(tid, self.request, self.destination, {}, generated))

    def test_large_notification_passes_only_the_image_transport(self):
        # Actual subprocess transport, with no model or network. Reproduces
        # the old text ceiling and checks the image-only larger ceiling.
        code = 'import json; print(json.dumps({"pixels":"x"*2100000}),flush=True); input()'
        for limit, succeeds in ((2_000_000, False), (60_000_000, True)):
            with self.subTest(limit=limit):
                client = StdioClient([sys.executable, '-c', code], {}, str(self.root), max_message_chars=limit)
                try:
                    if succeeds:
                        self.assertEqual(len(client.receive(time.monotonic()+5)['pixels']), 2_100_000)
                    else:
                        with self.assertRaises(CodexError): client.receive(time.monotonic()+5)
                finally: client.close()
