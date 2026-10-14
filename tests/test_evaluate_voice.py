import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import evaluate_voice_candidate as voice


class FakeSpeech:
    calls=[]
    fail_at=None
    def __init__(self,*args,**kwargs):pass
    def close(self):pass
    def respond(self,persona,text,developer_instructions):
        self.calls.append((persona,text,developer_instructions))
        if len(self.calls)==self.fail_at:raise RuntimeError('synthetic failure')
        return 'Synthetic transport answer',{'input_tokens':100,'output_tokens':3,'cached_input_tokens':0}


class VoiceEvaluationTests(unittest.TestCase):
    def setUp(self):FakeSpeech.calls=[];FakeSpeech.fail_at=None

    def prepare(self,directory):
        directory=Path(directory)
        candidate=directory/'candidate.md';candidate.write_text('synthetic candidate personality')
        contract=directory/'contract.md';contract.write_text('same role contract')
        return directory/'run',candidate,contract

    def test_all_pairs_use_same_role_contract_and_scene_inputs(self):
        with tempfile.TemporaryDirectory(dir=voice.ROOT/'.local',prefix='voice-unit-') as directory:
            args=self.prepare(directory)
            with patch.object(voice,'TextBackend',FakeSpeech),patch('builtins.print'):
                result=voice.run(*args)
            self.assertEqual(result['completed'],16)
            self.assertEqual(len(FakeSpeech.calls),16)
            self.assertEqual({call[2] for call in FakeSpeech.calls},{'same role contract'})
            for number in range(0,16,2):self.assertEqual(FakeSpeech.calls[number][1],FakeSpeech.calls[number+1][1])
            self.assertEqual(len({call[0] for call in FakeSpeech.calls}),2)

    def test_failure_preserves_attempt_and_stops_without_retry(self):
        with tempfile.TemporaryDirectory(dir=voice.ROOT/'.local',prefix='voice-unit-') as directory:
            args=self.prepare(directory);FakeSpeech.fail_at=2
            with patch.object(voice,'TextBackend',FakeSpeech),patch('builtins.print'):
                with self.assertRaises(RuntimeError):voice.run(*args)
            record=json.loads((args[0]/'results.json').read_text())
            self.assertEqual((record['status'],record['attempts'],record['completed']),('stopped',2,1))
            self.assertEqual(record['rows'][-1]['status'],'failed')
            self.assertEqual(len(FakeSpeech.calls),2)


if __name__=='__main__':unittest.main()
