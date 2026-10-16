"""Author events run once through the serial world model; heroes retain agency."""
import copy
import json
import unittest

import test_shared_life as fixtures
from persona_runtime.world_interventions import enqueue,cancel,catalog,facts
from persona_runtime.shared_world import SharedTimeline
from persona_runtime.world_backend import WorldBackend
from persona_runtime.world import WorldConflict
from test_world_backend import Client


def event():
    return {'decision':'apply','reason':'Внешнее происшествие доступно по местным объявлениям.',
            'summary':'В городе отключили горячую воду. На дверях расклеены объявления.',
            'entities':[],'threads':[],
            'observations':[{'actor_id':a,'via':'news','observation':'На телефон пришло сообщение управляющей компании: отключили горячую воду.',
                             'entity_ids':[]} for a in ('arkady','garik')]}


class InterventionTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f;self.store=f.store

    def queue(self):enqueue(self.store,'author-event-1','В городе отключили горячую воду.')

    def test_one_global_call_two_distinct_evidence_records_no_posts_or_private_leaks(self):
        f=self.f;self.queue();f.backends['arkady'].answers=[event()]
        f.scheduler.tick()
        self.assertEqual(len(f.backends['arkady'].inputs),1)
        self.assertEqual(f.backends['garik'].inputs,[])
        self.assertEqual(catalog(self.store)[0]['status'],'applied')
        self.assertEqual(len(facts(self.store)),1)
        for actor in ('arkady','garik'):
            history=self.store.actor_view(actor)['history']
            worlds=[e for e in history if e['stage']=='world']
            self.assertEqual(len(worlds),1)
            self.assertIn('горячую воду',worlds[0]['observation'])
            self.assertNotIn('author_event',json.dumps(self.store.actor_view(actor)))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE stage='post'").fetchone())
        f.scheduler.tick();self.assertEqual(len(f.backends['arkady'].inputs),1)
        payload=SharedTimeline(self.store).enrich('world',{}, {'actor_id':'garik'})
        self.assertEqual(payload['author_world_facts'],facts(self.store))

    def test_pause_and_cancellation_never_call_a_model_and_request_is_idempotent(self):
        self.store.pause(True);self.queue();self.queue();self.f.scheduler.tick()
        self.assertEqual(len(catalog(self.store)),1)
        self.assertEqual(catalog(self.store)[0]['status'],'queued')
        self.assertFalse(self.f.backends['arkady'].inputs)
        with self.assertRaises(WorldConflict):enqueue(self.store,'author-event-1','Другие слова.')
        cancel(self.store,'author-event-1');self.store.pause(False);self.f.scheduler.tick()
        self.assertEqual(catalog(self.store)[0]['status'],'cancelled')
        self.assertFalse(self.f.backends['arkady'].inputs)

    def test_failure_saved_with_usage_without_retry_or_stopping_characters(self):
        self.queue();self.f.backends['arkady'].answers=[RuntimeError('synthetic failure')]
        self.f.scheduler.tick();self.f.scheduler.tick()
        self.assertEqual(catalog(self.store)[0]['status'],'failed')
        self.assertEqual(len(self.f.backends['arkady'].inputs),1)
        call=self.store.db.execute('SELECT status,usage FROM world_calls').fetchone()
        self.assertEqual(call['status'],'failed');self.assertIn('input_tokens',call['usage'])
        self.assertTrue(all(self.f.scheduler.enabled(a) for a in self.f.lives))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE stage='world'").fetchone())

    def test_direct_observation_requires_location_and_managed_actor_cannot_be_patched(self):
        from persona_runtime.world_interventions import validate
        state=self.store.state();payload={k:state[k] for k in ('actors','entities','positions','minute')}
        answer=event();answer['observations'][0]['via']='direct'
        payload['positions']=[]
        with self.assertRaises(ValueError):validate(answer,payload)
        answer=event();answer['entities']=[copy.deepcopy(next(e for e in state['entities'] if e['id']=='garik'))]
        with self.assertRaises(ValueError):validate(answer,payload)

    def test_unaware_actor_does_not_receive_omniscient_world_facts(self):
        answer=event();answer['observations'][1].update(via='unaware',observation='',entity_ids=[])
        self.queue();self.f.backends['arkady'].answers=[answer];self.f.scheduler.tick()
        self.assertFalse(self.store.actor_view('garik')['history'])
        self.assertEqual(catalog(self.store)[0]['result']['observations'][1]['via'],'unaware')

    def test_felt_effect_does_not_require_or_establish_a_location(self):
        self.store.db.execute('UPDATE peer_positions SET place_id=NULL')
        before=list(self.store.db.execute('SELECT actor,place_id FROM peer_positions'))
        answer=event()
        for obs in answer['observations']:
            obs.update(via='felt',observation='На секунду сильно тряхнуло; причина неизвестна.')
        self.queue();self.f.backends['arkady'].answers=[answer];self.f.scheduler.tick()
        self.assertEqual(catalog(self.store)[0]['status'],'applied')
        self.assertEqual(list(self.store.db.execute('SELECT actor,place_id FROM peer_positions')),before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM world_events WHERE stage='world'").fetchone()[0],2)

    def test_felt_cannot_reveal_local_subjects(self):
        from persona_runtime.world_interventions import validate
        state=self.store.state();payload={k:state[k] for k in ('actors','entities','positions','minute')}
        answer=event();answer['observations'][0].update(via='felt',entity_ids=['arkady_garage'])
        with self.assertRaisesRegex(ValueError,'felt observation'):validate(answer,payload)
        answer=event();answer['observations'][0]['via']='felt'
        subject=copy.deepcopy(next(e for e in state['entities'] if e['id']=='arkady_workbench'))
        subject['id']='unseen_workbench';answer['entities']=[subject]
        with self.assertRaisesRegex(ValueError,'felt observation'):validate(answer,payload)

    def test_explicit_felt_review_keeps_failed_call_usage_and_original_answer_once(self):
        from persona_runtime.world_interventions import recover_felt_event
        self.store.db.execute('UPDATE peer_positions SET place_id=NULL')
        answer=event()
        for obs in answer['observations']:obs.update(via='direct',observation='На секунду сильно тряхнуло; причина неизвестна.')
        self.queue();self.f.backends['arkady'].answers=[answer];self.f.scheduler.tick()
        row=catalog(self.store)[0];cid=row['cycle_id']
        original_call=dict(self.store.db.execute('SELECT * FROM world_calls').fetchone())
        original_rejected=copy.deepcopy(self.store.cycle(cid)['value']['rejected'])
        self.assertIn('местоположение',row['error'])
        self.assertEqual(self.store.cycle(cid)['value']['failure']['diagnostics']['validation_reason'],'direct_location_unknown')
        with self.assertRaises(WorldConflict):recover_felt_event(self.store,row['id'],'Explicit synthetic review')
        self.store.pause(True)
        recover_felt_event(self.store,row['id'],'Explicit synthetic review')
        recover_felt_event(self.store,row['id'],'Same review, no second application')
        self.assertEqual(catalog(self.store)[0]['status'],'applied')
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM world_calls').fetchone()),original_call)
        self.assertEqual(self.store.cycle(cid)['value']['rejected'],original_rejected)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM world_events WHERE stage='world'").fetchone()[0],2)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM world_events WHERE stage='post'").fetchone())
        self.assertEqual(len(self.f.backends['arkady'].inputs),1)

    def test_felt_review_cannot_project_local_entities(self):
        from persona_runtime.world_interventions import recover_felt_event
        self.store.db.execute('UPDATE peer_positions SET place_id=NULL')
        answer=event();answer['entities']=[copy.deepcopy(next(e for e in self.store.state()['entities'] if e['id']=='arkady_garage'))]
        for obs in answer['observations']:obs['via']='direct'
        self.queue();self.f.backends['arkady'].answers=[answer];self.f.scheduler.tick();self.store.pause(True)
        with self.assertRaises(WorldConflict):recover_felt_event(self.store,'author-event-1','Cannot infer location')
        self.assertEqual(catalog(self.store)[0]['status'],'failed')

    def test_native_backend_uses_closed_world_event_schema(self):
        backend=WorldBackend(self.f.root/'backend');backend._start=lambda:None;backend.client=Client(event())
        backend.workspace=self.f.root;backend.overrides={}
        self.assertEqual(backend.generate('world_intervention','World',{})[0],event())
        start=next(p for m,p in backend.client.calls if m=='turn/start')
        self.assertIn('observations',start['outputSchema']['properties'])

    def test_world_event_preserves_deadline_of_an_accepted_physical_action(self):
        cid=self.f.plan('garik',20,'home')
        before=copy.deepcopy(self.store.cycle(cid)['value']['shared_window'])
        self.queue();self.f.backends['arkady'].answers=[event()];self.f.scheduler.tick()
        cycle=self.store.cycle(cid)
        self.assertEqual(cycle['status'],'waiting')
        self.assertEqual(cycle['value']['shared_window'],before)
        self.assertNotIn('outcome',cycle['value']['results'])

    def test_cancelled_between_queue_read_and_claim_never_runs(self):
        from persona_runtime.world_interventions import realize
        self.queue();row=self.store.db.execute('SELECT * FROM world_interventions').fetchone()
        cancel(self.store,'author-event-1');realize(self.f.scheduler,row)
        self.assertFalse(self.f.backends['arkady'].inputs)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM world_calls').fetchone())


if __name__=='__main__':unittest.main()
