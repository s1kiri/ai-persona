"""One SQLite owner coordinates input, cancellable generation and timed delivery."""
from concurrent.futures import ThreadPoolExecutor
import queue
import sys
import threading
import time

from .codex import CodexCancelled
from .store import StaleTurn


class Session:
    def __init__(self,store,engine,person='operator',name='Кирилл',conversation='local',
                 register='auto',autonomy=True,max_calls=20,max_wakes=3,max_minutes=60,
                 deliver=None,output=print,clock=time.monotonic,surface=None,continuous=False):
        self.store,self.engine=store,engine
        self.person,self.name,self.conversation,self.register=person,name,conversation,register
        self.surface=surface or ('private' if person=='operator' and conversation=='local' else 'unknown')
        self.autonomy,self.paused=autonomy,store.get('initiative_paused')=='1'
        self.max_calls,self.max_wakes=max_calls,max_wakes
        self.continuous=continuous
        self.dispatch_blocked=False
        self.calls,self.wakes=0,0
        self.clock,self.deadline=clock,clock()+max_minutes*60 if max_minutes else float('inf')
        self.deliver,self.output=deliver or (lambda:None),output
        self.stopped,self.exit_requested=False,False
        self.async_mode=False
        self.executor,self.future,self.turn=None,None,None
        self.last_input=-float('inf')
        self.on_dispatch=None
        self.dispatch_guard=None
        self.research=None
        self.on_research_dispatch=None

    def autonomy_status(self):
        if self.paused:return 'на паузе'
        if not self.autonomy:return 'выключена'
        if self.max_wakes and self.wakes>=self.max_wakes:return 'лимит пробуждений достигнут'
        if not self.max_wakes and not self.continuous:return 'лимит пробуждений достигнут'
        if self.clock()>=self.deadline:return 'время самостоятельной работы истекло'
        return 'включена'

    def limits(self):
        self.engine.runtime_limits={'autonomy_enabled':self.autonomy_status()=='включена',
            'autonomous_minutes_remaining':None if self.deadline==float('inf') else max(0,int((self.deadline-self.clock())/60)),
            'scheduled_wakes_remaining':None if self.continuous and not self.max_wakes else max(0,self.max_wakes-self.wakes),
            'model_calls_remaining':None if self.continuous and not self.max_calls else max(0,self.max_calls-self.calls),
            'continuous':self.continuous}

    def remaining_calls(self):
        return 3 if self.continuous and not self.max_calls else self.max_calls-self.calls

    def process(self):
        if self.async_mode:
            return self.start_pending()
        remaining=self.remaining_calls()
        if remaining<=0:
            self.stopped=True
            return False
        self.limits()
        try:
            done=self.engine.step(max_calls=min(3,remaining))
        finally:
            self.calls+=self.engine.last_calls
        self.deliver()
        self.check_limit()
        return done is not None

    def check_limit(self):
        if self.max_calls and self.calls>=self.max_calls and not self.stopped:
            self.output('Достигнут лимит вызовов. Новые ходы остановлены; готовые сообщения ещё могут прийти.')
            self.stopped=True

    def start_pending(self):
        if self.future is not None or self.stopped or self.exit_requested:
            return False
        remaining=self.remaining_calls()
        if remaining<=0:
            self.check_limit()
            return False
        self.limits()
        self.turn=self.engine.begin(max_calls=min(3,remaining))
        if self.turn is None:return False
        self.submit()
        return True

    def submit(self):
        if self.remaining_calls()<=0:
            self.engine.cancel(self.turn,'session model call limit reached')
            self.turn=None
            self.check_limit()
            return False
        if self.dispatch_guard and not self.dispatch_guard():
            self.dispatch_blocked=True
            self.stopped=True
            self.engine.cancel(self.turn,'known usage or daily attempt limit reached')
            self.turn=None
            self.output('Достигнут временный предел расхода. Ожидаем освобождения бюджета.')
            return False
        self.engine.dispatch(self.turn)
        self.calls+=1
        if self.on_dispatch:self.on_dispatch(self.turn)
        self.future=self.executor.submit(self.engine.generate,self.turn)
        return True

    def poll(self):
        if self.research:
            self.research.poll()
            self.research.start(self)
        if self.future is None:
            return False
        if not self.future.done():
            if self.store.is_stale(self.turn.event) and not self.turn.cancel.is_set():
                self.engine.cancel(self.turn)
            return False
        turn,future=self.turn,self.future
        self.future=None
        try:
            if self.engine.accept(turn,future.result()):
                with self.store.db:self.store.put('consecutive_failures','0')
                self.turn=None
                self.deliver()
                self.check_limit()
            else:
                self.submit()
                return True
        except Exception as exc:
            self.engine.failed(turn,exc)
            self.turn=None
            if not isinstance(exc,(CodexCancelled,StaleTurn)):
                failures=int(self.store.get('consecutive_failures','0'))+1
                with self.store.db:
                    self.store.put('consecutive_failures',failures)
                    self.store.put('next_wake',time.time()+min(3600,60*2**min(failures,6)))
                self.output('Ошибка хода: '+str(exc)+'. Событие сохранено для явного retry.')
            self.check_limit()
        self.start_pending()
        return True

    def interrupt(self,reason='new audience input',wakes_only=False):
        if self.turn and (not wakes_only or self.turn.event['kind']=='wake'):
            self.engine.cancel(self.turn,reason)

    def tick(self,now=None):
        if self.stopped or self.autonomy_status()!='включена':
            return False
        if self.async_mode and (self.future is not None or self.clock()-self.last_input<30 or
                self.store.pending_output(self.person,self.conversation,include_future=True)):
            return False
        event=self.engine.wake_if_due(self.register,now=now)
        if event is None:return False
        self.wakes+=1
        return self.process()

    def line(self,text):
        text=text.strip()
        if text in ('/quit','/exit'):
            self.exit_requested=self.stopped=True
            self.interrupt('operator stopped the session')
            self.store.supersede_pending(self.person,self.conversation)
            return
        if text in ('/pause','/stop'):
            self.paused=True
            with self.store.db:
                self.store.put('initiative_paused','1')
                self.store.db.execute("UPDATE events SET status='superseded',error='initiative paused' WHERE kind='wake' AND status='pending'")
                self.store.db.execute("UPDATE outbox SET status='cancelled' WHERE status='queued' AND event_id IN (SELECT id FROM events WHERE kind='wake')")
            self.interrupt('initiative paused',wakes_only=True)
            if self.research:self.research.cancel(wakes_only=True)
            self.output('Самостоятельные пробуждения на паузе. Разговаривать можно.')
        elif text=='/resume':
            self.paused=False;self.autonomy=True
            with self.store.db:self.store.put('initiative_paused','0')
            self.output('Самостоятельные пробуждения включены в пределах лимитов сеанса.')
        elif text=='/cancel':
            self.interrupt('operator cancelled generation')
            if self.research:self.research.cancel()
            self.store.supersede_pending(self.person,self.conversation)
            self.output('Текущий ответ и ещё не доставленные продолжения отменены.')
        elif text=='/status':
            self.output('Вызовы: %d/%d; пробуждения: %d/%d; самостоятельность: %s.'%
                (self.calls,self.max_calls,self.wakes,self.max_wakes,self.autonomy_status()))
        elif text=='/journal':
            rows=self.store.db.execute('SELECT title,kind FROM journal ORDER BY created DESC LIMIT 5').fetchall()
            self.output('Дневник: '+('; '.join(row['title']+' ['+row['kind']+']' for row in rows) or 'пока пусто'))
        elif text=='/wake':
            if self.stopped:return
            if self.paused:
                self.output('Инициатива на паузе. /resume — разрешить пробуждения.')
                return
            self.store.enqueue('The local operator invited a check-in on your own intentions. No new participant is implied.',
                person='self',display_name='Гарик',conversation='local',register=self.register,kind='wake',surface='private')
            self.process()
        elif text=='/help':
            self.output('/quit — выход; /pause (/stop) и /resume — самостоятельность; /cancel — отменить ответ; /wake — пригласить заняться своим; /status — лимиты; /journal — записи.')
        elif text.startswith('/'):
            self.output('Неизвестная команда. /help — список команд.')
        elif text:
            self.message(text)

    def message(self,text,event_id=None):
        if event_id and self.store.db.execute('SELECT 1 FROM events WHERE id=?',(event_id,)).fetchone():
            return self.store.enqueue(text,self.person,self.name,self.conversation,self.register,event_id=event_id,surface=self.surface)
        self.last_input=self.clock()
        self.interrupt()
        self.store.supersede_pending(self.person,self.conversation)
        event_id=self.store.enqueue(text,self.person,self.name,self.conversation,self.register,event_id=event_id,surface=self.surface)
        if self.stopped:self.output('Лимит исчерпан; сообщение сохранено без вызова модели.')
        else:self.process()
        return event_id

    def close(self):
        if self.turn:
            self.interrupt('session stopped')
        if self.research:self.research.close()
        if self.future:
            try:
                self.future.result(timeout=5)
            except Exception:
                pass
            self.engine.failed(self.turn,CodexCancelled('session stopped'))
        if self.executor:
            self.executor.shutdown(wait=True,cancel_futures=True)
        if getattr(self.engine.backend,'supports_sessions',False):self.engine.backend.close()
        self.turn=self.future=None

    def run(self,stream=None):
        stream=stream or sys.stdin
        speech='по ситуации' if self.register=='auto' else self.register
        self.output('Гарик локально. /quit — выйти, /help — команды. Речь: %s.'%speech)
        self.output('Самостоятельность: %s; максимум %d вызовов, %d пробуждений за %d минут.'%
            ('включена' if self.autonomy and not self.paused else 'выключена',self.max_calls,self.max_wakes,
             max(0,round((self.deadline-self.clock())/60))))
        if not stream.isatty():
            # Pipe scripts deliberately preserve sequential messages; no autonomous wake.
            while not self.stopped:
                text=stream.readline()
                if not text:break
                self.line(text)
            return
        self.async_mode=True
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='garik-generation')
        inputs=queue.Queue()
        def read_input():
            try:
                for line in stream:inputs.put(line)
            finally:inputs.put(None)
        reader=threading.Thread(target=read_input,daemon=True)
        reader.start()
        print('Ты: ',end='',flush=True)
        try:
            while not self.exit_requested:
                # Audience input is consumed before completion or a due autonomous tick.
                received=False
                while True:
                    try:text=inputs.get_nowait()
                    except queue.Empty:break
                    if text is None:
                        self.exit_requested=True
                        break
                    self.line(text)
                    received=True
                    if self.exit_requested:break
                if self.exit_requested:break
                completed=self.poll()
                self.deliver()
                if not self.stopped:
                    self.start_pending()
                    self.tick()
                elif not self.store.pending_output(self.person,self.conversation,include_future=True):
                    break
                if received or completed:
                    print('Ты: ',end='',flush=True)
                time.sleep(.05)
        finally:
            self.close()
            self.async_mode=False
