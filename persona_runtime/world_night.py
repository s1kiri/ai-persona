"""An explicitly started, finite overnight review; no retry of failed stages."""
import time
import uuid

from .world import WorldConflict


class Night:
    def __init__(self, lab):
        self.lab, self.store = lab, lab.store

    def start(self, request, now=None):
        now = time.time() if now is None else now
        if set(request) != {'id', 'hours', 'interval_minutes', 'virtual_minutes', 'max_calls', 'speech_every'}:
            raise ValueError('invalid night controls')
        uuid.UUID(request['id'])
        for key, lower, upper in [('hours', 1, 12), ('interval_minutes', 5, 90),
                                  ('virtual_minutes', 5, 360), ('max_calls', 3, 100), ('speech_every', 0, 12)]:
            if type(request[key]) is not int or not lower <= request[key] <= upper:
                raise ValueError('invalid night control')
        if self.lab.session is None:
            raise WorldConflict('night needs an explicitly enabled model session')
        with self.store.lock, self.store.db:
            old = self.store.get('night')
            if old and old['id'] == request['id']:
                return old
            if old and old['status'] == 'running':
                raise WorldConflict('night already running')
            state = self.store.monitor()
            ceiling = min(self.lab.session.max_calls, state['call_count'] + request['max_calls'])
            if ceiling - state['call_count'] < 3 or any(c['status'] == 'blocked' for c in state['cycles']):
                raise WorldConflict('review blocked or too few calls remain for one cycle')
            latest = state['cycles'][0] if state['cycles'] else None
            plan = {**request, 'status': 'running', 'started': now,
                    'deadline': now + request['hours'] * 3600, 'call_ceiling': ceiling,
                    'next_due': now + request['interval_minutes'] * 60 if latest and latest['status'] == 'complete' else now,
                    'last_cycle': latest['id'] if latest and latest['status'] == 'complete' else None,
                    'initial_cycle': latest['id'] if latest and latest['status'] == 'complete' else None,
                    'cycles_completed': 0, 'ended': None}
            self.store.put('night', plan)
            self.store.put('paused', False)
            return plan

    def stop(self, status='stopped', now=None):
        with self.store.lock, self.store.db:
            plan = self.store.get('night')
            if plan and plan['status'] == 'running':
                count = self.store.db.execute("SELECT COUNT(*) FROM world_cycles WHERE status='complete' AND created>=? AND id!=?",
                                              (plan['started'], plan.get('initial_cycle') or '')).fetchone()[0]
                plan.update(status=status, ended=time.time() if now is None else now, cycles_completed=count)
                self.store.put('night', plan)
            self.store.put('paused', True)

    def tick(self, now=None):
        now = time.time() if now is None else now
        state = self.store.monitor()
        plan = self.store.get('night')
        if not plan or plan['status'] != 'running':
            return
        if now >= plan['deadline']:
            return self.stop('finished', now)
        if any(c['status'] == 'blocked' for c in state['cycles']):
            return self.stop('error', now)
        if state['call_count'] >= plan['call_ceiling']:
            return self.stop('budget', now)
        if self.lab.busy or state['paused'] or self.lab.session is None:
            return
        current = next((c for c in state['cycles'] if c['status'] in ('ready', 'running')), None)
        if current:
            try:
                return self.lab.step(current['id'], current['phase'], current['revision'])
            except WorldConflict:
                return  # A concurrent click won; no accepted model stage is replayed.
        latest = state['cycles'][0] if state['cycles'] else None
        if latest and latest['status'] == 'complete' and latest['id'] != plan['last_cycle']:
            count = plan['cycles_completed'] + 1
            if plan['speech_every'] and count % plan['speech_every'] == 0 and 'speech' not in latest['value']['results']:
                self.store.request_speech(latest['id'], 'private', 'Как продвигается то, чем ты занимался?')
                return
            plan.update(cycles_completed=count, last_cycle=latest['id'], next_due=now+plan['interval_minutes']*60)
            with self.store.lock, self.store.db:
                # Do not revive a plan that the user stopped during this tick.
                if self.store.get('night')['status'] == 'running':
                    self.store.put('night', plan)
            return
        if now >= plan['next_due']:
            if plan['call_ceiling'] - state['call_count'] < 3:
                return self.stop('budget', now)
            self.store.prepare('garik', plan['virtual_minutes'])

    def loop(self, stopping):
        while not stopping.wait(1):
            try:
                self.tick()
            except Exception:
                self.stop('error')
                self.lab.failure = 'Ночной прогон остановлен. Автоматического повтора нет.'
