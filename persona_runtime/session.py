"""Interactive input and scheduled local life share one worker, with explicit call limits."""
import select
import sys
import time


class Session:
    def __init__(self, store, engine, person='operator', name='Кирилл', conversation='local',
                 register='free', autonomy=True, max_calls=20, max_wakes=3, max_minutes=60,
                 deliver=None, output=print, clock=time.monotonic):
        self.store, self.engine = store, engine
        self.person, self.name, self.conversation, self.register = person,name,conversation,register
        self.autonomy, self.paused = autonomy, False
        self.max_calls, self.max_wakes = max_calls,max_wakes
        self.calls, self.wakes = 0,0
        self.clock, self.deadline = clock, clock()+max_minutes*60
        self.deliver, self.output = deliver or (lambda: None),output
        self.stopped = False

    def autonomy_status(self):
        if self.paused:
            return 'на паузе'
        if not self.autonomy:
            return 'выключена'
        if self.wakes >= self.max_wakes:
            return 'лимит пробуждений достигнут'
        if self.clock() >= self.deadline:
            return 'время самостоятельной работы истекло'
        return 'включена'

    def process(self):
        remaining = self.max_calls-self.calls
        if remaining <= 0:
            self.stopped = True
            return False
        self.engine.runtime_limits = {'autonomy_enabled':self.autonomy_status() == 'включена',
                                      'autonomous_minutes_remaining':max(0,int((self.deadline-self.clock())/60)),
                                      'scheduled_wakes_remaining':max(0,self.max_wakes-self.wakes),
                                      'model_calls_remaining':remaining}
        try:
            done = self.engine.step(max_calls=min(2,remaining))
        finally:
            self.calls += self.engine.last_calls
        self.deliver()
        if self.calls >= self.max_calls:
            self.output('Достигнут лимит вызовов модели. Память сохранена; сеанс остановлен.')
            self.stopped = True
        return done is not None

    def tick(self, now=None):
        if self.stopped or not self.autonomy or self.paused or self.wakes>=self.max_wakes or self.clock()>=self.deadline:
            return False
        event = self.engine.wake_if_due(self.register,now=now)
        if event is None:
            return False
        self.wakes += 1
        return self.process()

    def line(self, text):
        if self.stopped:
            return
        text=text.strip()
        if text in ('/quit','/exit'):
            self.stopped=True
        elif text=='/pause':
            self.paused=True
            self.output('Самостоятельные пробуждения на паузе. Разговаривать можно.')
        elif text=='/resume':
            self.paused=False
            self.autonomy=True
            self.output('Самостоятельные пробуждения включены в пределах лимитов сеанса.')
        elif text=='/status':
            self.output('Вызовы: %d/%d; пробуждения: %d/%d; самостоятельность: %s.' %
                        (self.calls,self.max_calls,self.wakes,self.max_wakes,
                         self.autonomy_status()))
        elif text=='/journal':
            entries=self.store.db.execute('SELECT id,title,kind FROM journal ORDER BY created DESC LIMIT 5').fetchall()
            self.output('Дневник: '+ ('; '.join(r['title']+' ['+r['kind']+']' for r in entries) or 'пока пусто'))
        elif text=='/wake':
            if self.calls >= self.max_calls:
                self.stopped=True
                return
            self.store.enqueue('The local operator invited you to check in on your own intentions. Choose an available action or wait.',
                               person='self',display_name='Гарик',conversation='local',register=self.register,kind='wake')
            self.process()
        elif text=='/help':
            self.output('/quit — выход; /pause и /resume — самостоятельность; /wake — пригласить заняться своим; /status — лимиты; /journal — записи.')
        elif text.startswith('/'):
            self.output('Неизвестная команда. /help — список команд.')
        elif text:
            self.store.enqueue(text,self.person,self.name,self.conversation,self.register)
            self.process()

    def run(self, stream=None):
        stream=stream or sys.stdin
        self.output('Гарик локально. /quit — выйти, /help — команды. Режим: %s.' % self.register)
        self.output('Самостоятельность: %s; максимум %d вызовов, %d пробуждений за %d минут.' %
                    ('включена' if self.autonomy else 'выключена',self.max_calls,self.max_wakes,
                     max(0,round((self.deadline-self.clock())/60))))
        # Buffered pipe reads can consume several lines at once; select is only used for a TTY.
        interactive=stream.isatty()
        print('Ты: ',end='',flush=True)
        while not self.stopped:
            if interactive:
                readable,_,_=select.select([stream],[],[],1.0)
                if not readable:
                    if self.tick():
                        print('Ты: ',end='',flush=True)
                    continue
            line=stream.readline()
            if not line:
                break
            # Readable audience input wins over a due wake.
            self.line(line)
            if not self.stopped:
                print('Ты: ',end='',flush=True)
