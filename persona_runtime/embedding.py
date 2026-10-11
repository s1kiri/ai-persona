"""Small local ONNX encoder. Chat never downloads weights or calls an embedding API."""
from pathlib import Path
import hashlib
import json
import os

MODEL_REPO = 'Xenova/multilingual-e5-small'
MODEL_REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
MODEL_FILES = ('onnx/model_quantized.onnx', 'tokenizer.json', 'tokenizer_config.json', 'config.json')
MODEL_DIR = Path(__file__).resolve().parent.parent / '.runtime/models/multilingual-e5-small'


def setup(directory=MODEL_DIR):
    from huggingface_hub import snapshot_download
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_download(MODEL_REPO, revision=MODEL_REVISION, allow_patterns=list(MODEL_FILES), local_dir=str(directory))
    # Save the proxy tokenizer locally too; normal chat must work with network disabled.
    import tiktoken
    os.environ['TIKTOKEN_CACHE_DIR'] = str(directory / 'tokenizer-cache')
    tiktoken.get_encoding('cl100k_base')
    encoder = LocalEncoder(directory)
    vectors = encoder.encode(['Белый бас с чёрной накладкой.'])
    return {'repo': MODEL_REPO, 'dimension': len(vectors[0]), 'directory': str(directory),
            'model_id': encoder.model_id, 'network_required_in_chat': False}


class LocalEncoder:
    def __init__(self, directory=MODEL_DIR):
        directory = Path(directory)
        if not all((directory / f).is_file() for f in MODEL_FILES):
            raise RuntimeError('local encoder missing; run memory-setup once')
        import onnxruntime as ort
        ort.disable_telemetry_events()
        from tokenizers import Tokenizer
        config = json.loads((directory / 'config.json').read_text())
        self.dimension = config['hidden_size']
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / MODEL_FILES[0]),
                                           sess_options=options, providers=['CPUExecutionProvider'])
        digest = hashlib.sha256()
        for filename in MODEL_FILES:
            with (directory / filename).open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
        self.model_id = MODEL_REPO + ':' + digest.hexdigest()[:20]

    def encode(self, texts, query=False):
        import numpy as np
        if not texts:
            return []
        prefix = 'query: ' if query else 'passage: '
        result = []
        for offset in range(0, len(texts), 16):
            batch = self.tokenizer.encode_batch([prefix + t for t in texts[offset:offset + 16]])
            inputs = {'input_ids': np.asarray([b.ids for b in batch], dtype=np.int64),
                      'attention_mask': np.asarray([b.attention_mask for b in batch], dtype=np.int64)}
            if any(i.name == 'token_type_ids' for i in self.session.get_inputs()):
                inputs['token_type_ids'] = np.zeros_like(inputs['input_ids'])
            outputs = self.session.run(None, inputs)
            hidden = outputs[0]
            if hidden.ndim == 3:
                mask = inputs['attention_mask'][..., None].astype(np.float32)
                pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
            else:
                pooled = hidden
            pooled = pooled / np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
            result.extend(pooled.astype('float32').tolist())
        return result


_ENCODER = None


def get_encoder():
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = LocalEncoder()
    return _ENCODER
