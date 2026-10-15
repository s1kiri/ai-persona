"""Native Codex image service, independent of the parent chat and Image API keys."""
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import uuid

from .app_server import AppServerBackend, StdioClient, cumulative_delta, save_private
from .codex import CodexError, CodexCancelled, cli_environment


class CodexImageBackend(AppServerBackend):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('timeout', 240)
        super().__init__(*args, **kwargs, minimal_tools=True, code_mode_host=True)

    def _feature_permissions(self):
        return {**super()._feature_permissions(), 'image_generation': True}

    def _make_client(self, command, environment, workspace):
        # Native image notifications may contain base64 pixels as well as a
        # savedPath. Text-only processes retain their much smaller ceiling.
        return StdioClient(command, environment, workspace, max_message_chars=60_000_000)

    def render(self, request, destination, references=(), cancel=None):
        if request.get('kind') != 'fictional_photo' or request.get('status') != 'requested':
            raise ValueError('render requires a validated pending world photo request')
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError('never overwrite an existing photograph')
        attempt_path = destination.with_suffix('.attempt.json')
        if attempt_path.exists():
            raise CodexError('image dispatch already recorded; explicit recovery required')
        inputs = [{'type': 'text', 'text': request['prompt']}]
        for reference in references:
            reference = Path(reference).resolve(strict=True)
            inputs.append({'type': 'localImage', 'path': str(reference)})
        self.last_usage = {}
        self._start()
        attempt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Atomic claim also prevents two local workers from dispatching the
        # same destination concurrently, before either receives an image.
        fd = os.open(attempt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'status': 'claimed', 'destination': str(destination)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        result = self.client.rpc('thread/start', {
            'model': self.model, 'cwd': str(self.workspace), 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'ephemeral': True, 'historyMode': 'legacy',
            'baseInstructions': 'You render one fictional world photograph. Use the native image generation tool exactly once. '
                                'Do not use shell, web, apps, publication, or API-key scripts. '
                                'Follow the supplied visual brief. Any attached image is a visual continuity reference. '
                                'Return a generated image, not a plan or a claim that you made one.',
            'config': {**self.overrides, 'web_search': 'disabled', 'features.image_generation': True,
                       'features.code_mode_host': True}})
        thread = result['thread']['id']
        save_private(attempt_path, {'thread_id': thread, 'status': 'pending', 'destination': str(destination),
                                   'prompt_sha256': hashlib.sha256(request['prompt'].encode()).hexdigest()})
        turn = None
        receipt = None
        seen = set()
        try:
            if cancel and cancel.is_set(): raise CodexCancelled('image cancelled before dispatch')
            turn = self.client.rpc('turn/start', {
                'threadId': thread, 'model': self.model, 'effort': self.reasoning_effort,
                'input': inputs, 'approvalPolicy': 'never',
                'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}}, timeout=30)['turn']['id']
            deadline = time.monotonic()+self.timeout
            while True:
                if cancel and cancel.is_set(): raise CodexCancelled('image generation cancelled')
                try: event = self.client.receive(min(deadline, time.monotonic()+.2))
                except CodexError as error:
                    if str(error).startswith('Codex session timed out') and time.monotonic()<deadline: continue
                    raise
                method, data = event.get('method'), event.get('params', {})
                if data.get('threadId') != thread or data.get('turnId') not in (None, turn): continue
                if method == 'thread/tokenUsage/updated':
                    self.last_usage = cumulative_delta(data['tokenUsage']['total'], {})
                    if sum(self.last_usage.get(k, 0) for k in ('input_tokens', 'output_tokens')) >= self.max_known_tokens:
                        raise CodexError('native image call token limit exceeded')
                elif method in ('item/started', 'item/completed'):
                    item = data.get('item', {})
                    if item.get('type') == 'imageGeneration':
                        seen.add(item['id'])
                        if len(seen)>1: raise CodexError('image service exceeded one image operation')
                        if method == 'item/completed': receipt = item
                    elif (item.get('type') == 'functionCallOutput' and item.get('name') in ('exec', 'wait')
                          and item.get('namespace') in (None, 'functions')):
                        pass
                    elif item.get('type') not in ('userMessage', 'reasoning', 'agentMessage'):
                        raise CodexError('unexpected operation in native image service')
                elif method == 'error': raise CodexError('native image stream error; no retry')
                elif method == 'turn/completed':
                    if data['turn']['status'] != 'completed': raise CodexError('image turn did not complete')
                    break
            result = self.save_result(receipt, request, destination, self.last_usage)
            save_private(attempt_path, {'thread_id': thread, 'status': 'completed', 'destination': str(destination)})
            return result
        except Exception as error:
            if turn:
                try: self.client.rpc('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=2)
                except Exception: pass
            # A native image may be durably saved before a large notification
            # is lost. Recover only this fresh thread's single PNG, never a
            # model's prose claim or a different chat's recent image.
            if isinstance(error, CodexError) and str(error).startswith('Codex session ended or exceeded output limits'):
                recovered = self.recover_result(thread, request, destination, self.last_usage)
                if recovered:
                    save_private(attempt_path, {'thread_id': thread, 'status': 'recovered', 'destination': str(destination)})
                    return recovered
            raise
        finally:
            try: self.client.rpc('thread/unsubscribe', {'threadId': thread}, timeout=2)
            except Exception: pass

    @staticmethod
    def save_result(receipt, request, destination, usage):
        if not receipt or receipt.get('status') != 'completed' or receipt.get('failure'):
            raise CodexError('no successful native image receipt')
        saved = receipt.get('savedPath')
        if saved:
            raw = Path(saved).read_bytes()
        else:
            encoded = receipt.get('result', '')
            if encoded.startswith('data:image/'):
                encoded = encoded.split(',', 1)[1]
            try: raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError): raise CodexError('native image result is not image bytes') from None
        metadata = {'renderer': 'native Codex app-server imageGeneration', 'tool_id': receipt['id'],
                    'revised_prompt': receipt.get('revisedPrompt'), 'status': 'generated',
                    'native_receipt_received': True}
        return CodexImageBackend._save_png(raw, request, destination, usage, metadata)

    @staticmethod
    def recover_result(thread, request, destination, usage, generated_root=None):
        # UUID validation prevents traversal and confines recovery to the exact
        # native thread, not a guess based on the newest image in another chat.
        uuid.UUID(thread)
        root = Path(generated_root) if generated_root else Path(cli_environment().get('CODEX_HOME', Path.home()/'.codex'))/'generated_images'
        files = list((root/thread).glob('*.png'))
        if len(files) != 1:
            return None
        return CodexImageBackend._save_png(files[0].read_bytes(), request, destination, usage, {
            'renderer': 'native Codex image generation; recovered saved asset', 'status': 'recovered',
            'native_receipt_received': False, 'turn_completion': 'unknown',
            'thread_id': thread, 'native_saved_path': str(files[0]), 'tool_id': None,
            'revised_prompt': None})

    @staticmethod
    def _save_png(raw, request, destination, usage, metadata):
        if (not raw.startswith(b'\x89PNG\r\n\x1a\n') or not 45 <= len(raw) <= 40_000_000 or
                raw[12:16] != b'IHDR' or raw[-12:] != b'\x00\x00\x00\x00IEND\xaeB\x60\x82'):
            raise CodexError('native image result is not a bounded PNG')
        width, height = struct.unpack('>II', raw[16:24])
        if not 1 <= width*height <= 16_000_000:
            raise CodexError('native image dimensions exceed the media limit')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open('xb') as stream: stream.write(raw)
        metadata = {**metadata, 'kind': 'fictional_photo', 'generated': True, 'published': False,
                    'image_path': str(destination.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'subject_ids': request['subject_ids'], 'source_ids': request['source_ids'],
                    'caption': request['caption'], 'usage': usage,
                    'width': width, 'height': height}
        save_private(destination.with_suffix('.json'), metadata)
        return metadata
