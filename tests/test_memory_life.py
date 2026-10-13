"""Synthetic memories/actions only. Tests do not invoke Codex or download an encoder."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.engine import Engine
from persona_runtime.memory import Memory, Budget
from persona_runtime.session import Session
from persona_runtime.store import Store
from persona_runtime.protocol import validate


def decision(**changes):
    value={'reply':None,'state':'Own local situation.','goals':'Own intentions, no external activity.',
           'wake_after_minutes':5,'memories':[],'relations':[],'journal':[],'memory_query':None}
    value.update(changes)
    return value


def note(key,text,visibility='person',kind='event',entities=None):
    return {'key':key,'kind':kind,'basis':'fiction','text':text,'visibility':visibility,'entities':entities or []}


class Backend:
    def __init__(self,*values):
        self.values=list(values)
        self.prompts=[]
    def decide(self,prompt):
        self.prompts.append(prompt)
        return copy.deepcopy(self.values.pop(0)),{'input_tokens':10,'output_tokens':5}


class MemoryLifeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        self.store=Store(self.directory)
        self.store.memory.dense=False
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def complete(self,value,text='Synthetic message',person='alice',conversation='one'):
        event_id=self.store.enqueue(text,person=person,conversation=conversation)
        event=self.store.claim()
        self.store.complete(event,value,{})
        return event_id
    def event(self,person='alice',text='bass',kind='message'):
        return {'person':person,'conversation':'one','kind':kind,'text':text}

    def test_full_recent_dialogue_is_not_duplicated_as_episode_but_facts_remain(self):
        event_id=self.complete(decision(reply='Bass is burgundy.',memories=[note('bass','Burgundy bass')]),text='Bass colour?')
        self.store.acknowledge(self.store.pending_output()[0]['id'])
        context,report=self.store.memory.context(self.event(),mode='recent')
        self.assertTrue(any(h['id']==event_id for h in context['history']))
        self.assertFalse(any(m['type']=='episode' and m['source_event']==event_id for m in context['memories']))
        self.assertTrue(any(m.get('key')=='bass' for m in context['memories']))
        self.assertGreater(report['duplicate_episode_chunks_removed'],0)
        other={**self.event(),'conversation':'another'}
        context,_=self.store.memory.context(other,mode='recent')
        self.assertTrue(any(m['type']=='episode' for m in context['memories']))

    def test_excerpted_history_keeps_recall_chunks(self):
        self.complete(decision(reply='Bass answer. '*150),text='Bass question. '*150)
        self.store.acknowledge(self.store.pending_output()[0]['id'])
        context,report=self.store.memory.context(self.event(),mode='recent')
        self.assertTrue(any(m['type']=='episode' for m in context['memories']))
        self.assertEqual(report['duplicate_episode_chunks_removed'],0)

    def test_old_preference_is_invalidated_but_source_and_versions_remain(self):
        old=self.complete(decision(memories=[note('bass','Burgundy bass')]))
        new=self.complete(decision(memories=[note('bass','Cream white bass')]))
        rows=list(self.store.db.execute('SELECT * FROM recollections ORDER BY created'))
        self.assertEqual(len(rows),2)
        self.assertIsNotNone(rows[0]['valid_to'])
        self.assertEqual(rows[1]['supersedes'],rows[0]['id'])
        hits,_=self.store.memory.search(self.event(), 'bass',mode='bm25')
        notes=[v for k,v in hits if v['record_type']=='note']
        self.assertEqual([v['text'] for v in notes],['Cream white bass'])
        self.assertEqual(notes[0]['source_event'],new)
        self.assertIsNotNone(self.store.db.execute('SELECT 1 FROM turns WHERE event_id=?',(old,)).fetchone())

    def test_private_profiles_episodes_and_graph_are_filtered_before_expansion(self):
        self.complete(decision(memories=[note('secret','Secret bass detail',entities=['bass'])],
                     relations=[{'key':'secret-edge','subject':'bass','predicate':'owner','object':'private',
                     'text':'Secret ownership','basis':'reported','visibility':'person'}]),text='Private transcript')
        self.complete(decision(memories=[note('own','My bass plan',visibility='self',entities=['bass'])]))
        for person,kind in [('bob','message'),('self','wake')]:
            hits,_=self.store.memory.search(self.event(person,kind=kind),'bass secret',mode='hybrid')
            joined=json.dumps(hits)
            self.assertIn('My bass plan',joined)
            self.assertNotIn('Secret',joined)
            self.assertNotIn('Private transcript',joined)

    def test_profile_follows_person_and_history_does_not_follow_other_chat(self):
        self.complete(decision(memories=[note('music','Alice likes melodic bass',kind='person')]))
        context,_=self.store.memory.context({'person':'alice','conversation':'two','kind':'message','text':'Hi'})
        self.assertEqual(context['history'],[])
        self.assertTrue(any(m['key']=='music' for m in context['memories']))

    def test_graph_adds_related_fact_without_matching_its_text(self):
        self.complete(decision(memories=[note('one','Uniqueanchor',entities=['instrument']),
                                        note('two','Second connected fact',entities=['instrument'])]))
        hits,report=self.store.memory.search(self.event(),'uniqueanchor',mode='hybrid',limit=1)
        self.assertEqual(len(hits),2)
        self.assertEqual(report['graph_added'],1)

    def test_long_history_and_many_notes_obey_whole_memory_budget(self):
        for number in range(25):
            self.complete(decision(reply='Synthetic bass answer. '*450,
                                   memories=[note(str(number),'Bass story '+('длинная запись '*100))]),
                          text='Synthetic bass question. '*600)
            output=self.store.pending_output()[0]
            self.store.acknowledge(output['id'])
        self.store.memory.budget=Budget(1200)
        context,report=self.store.memory.context(self.event(text='bass'))
        self.assertLessEqual(self.store.memory.budget.cost(context),1200)
        self.assertEqual(report['budget'],1200)

    def test_middle_of_long_episode_can_be_found_without_passing_whole_transcript(self):
        event_id=self.complete(decision(),text='Ordinary introduction. '*300+' rareanchor lithium souvenir '+'Ordinary ending. '*300)
        hits,_=self.store.memory.search(self.event(),'rareanchor',mode='bm25')
        self.assertTrue(hits)
        self.assertTrue(any('rareanchor' in value['text'] for _,value in hits))
        self.assertTrue(all(len(value['text'])<=900 for _,value in hits))
        self.assertTrue(all(value['source_event']==event_id for _,value in hits))

    def test_additional_recall_is_read_only_and_counts_both_model_calls(self):
        backend=Backend(decision(memory_query='old bass'),decision(reply='Synthetic final'))
        engine=Engine(self.store,backend)
        self.store.enqueue('Synthetic question')
        engine.step()
        self.assertEqual(engine.last_calls,2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.assertIn('"requested_recall": "old bass"',backend.prompts[1])
        self.assertIn('"memory_query_allowed": false',backend.prompts[1])
        usage=json.loads(self.store.db.execute('SELECT usage FROM turns').fetchone()[0])
        self.assertEqual(usage['input_tokens'],20)
        self.assertEqual(usage['model_calls'],2)

    def test_repeated_recall_or_exhausted_budget_cannot_write_any_decision(self):
        for max_calls in (1,2):
            backend=Backend(decision(memory_query='bass'),decision(memory_query='bass again'))
            engine=Engine(self.store,backend)
            event_id=self.store.enqueue('Synthetic question')
            with self.assertRaises(ValueError):
                engine.step(max_calls=max_calls)
            self.assertEqual(engine.last_calls,max_calls)
            self.assertEqual(self.store.db.execute('SELECT status FROM events WHERE id=?',(event_id,)).fetchone()[0],'failed')
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],0)

    def test_journal_is_real_durable_idempotent_and_has_host_receipt(self):
        event_id=self.complete(decision(journal=[{'kind':'reflection','title':'Synthetic diary','text':'A local thought.'}]))
        row=self.store.db.execute('SELECT * FROM journal').fetchone()
        path=self.directory/'journal'/(row['id']+'.md')
        self.assertIn('A local thought.',path.read_text())
        self.assertEqual(row['source_event'],event_id)
        receipt=self.store.db.execute("SELECT * FROM recollections WHERE basis='observed'").fetchone()
        self.assertEqual(receipt['owner'],'self')
        self.store.close()
        path.unlink()
        self.store=Store(self.directory)
        self.store.memory.dense=False
        self.assertTrue(path.exists())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],1)

    def test_export_error_does_not_repeat_committed_action(self):
        with patch.object(self.store.memory,'export_journal',side_effect=OSError('synthetic export unavailable')):
            self.complete(decision(journal=[{'kind':'story','title':'Synthetic story','text':'Fiction.'}]))
        self.assertEqual(self.store.db.execute('SELECT status FROM events').fetchone()[0],'done')
        self.assertIn('unavailable',self.store.get('journal_export_error'))
        self.store.sync_journal()
        self.assertEqual(self.store.get('journal_export_error'),'')
        self.assertEqual(len(list((self.directory/'journal').glob('*.md'))),1)

    def test_transaction_failure_rolls_back_graph_episode_journal_and_memory(self):
        self.store.db.execute("CREATE TRIGGER fail_turn BEFORE INSERT ON turns BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete(decision(memories=[note('a','Example')],
                         journal=[{'kind':'reflection','title':'Example','text':'Synthetic.'}],
                         relations=[{'key':'a','subject':'a','predicate':'likes','object':'b','text':'Example',
                                     'basis':'fiction','visibility':'self'}]))
        for table in ('recollections','relations','episodes','journal','outbox'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_outbox_delivery_does_not_cross_people(self):
        self.complete(decision(reply='Private Alice reply'))
        self.assertEqual(self.store.pending_output('bob','one'),[])
        self.assertEqual(len(self.store.pending_output('alice','one')),1)
        self.assertEqual(self.store.pending_output('alice','two'),[])

    def test_autonomous_wake_writes_without_user_message_then_waits(self):
        backend=Backend(decision(journal=[{'kind':'plan','title':'Synthetic intention','text':'An actual saved plan.'}]))
        engine=Engine(self.store,backend)
        session=Session(self.store,engine,max_calls=3,max_wakes=1,output=lambda _:None)
        self.assertTrue(session.tick(now=1000))
        self.assertEqual(session.wakes,1)
        self.assertEqual(session.calls,1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],1)
        self.assertFalse(session.tick(now=2000))
        self.assertEqual(len(backend.prompts),1)

    def test_pause_resume_deadline_and_call_budget(self):
        backend=Backend(decision(),decision())
        engine=Engine(self.store,backend)
        clock=[0]
        session=Session(self.store,engine,max_calls=1,clock=lambda:clock[0],output=lambda _:None)
        session.line('/pause')
        self.assertFalse(session.tick(now=1000))
        session.line('/resume')
        self.assertTrue(session.tick(now=1000))
        self.assertTrue(session.stopped)
        self.assertFalse(session.tick(now=2000))
        self.assertEqual(session.calls,1)
        other=Session(self.store,engine,clock=lambda:clock[0],max_minutes=1,output=lambda _:None)
        clock[0]=61
        self.assertFalse(other.tick(now=2000))

    def test_pipe_chat_does_not_drop_buffered_messages_or_run_after_quit(self):
        backend=Backend(decision(),decision())
        session=Session(self.store,Engine(self.store,backend),output=lambda _:None)
        session.run(io.StringIO('First\nSecond\n/quit\nThird\n'))
        self.assertEqual(len(backend.prompts),2)

    def test_query_cannot_also_modify_memory_or_journal(self):
        with self.assertRaises(ValueError):
            validate(decision(memory_query='bass',journal=[{'kind':'plan','title':'bad','text':'bad'}]))
        self.store.enqueue('Synthetic question')
        event=self.store.claim()
        with self.assertRaises(ValueError):
            self.store.complete(event,decision(memory_query='bass'),{})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    def test_v1_migration_backs_up_and_preserves_both_versions(self):
        self.complete(decision(memories=[note('bass','Burgundy')]))
        self.complete(decision(memories=[note('bass','White')]))
        with self.store.db:
            self.store.db.execute("DELETE FROM metadata WHERE key='memory_version'")
            for table in ('recall_fts','recall_vectors','retrieval_log','journal','relations','episodes','recollections'):
                self.store.db.execute('DROP TABLE '+table)
        self.store.close()
        self.store=Store(self.directory)
        self.store.memory.dense=False
        self.assertEqual(self.store.get('memory_version'),'2')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM recollections').fetchone()[0],2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM recollections WHERE valid_to IS NULL').fetchone()[0],1)
        backups=list((self.directory/'backups').glob('*.sqlite3'))
        self.assertEqual(len(backups),1)
        with sqlite3.connect(backups[0]) as old:
            self.assertEqual(old.execute('SELECT count(*) FROM turns').fetchone()[0],2)
        self.store.close()
        self.store=Store(self.directory)
        self.assertEqual(len(list((self.directory/'backups').glob('*.sqlite3'))),1)


if __name__=='__main__':
    unittest.main()
