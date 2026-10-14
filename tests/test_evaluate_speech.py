import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.codex import CodexError
from tools.evaluate_speech import TextBackend, make_blind_review


class Client:
    def __init__(self, tool=False, empty=False):
        self.calls=[];self.events=[];self.turn=0;self.tool=tool;self.empty=empty

    def rpc(self,method,params,**kwargs):
        self.calls.append((method,params))
        if method=='thread/start':return {'thread':{'id':'test-thread'}}
        if method=='turn/start':
            self.turn+=1
            base={'threadId':'test-thread','turnId':str(self.turn)}
            usage={'method':'thread/tokenUsage/updated','params':{**base,
                'tokenUsage':{'total':{'inputTokens':100*self.turn,'outputTokens':3*self.turn}}}}
            self.events=[usage,usage]
            if not self.empty:
                self.events.append({'method':'item/completed','params':{**base,'item':{
                    'id':'answer','type':'commandExecution' if self.tool else 'agentMessage','text':'Обычный ответ'}}})
            self.events.append({'method':'turn/completed','params':{**base,'turn':{'id':str(self.turn),'status':'completed'}}})
            return {'turn':{'id':str(self.turn)}}
        return {}

    def receive(self,deadline):return self.events.pop(0)


class SpeechExperimentTests(unittest.TestCase):
    def backend(self,directory,client):
        backend=TextBackend(Path(directory)/'private')
        backend.client=client;backend.workspace=Path(directory);backend.overrides={}
        backend._start=lambda:0
        return backend

    def test_plain_output_retains_thread_without_schema_or_duplicate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Client();backend=self.backend(directory,client)
            reply,usage=backend.respond('unchanged persona','first')
            self.assertEqual(reply,'Обычный ответ')
            self.assertEqual(usage,{'input_tokens':100,'output_tokens':3})
            _,second=backend.respond('unchanged persona','second')
            self.assertEqual(second,usage)
            starts=[params for method,params in client.calls if method=='thread/start']
            self.assertEqual(len(starts),1)
            self.assertEqual(starts[0]['baseInstructions'],'unchanged persona')
            turns=[params for method,params in client.calls if method=='turn/start']
            self.assertNotIn('outputSchema',turns[0])
            self.assertEqual(turns[1]['input'],[{'type':'text','text':'second'}])
            self.assertEqual(turns[1]['sandboxPolicy'],{'type':'readOnly','networkAccess':False})

    def test_unexpected_tool_aborts_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Client(tool=True);backend=self.backend(directory,client)
            with self.assertRaises(CodexError):backend.respond('persona','input')
            self.assertEqual([method for method,_ in client.calls],['thread/start','turn/start','turn/interrupt'])

    def test_no_answer_aborts_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Client(empty=True);backend=self.backend(directory,client)
            with self.assertRaises(CodexError):backend.respond('persona','input')
            self.assertEqual(client.turn,1)

    def test_review_only_compares_completed_matching_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            rows=[{'scene':'scene','turn':1,'arm':arm,'status':'completed','input':'x','reply':arm} for arm in ('current','minimal')]
            rows.append({'scene':'scene','turn':2,'arm':'minimal','status':'completed','input':'unmatched','reply':'extra'})
            make_blind_review(Path(directory),{'rows':rows})
            review=(Path(directory)/'blind-review.md').read_text()
            self.assertNotIn('unmatched',review)
            self.assertNotIn('extra',review)
            self.assertEqual(len(json.loads((Path(directory)/'blind-key.json').read_text())['scene']),2)


if __name__=='__main__':unittest.main()
