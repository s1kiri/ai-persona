"""Run with python3 -m persona_runtime. All live calls are explicit and bounded."""
import argparse
from datetime import datetime
import json
import sqlite3
from pathlib import Path
import sys
import time

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
    p.add_argument('--model', help='модель Codex; иначе строка model из конфигурации или стандарт CLI')
    p.add_argument('--timeout', type=int, default=180, help='предел секунд на один вызов')
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
    chat = commands.add_parser('chat', help='личный разговор; свободный регистр, /quit для выхода')
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
        sub.add_argument('--register', choices=('non-obscene', 'free'),
                         default='free' if sub is chat else 'non-obscene')
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
    watch.add_argument('--register', choices=('non-obscene', 'free'), default='non-obscene')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
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
    try:
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
        store = Store(args.data_dir)
        from .memory import Budget
        store.memory.budget = Budget(args.memory_budget)
        store.memory.mode = args.retrieval
        engine = Engine(store, backend)
        if args.command == 'init':
            show({'data': str(store.directory), 'ready': True, 'model_called': False})
        elif args.command == 'status':
            status = store.status()
            if status['next_wake']:
                status['next_wake_local'] = datetime.fromtimestamp(float(status['next_wake'])).isoformat(timespec='seconds')
            show(status)
        elif args.command == 'send':
            show({'event_id': store.enqueue(args.text, args.person, args.name, args.conversation,
                                             args.register, args.event_id), 'model_called': False})
        elif args.command == 'outbox':
            show(store.pending_output())
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
            with worker_lock(store.directory):
                store.recover()
                if args.command == 'chat':
                    deliver(store, args.person, args.conversation)
                else:
                    deliver(store)
                if args.command == 'step':
                    completed = 0
                    for _ in range(args.count):
                        if engine.step() is None:
                            break
                        completed += 1
                        deliver(store)
                    show({'processed': completed})
                elif args.command == 'chat':
                    from .session import Session
                    autonomy = args.autonomy if args.autonomy is not None else (args.person == 'operator' and args.conversation == 'local')
                    session = Session(store, engine, args.person, args.name, args.conversation,
                                      args.register, autonomy, args.max_calls, args.max_wakes, args.max_minutes,
                                      deliver=lambda: deliver(store,args.person,args.conversation))
                    while store.status()['events'].get('pending',0) and not session.stopped:
                        session.process()
                    session.run()
                elif args.command == 'watch':
                    end = time.monotonic() + args.max_minutes * 60
                    completed = 0
                    print('Локальный цикл: не более %d вызовов / %d минут. Ctrl+C — остановка.' % (args.max_turns, args.max_minutes), flush=True)
                    while completed < args.max_turns and time.monotonic() < end:
                        engine.wake_if_due(args.register)
                        if engine.step(max_calls=min(2,args.max_turns-completed)) is not None:
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


if __name__ == '__main__':
    sys.exit(main())
