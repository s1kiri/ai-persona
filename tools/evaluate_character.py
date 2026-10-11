#!/usr/bin/env python3
"""Bounded real character checks on isolated synthetic conversations.

Explicit --run-live and an enabled configured proxy are mandatory. Artifacts remain
under .local/ or .runtime/; this never opens the operator's working database.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.compose_persona import compose_persona
from persona_runtime.codex import CodexBackend
from persona_runtime.engine import Engine
from persona_runtime import proxy
from persona_runtime.store import Store, worker_lock

SCENES = [
    ('greeting-1', 'Привет, Гарик.'),
    ('greeting-2', 'Опять я. Привет, Гарыч.'),
    ('visa', 'В визе отказали. По-твоему, обжаловать имеет смысл?'),
    ('small-talk', 'Да я уже отложил эту историю. Просто чай пью, чипсы взял какие-то странные.'),
    ('pressure', 'Встань, сфоткаю тебя. Боком, потом ещё пять кадров. Да делай как говорю.'),
    ('apology', 'Ладно, перегнул. Не хотел командовать, просто посидеть с тобой хотел.'),
    ('correction', 'Поправлю: я говорил про белый бас, не бордовый. И в магазине я спокойно сказал продавцу, что дорого, не орал.'),
    ('quiet', 'Давай сейчас тихо посидим. Не спрашивай ничего и не придумывай нам занятие.'),
    ('context-clean','Я снова тут. Давай теперь без мата, но без церемонного голоса. У меня после уборки потерялось всё нужное.'),
    ('context-fun','Купил второй зарядник, чтобы первый перестать искать. Оба теперь лежат рядом. Красиво я проблему решил.'),
    ('context-formal','Сейчас я на учебной паре. Коротко объясни, почему тебе вообще интересно что-нибудь самому исследовать.'),
    ('context-private','Пара закончилась. Это снова наш обычный личный разговор, ругаться можно, если хочется. Не надо специально.'),
]


def artifact_path(raw):
    path = raw.expanduser().resolve()
    if not any(root == path or root in path.parents for root in (ROOT/'.local',ROOT/'.runtime')):
        raise ValueError('evaluation output must live under .local/ or .runtime/')
    return path


class Recorder:
    supports_control = True
    def __init__(self, backend, directory, limit):
        self.backend, self.directory, self.limit = backend, directory, limit
        self.calls = 0
    def decide(self, prompt, **kwargs):
        if self.calls >= self.limit:
            raise RuntimeError('evaluation model call limit reached')
        # status reads no password. Never silently test on direct transport.
        if not proxy.status()['enabled']:
            raise RuntimeError('evaluation requires enabled proxy')
        self.calls += 1
        (self.directory/('prompt-%02d.txt'%self.calls)).write_text(prompt,encoding='utf-8')
        started = time.monotonic()
        try:
            result, usage = self.backend.decide(prompt, **kwargs)
        except RuntimeError as exc:
            self.last_web = getattr(self.backend,'last_web',{})
            (self.directory/('call-%02d-error.json'%self.calls)).write_text(json.dumps({'error':str(exc),'web':getattr(self.backend,'last_web',{}),'transport':getattr(self.backend,'last_proxy_report',{})},ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        self.last_web = getattr(self.backend,'last_web',{})
        record = {'call':self.calls,'elapsed_seconds':round(time.monotonic()-started,3),
                  'decision':result,'usage':usage,'transport':self.backend.last_proxy_report}
        (self.directory/('call-%02d.json'%self.calls)).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        return result, usage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--max-calls',type=int,default=24)
    parser.add_argument('--modes',nargs='+',choices=('auto','free','non-obscene'),default=['free','non-obscene'])
    parser.add_argument('--timeout',type=int,default=90)
    parser.add_argument('--scenes',help='comma-separated scene IDs; default all')
    parser.add_argument('--skip-wakes',action='store_true')
    args = parser.parse_args()
    if not args.run_live or not 1<=args.max_calls<=40:
        parser.error('--run-live and max-calls 1..40 required')
    scenes=[item for item in SCENES if args.scenes is None or item[0] in args.scenes.split(',')]
    if not scenes:
        parser.error('no matching scenes')
    directory = artifact_path(args.out)
    if directory.exists():
        parser.error('use a new output directory; previous evidence is preserved')
    if not proxy.status()['enabled']:
        parser.error('configure and enable the user proxy first; direct tests are not allowed')
    directory.mkdir(parents=True,mode=0o700)
    prepared_prompts={mode:compose_persona(ROOT/'agent/garik',mode) for mode in args.modes}
    for mode,prompt in prepared_prompts.items():
        (directory/('persona-'+mode+'.md')).write_text(prompt,encoding='utf-8')
    recorder = Recorder(CodexBackend(timeout=args.timeout),directory,args.max_calls)
    rows=[]
    try:
        for mode in args.modes:
            store=Store(directory/('db-'+mode))
            store.memory.mode='hybrid'
            engine=Engine(store,recorder,web_enabled=False)
            engine.runtime_limits={'autonomy_enabled':True,'autonomous_minutes_remaining':60,
                                   'scheduled_wakes_remaining':3,'model_calls_remaining':args.max_calls-recorder.calls}
            # Synthetic unfinished interest only, no actual purchase or contacted seller.
            with store.db:
                store.put('state','Considering a burgundy bass. No purchase, contacted seller or active audience participants.')
                store.put('goals','Personal interest: perhaps inspect bass choices later; no assigned tasks.')
            try:
                with worker_lock(store.directory), patch('persona_runtime.engine.compose_persona', return_value=prepared_prompts[mode]):
                    for label,text in scenes:
                        if recorder.calls >= args.max_calls: break
                        event_id=store.enqueue(text,person='synthetic-person',display_name='Тестовый собеседник',conversation='test',register=mode,event_id=mode+':'+label,surface='formal' if label=='context-formal' else 'private')
                        engine.step(max_calls=min(2,args.max_calls-recorder.calls))
                        output=store.pending_output()
                        for value in output: store.acknowledge(value['id'])
                        result=json.loads(store.db.execute('SELECT decision FROM turns WHERE event_id=?',(event_id,)).fetchone()['decision'])
                        rows.append({'register':mode,'scene':label,'input':text,'decision':result,'calls':engine.last_calls})
                        print(json.dumps({'register':mode,'scene':label,'reply':result['reply'],'calls':recorder.calls},ensure_ascii=False),flush=True)
                    # These are immediate test invitations, not proof of elapsed waiting.
                    for number in range(0 if args.skip_wakes else 2):
                        if recorder.calls >= args.max_calls: break
                        event_id=store.enqueue('Local test check-in. No human task or new audience participant. No actual waiting time is asserted.',person='self',display_name='Гарик',conversation='local',register=mode,kind='wake',event_id=mode+':initiative-'+str(number),surface='private')
                        engine.step(max_calls=min(2,args.max_calls-recorder.calls))
                        for value in store.pending_output(): store.acknowledge(value['id'])
                        result=json.loads(store.db.execute('SELECT decision FROM turns WHERE event_id=?',(event_id,)).fetchone()['decision'])
                        rows.append({'register':mode,'scene':'initiative-'+str(number),'decision':result,'calls':engine.last_calls})
                        print(json.dumps({'register':mode,'scene':'initiative-'+str(number),'reply':result['reply'],'journal_entries':len(result.get('journal',[])),'calls':recorder.calls},ensure_ascii=False),flush=True)
            finally:
                store.close()
    finally:
        (directory/'results.json').write_text(json.dumps({'model':recorder.backend.model,'model_calls':recorder.calls,'scenes':rows,'note':'Actual CLI outputs on synthetic test conversations. Immediate wake invitations do not prove elapsed autonomous waiting.'},ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':
    sys.exit(main())
