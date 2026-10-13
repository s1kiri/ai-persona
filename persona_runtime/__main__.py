"""Run with python3 -m persona_runtime. All live calls are explicit and bounded."""
import argparse
from datetime import datetime
import json
import sqlite3
from pathlib import Path
import sys
import time
from contextlib import nullcontext

from .codex import CodexBackend
from .engine import Engine, ROOT
from .store import Store, worker_lock


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def deliver(store, person=None, conversation=None):
    for output in store.pending_output(person, conversation):
        print('\nГарик [%s]: %s\n' % (output['id'], output['text']), flush=True)
        # Confirmation is distinct from generation; a crash before this may replay output.
        store.acknowledge(output['id'])


def parser():
    p = argparse.ArgumentParser(description='Локальный Гарик: память, события и Codex по подписке.')
    p.add_argument('--data-dir', type=Path, default=ROOT / '.runtime/garik')
    p.add_argument('--codex', help='путь к Codex CLI; иначе поиск в PATH и приложении на Mac')
    p.add_argument('--model', help='модель Codex; по умолчанию gpt-6.1-sol, рассуждение medium')
    p.add_argument('--timeout', type=int, default=180, help='предел секунд на один вызов')
    p.add_argument('--no-web', action='store_true', help='не разрешать персонажу настоящий поиск Codex')
    p.add_argument('--max-web-calls', type=int, default=2, help='предел native web tool calls за поисковый ход (1..3)')
    p.add_argument('--memory-budget', type=int, default=4000, help='бюджет контекста памяти в оценочных токенах')
    p.add_argument('--retrieval', choices=('recent','bm25','dense','hybrid'), default='hybrid')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='проверить CLI, вход и сеть без вызова модели; нужен доступ к служебной папке Codex')
    proxy = commands.add_parser('proxy', help='SOCKS5: локальная настройка, проверка и отключение')
    actions = proxy.add_subparsers(dest='proxy_action', required=True)
    for action in ('set', 'status', 'off'):
        actions.add_parser(action)
    health = actions.add_parser('health')
    health.add_argument('--codex', dest='probe_codex', action='store_true', help='явно разрешить один ограниченный запрос модели')
    commands.add_parser('init', help='создать локальную память, не вызывая модель')
    commands.add_parser('status', help='очередь, ошибки, следующее пробуждение; без модели')
    send = commands.add_parser('send', help='поставить сообщение в очередь; без модели')
    send.add_argument('text')
    send.add_argument('--event-id', help='стабильный ID события для защиты от повторов')
    chat = commands.add_parser('chat', help='личный разговор; речь по контексту, /quit для выхода')
    chat.add_argument('--max-calls', type=int, default=20, help='предел вызовов Codex за сеанс, включая дополнительный поиск')
    chat.add_argument('--max-wakes', type=int, default=3, help='предел самостоятельных пробуждений за сеанс')
    chat.add_argument('--max-minutes', type=int, default=60, help='время самостоятельных пробуждений; разговор доступен после него')
    autonomy = chat.add_mutually_exclusive_group()
    autonomy.add_argument('--autonomy', dest='autonomy', action='store_true', default=None)
    autonomy.add_argument('--no-autonomy', dest='autonomy', action='store_false')
    for sub in (send, chat):
        sub.add_argument('--person', default='operator', help='стабильный ID собеседника')
        sub.add_argument('--name', default='Кирилл', help='отображаемое имя')
        sub.add_argument('--conversation', default='local')
        sub.add_argument('--surface', choices=('private','formal','unknown'), help='контекст речи; личный чат оператора считается private')
        sub.add_argument('--register', choices=('auto', 'non-obscene', 'free'),
                         default='auto' if sub is chat else 'non-obscene')
    step = commands.add_parser('step', help='обработать очередь и показать ответы')
    step.add_argument('--count', type=int, default=1, help='максимум вызовов модели (1..20)')
    commands.add_parser('outbox', help='показать неподтверждённые ответы, не меняя статус')
    ack = commands.add_parser('ack', help='подтвердить локальную доставку ответа')
    ack.add_argument('output_id')
    retry = commands.add_parser('retry', help='разрешить повтор ошибочного события; без модели')
    retry.add_argument('event_id')
    commands.add_parser('memory', help='показать текущую память; без модели')
    commands.add_parser('memory-setup', help='один раз скачать локальный эмбеддер и токенизатор')
    commands.add_parser('journal', help='показать сохранённый личный дневник; без модели')
    recall = commands.add_parser('recall', help='проверить локальный поиск без вызова Codex')
    recall.add_argument('query')
    recall.add_argument('--person', default='operator')
    recall.add_argument('--conversation', default='local')
    backup = commands.add_parser('backup', help='сохранить согласованную копию SQLite; без модели')
    backup.add_argument('path', type=Path)
    watch = commands.add_parser('watch', help='локальный цикл с конечным лимитом вызовов; Ctrl+C для остановки')
    watch.add_argument('--max-turns', type=int, default=3, help='предел вызовов модели (1..20)')
    watch.add_argument('--max-minutes', type=int, default=30, help='предел времени работы (1..1440 минут)')
    watch.add_argument('--register', choices=('auto', 'non-obscene', 'free'), default='non-obscene')
    daemon=commands.add_parser('daemon',help='фоновая жизнь и локальная панель')
    daemon_commands=daemon.add_subparsers(dest='daemon_action',required=True)
    for name in ('start','run'):
        sub=daemon_commands.add_parser(name)
        sub.add_argument('--port',type=int,default=8765)
        from .daemon import Limits
        for key,value in vars(Limits()).items():
            if type(value) is bool:sub.add_argument('--no-'+key,dest=key,action='store_false',default=True)
            else:sub.add_argument('--'+key.replace('_','-'),dest=key,type=int,default=value)
    for name in ('status','stop','autostart-template'):daemon_commands.add_parser(name)
    commands.add_parser('panel',help='открыть панель; при необходимости запустить ограниченный daemon')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.max_web_calls <= 3:
        print('--max-web-calls must be 1..3', file=sys.stderr)
        return 2
    if not 1 <= args.timeout <= 600:
        print('--timeout must be 1..600', file=sys.stderr)
        return 2
    if not 1000 <= args.memory_budget <= 12000:
        print('--memory-budget must be 1000..12000', file=sys.stderr)
        return 2
    if args.command == 'chat' and (not 1 <= args.max_calls <= 100 or not 0 <= args.max_wakes <= 20 or not 1 <= args.max_minutes <= 1440):
        print('invalid chat limits', file=sys.stderr)
        return 2
    if args.command == 'step' and not 1 <= args.count <= 20:
        print('--count must be 1..20', file=sys.stderr)
        return 2
    if args.command == 'watch' and (not 1 <= args.max_turns <= 20 or not 1 <= args.max_minutes <= 1440):
        print('invalid watch limits', file=sys.stderr)
        return 2
    store = None
    owned_lock=None
    lock_entered=False
    try:
        if args.command in ('daemon','panel'):
            destination=args.data_dir.expanduser().resolve()
            if destination==ROOT or (ROOT in destination.parents and not any(p==destination or p in destination.parents for p in (ROOT/'.local',ROOT/'.runtime'))):
                raise ValueError('within repository, data must live under .runtime/ or .local/')
            from .daemon import Runtime,Limits,control,start,stop
            action='panel' if args.command=='panel' else args.daemon_action
            if action in ('start','run'):
                values={key:getattr(args,key) for key in vars(Limits())}
                limits=Limits(**values).validate()
                if not 1024<=args.port<=65535:raise ValueError('port must be 1024..65535')
                if action=='start':show(start(args.data_dir,limits,args.port,args.codex,args.model,args.timeout))
                else:
                    runtime=Runtime(args.data_dir,args.port,limits,backend_factory=lambda:CodexBackend(args.codex,args.model,args.timeout))
                    import signal
                    signal.signal(signal.SIGTERM,lambda *_:runtime.stopping.set())
                    signal.signal(signal.SIGINT,lambda *_:runtime.stopping.set())
                    runtime.run()
            elif action=='status':
                state=control(args.data_dir)
                show({'running':bool(state),**({k:state[k] for k in ('pid','started','mode','model','limits','calls_used','wakes_used','next_wake','usage')} if state else {}),
                      'url':json.loads((args.data_dir/'daemon.json').read_text())['url'] if state else None})
            elif action=='stop':show(stop(args.data_dir))
            elif action=='autostart-template':
                import plistlib
                destination=ROOT/'.local/garik.launchagent.plist'
                destination.parent.mkdir(parents=True,exist_ok=True)
                with destination.open('wb') as output:plistlib.dump({'Label':'local.persona.garik','ProgramArguments':[sys.executable,'-m','persona_runtime','--data-dir',str(args.data_dir.resolve()),'daemon','run'],
                    'WorkingDirectory':str(ROOT),'RunAtLoad':False,'KeepAlive':False},output)
                destination.chmod(0o600)
                show({'template':str(destination),'installed':False,'autostart_enabled':False})
            else:
                import subprocess
                result=start(args.data_dir,codex=args.codex,model=args.model,timeout=args.timeout)
                subprocess.run(['/usr/bin/open','-a','Microsoft Edge',result['url']],check=True)
                show(result)
            return 0
        if args.command == 'proxy':
            from . import proxy
            if args.proxy_action == 'set':
                show(proxy.interactive_set())
            elif args.proxy_action == 'status':
                show(proxy.status())
            elif args.proxy_action == 'off':
                show(proxy.disable())
            else:
                report = proxy.health(timeout=min(args.timeout, 10))
                if args.probe_codex and report['ok']:
                    backend = CodexBackend(args.codex, args.model, min(args.timeout, 60))
                    # Same restricted transport/schema as character turns; no memory writes.
                    from .protocol import SCHEMA
                    prompt = ('Connection probe only. Return a valid decision with reply=OK, state=probe, '
                              'goals=probe, wake_after_minutes=5, memories=[], relations=[], journal=[], memory_query=null. No tools. Schema: ' + json.dumps(SCHEMA))
                    report['model_called'] = True
                    try:
                        backend.decide(prompt)
                        report['proxy_transport'] = backend.last_proxy_report
                        report['codex'] = 'ok'
                    except RuntimeError as exc:
                        report.update(ok=False, codex='failed', error=str(exc))
                        report['proxy_transport'] = getattr(backend, 'last_proxy_report', {'enabled': True})
                show(report)
                return 0 if report['ok'] else 1
            return 0
        if args.command == 'memory-setup':
            from .embedding import setup
            show(setup())
            return 0
        backend = CodexBackend(args.codex, args.model, args.timeout)
        if args.command == 'doctor':
            show(backend.doctor())
            return 0
        destination = args.data_dir.expanduser().resolve()
        if destination == ROOT or (ROOT in destination.parents and
                not any(allowed == destination or allowed in destination.parents
                        for allowed in ((ROOT / '.runtime').resolve(), (ROOT / '.local').resolve()))):
            raise ValueError('within the repository, data must live under .runtime/ or .local/')
        destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        owned_lock=worker_lock(destination)
        owned_lock.__enter__();lock_entered=True
        store = Store(args.data_dir,locked=True)
        from .memory import Budget
        store.memory.budget = Budget(args.memory_budget)
        store.memory.mode = args.retrieval
        engine = Engine(store, backend, web_enabled=not args.no_web, max_web_calls=args.max_web_calls)
        if args.command == 'init':
            show({'data': str(store.directory), 'ready': True, 'model_called': False})
        elif args.command == 'status':
            status = store.status()
            if status['next_wake']:
                status['next_wake_local'] = datetime.fromtimestamp(float(status['next_wake'])).isoformat(timespec='seconds')
            show(status)
        elif args.command == 'send':
            show({'event_id': store.enqueue(args.text, args.person, args.name, args.conversation,
                                             args.register, args.event_id, surface=args.surface or 'unknown'), 'model_called': False})
        elif args.command == 'outbox':
            show(store.pending_output(include_future=True))
        elif args.command == 'ack':
            store.acknowledge(args.output_id)
            show({'acknowledged': args.output_id})
        elif args.command == 'retry':
            store.retry(args.event_id)
            show({'queued_for_retry': args.event_id})
        elif args.command == 'memory':
            show({'state': store.get('state'), 'goals': store.get('goals'),
                  'notes': [dict(r) for r in store.db.execute('SELECT * FROM recollections WHERE valid_to IS NULL ORDER BY created,key')],
                  'relations': [dict(r) for r in store.db.execute('SELECT * FROM relations WHERE valid_to IS NULL ORDER BY created,key')]})
        elif args.command == 'journal':
            show([dict(r) for r in store.db.execute('SELECT * FROM journal ORDER BY created,id')])
        elif args.command == 'recall':
            context, report = store.memory.context({'person':args.person,'conversation':args.conversation,'kind':'message','text':args.query}, query=args.query)
            show({'context':context,'report':report,'model_called':False})
        elif args.command == 'backup':
            destination = args.path.expanduser().resolve()
            # A casual backup command must not put private data into tracked source paths.
            allowed = (ROOT / '.runtime').resolve()
            if allowed not in destination.parents:
                raise ValueError('save backups under .runtime/; move them deliberately when migrating')
            store.backup(destination)
            show({'backup': str(destination)})
        else:
            with nullcontext():
                store.recover()
                if args.command == 'chat':
                    deliver(store, args.person, args.conversation)
                else:
                    deliver(store)
                if args.command == 'step':
                    completed = model_calls = 0
                    while model_calls < args.count:
                        if engine.step(max_calls=min(3,args.count-model_calls)) is None:
                            break
                        model_calls += engine.last_calls
                        completed += 1
                        deliver(store)
                    show({'processed': completed, 'model_calls':model_calls})
                elif args.command == 'chat':
                    from .session import Session
                    autonomy = args.autonomy if args.autonomy is not None else (args.person == 'operator' and args.conversation == 'local')
                    session = Session(store, engine, args.person, args.name, args.conversation,
                                      args.register, autonomy, args.max_calls, args.max_wakes, args.max_minutes,
                                      deliver=lambda: deliver(store,args.person,args.conversation), surface=args.surface)
                    session.run()
                elif args.command == 'watch':
                    end = time.monotonic() + args.max_minutes * 60
                    completed = 0
                    print('Локальный цикл: не более %d вызовов / %d минут. Ctrl+C — остановка.' % (args.max_turns, args.max_minutes), flush=True)
                    while (completed < args.max_turns or store.pending_output(include_future=True)) and time.monotonic() < end:
                        # Timed delivery is independent of another model call.
                        deliver(store)
                        if completed >= args.max_turns:
                            time.sleep(.05)
                            continue
                        engine.wake_if_due(args.register)
                        if engine.step(max_calls=min(3,args.max_turns-completed)) is not None:
                            completed += engine.last_calls
                            deliver(store)
                        else:
                            time.sleep(1)
                    show({'model_calls': completed, 'stopped': 'local limit'})
        return 0
    except KeyboardInterrupt:
        message = ('Операция прокси отменена.' if args.command == 'proxy' else
                   'Остановлено. Незавершённое событие сохранено; следующий worker восстановит очередь.')
        print('\n' + message, file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ImportError) as exc:
        print('Ошибка: %s' % exc, file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
        if lock_entered:owned_lock.__exit__(None,None,None)


if __name__ == '__main__':
    sys.exit(main())
