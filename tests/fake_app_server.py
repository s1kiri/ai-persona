"""Synthetic stdio executable for CLI/session coordination tests; no inference."""
from pathlib import Path
import sys
from persona_runtime.app_server import DISABLED, PINNED_VERSION


def make_cli(directory, answer, started=None, delay=0):
    binary=Path(directory)/'fake-app-server'
    binary.write_text('#!'+str(Path(sys.executable).resolve())+'\n'+'''
import json,pathlib,sys,time
if '--version' in sys.argv:
 print('codex-cli '+%r);raise SystemExit()
def emit(value):print(json.dumps(value),flush=True)
turn=0
for line in sys.stdin:
 value=json.loads(line);method=value.get('method');params=value.get('params',{})
 if 'id' not in value:continue
 result={}
 if method=='config/read':result={'config':{'features':%r}}
 elif method=='thread/start':result={'thread':{'id':'synthetic-thread'}}
 elif method=='turn/start':
  turn+=1;result={'turn':{'id':'turn-'+str(turn)}}
 emit({'id':value['id'],'result':result})
 if method=='turn/start':
  if %r:pathlib.Path(%r).write_text('started')
  time.sleep(%r)
  common={'threadId':params['threadId'],'turnId':result['turn']['id']}
  emit({'method':'thread/tokenUsage/updated','params':{**common,'tokenUsage':{'total':{'inputTokens':turn,'outputTokens':turn}}}})
  emit({'method':'item/completed','params':{**common,'item':{'type':'agentMessage','id':'answer','text':json.dumps(%r)}}})
  emit({'method':'turn/completed','params':{**common,'turn':{**result['turn'],'status':'completed'}}})
'''%(PINNED_VERSION,{key:False for key in DISABLED},bool(started),str(started),delay,answer))
    binary.chmod(0o700)
    return str(binary)
