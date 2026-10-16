"""Real queue semantics using synthetic readers, model answers and Bot API receipts."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from persona_runtime.channel_life import ChannelLife
from persona_runtime.channel_social import person_key,constrained_io,map_sources,LIVE_CHANNEL
from persona_runtime.world_protocol import validate_shape
import test_channel_life as fixtures
from test_telegram import GROUP,comment,forward


def rich(**changes):
    value={'decision':'keep','text':'','source_ids':[],'people':[],'commitments':[],
           'intent_updates':[],'fulfills':[],'reaction':None,'read_ids':[],'defer_ids':[],'leave':False,'attachment':None}
    value.update(changes);return value


class SourceHandleTests(unittest.TestCase):
    def test_handles_round_trip_only_structural_ids_and_constrain_choices(self):
        payload={'_source_ids':['tg-comment-3','own-outcome-1'],
                 'attention':{'focus_ids':['tg-comment-3']},
                 'life':{'reader_memory':[{'key':'reader-1'}],
                         'own_intentions':[{'key':'intro','kind':'post','status':'pending'}]},
                 'archive':[{'id':'tg-comment-3','text':'tg-comment-3 is what I wrote'}]}
        schema,encoded,decode=constrained_io(LIVE_CHANNEL,payload)
        self.assertEqual(encoded['archive'][0]['id'],'s1')
        self.assertEqual(encoded['archive'][0]['text'],'tg-comment-3 is what I wrote')
        self.assertNotIn('_source_ids',encoded)
        props=schema['properties']
        self.assertEqual(props['source_ids']['items']['enum'],['s1','s2'])
        self.assertEqual(props['read_ids']['items']['enum'],['s1'])
        self.assertEqual(props['people']['items']['properties']['person_key']['enum'],['reader-1'])
        self.assertEqual(props['fulfills']['items']['enum'],['intro'])
        result=map_sources(rich(source_ids=['s1'],text='s1',commitments=[]),decode)
        self.assertEqual(result['source_ids'],['tg-comment-3'])
        self.assertEqual(result['text'],'s1')
        self.assertNotIn('enum',LIVE_CHANNEL['properties']['source_ids']['items'])
        validate_shape(rich(source_ids=['s1']),schema)
        with self.assertRaises(ValueError):validate_shape(rich(source_ids=['s1 - absent']),schema)

    def test_no_empty_enum_for_absent_sources_or_people(self):
        schema,_,_=constrained_io(LIVE_CHANNEL,{'_source_ids':[]})
        for field in ('source_ids','read_ids','defer_ids','fulfills','people','intent_updates'):
            self.assertEqual(schema['properties'][field]['maxItems'],0)
        self.assertNotIn('"enum": []',json.dumps(schema))
        self.assertEqual(schema['properties']['reaction']['type'],'null')
        own,_,_=constrained_io(LIVE_CHANNEL,{'attention':{'purpose':'own_channel'}})
        self.assertEqual(own['properties']['decision']['enum'],['post','keep'])


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.LifeTests();self.fixture.setUp()
        f=self.fixture;self.store=f.store;self.backend=f.backend;self.connection=f.connection
        self.life=ChannelLife(f.lab,self.connection,'Synthetic',now=lambda:f.now,social_enabled=True)
        with self.store.lock,self.store.db:self.store.put('social_review_complete',True)

    def tearDown(self):self.fixture.tearDown()

    def schedule(self,seconds=900):
        with self.store.lock,self.store.db:
            status=self.store.get('life_status');status['next_wake']=self.fixture.now+seconds;self.store.put('life_status',status)

    def root(self):
        self.backend.answers=[rich(decision='post',text='Initial own post')]
        for _ in range(3):self.life.tick()
        self.schedule()
        return self.connection.state()['outbox'][0]['message_id']

    def receive(self,*messages):self.fixture.receive(*messages)

    def prepare_comments(self,*messages):
        post=self.root()
        self.receive(forward(50,post),*messages)
        self.life.tick()
        return self.store.get('channel_attention')['focus_ids']

    def test_chat_turn_neither_advances_world_clock_nor_postpones_life(self):
        ids=self.prepare_comments(comment())
        deadline=self.store.get('life_status')['next_wake'];minute=self.store.get('minute')
        self.backend.answers=[rich(decision='reply',text='short answer',source_ids=[ids[0]],read_ids=ids)]
        self.life.tick();self.life.tick()
        self.assertEqual(self.store.get('life_status')['next_wake'],deadline)
        self.assertEqual(self.store.get('minute'),minute)
        self.assertEqual(self.connection.api.calls[-1][0],'sendMessage')

    def test_own_channel_opportunity_preserves_comments_and_excludes_replies_from_recent_posts(self):
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(decision='reply',text='ONLY IN COMMENTS',source_ids=ids,read_ids=ids)]
        self.life.tick();self.life.tick()
        with self.store.lock,self.store.db:self.store.put('own_channel_check_requested',True)
        self.life.tick()
        self.assertFalse(self.store.get('own_channel_check_requested'))
        self.backend.answers=[rich(decision='post',text='A CHANNEL FRAGMENT')]
        self.life.tick()
        payload=self.backend.inputs[-1][2]
        self.assertEqual(payload['attention']['purpose'],'own_channel')
        self.assertEqual(payload['attention']['focus_ids'],[])
        self.assertNotIn('ONLY IN COMMENTS',payload['previous_posts'])
        self.assertIn('Initial own post',payload['previous_posts'])
        self.assertIn('sharing_appetite',payload['publication'])

    def test_channel_post_and_comment_can_both_happen_after_one_episode(self):
        ids=self.prepare_comments(comment())
        # End the prepared comment visit without consuming this reader message.
        self.backend.answers=[rich(leave=True)]
        self.life.tick()
        self.life.focus(own_channel=True);cycle=self.life.channel_cycle()
        with self.store.lock,self.store.db:
            value=cycle['value'];value['channel_visit']=False
            self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(value),cycle['id']))
            self.store.put('channel_visit',{})
        self.backend.answers=[rich(decision='post',text='Life fragment')]
        self.life.tick();self.life.tick()
        self.assertEqual(self.backend.inputs[-1][2]['attention']['purpose'],'own_channel')
        self.assertEqual(self.store.db.execute('SELECT considered FROM channel_read_batches WHERE id=?',(ids[0],)).fetchone()[0],0)
        self.life.tick()
        self.backend.answers=[rich(decision='reply',text='Reader reply',source_ids=ids,read_ids=ids)]
        self.life.tick();self.life.tick()
        texts=[p['value']['text'] for p in self.store.monitor()['posts']]
        self.assertIn('Life fragment',texts);self.assertIn('Reader reply',texts)

    def test_own_channel_can_keep_silent_but_cannot_spend_its_choice_on_reply(self):
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(leave=True)];self.life.tick()
        self.life.focus(own_channel=True);self.life.channel_cycle()
        self.backend.answers=[rich()];self.life.tick()
        self.assertEqual(self.life.state()['status'],'running')
        self.life.focus(own_channel=True);self.life.channel_cycle()
        self.backend.answers=[rich(decision='reply',text='Wrong surface',source_ids=ids)]
        self.life.tick()
        self.assertEqual(self.life.state()['status'],'error')
        self.assertFalse([p for p in self.store.monitor()['posts'] if p['value']['text']=='Wrong surface'])

    def test_unanswered_comment_is_not_silently_marked_read_with_the_whole_batch(self):
        ids=self.prepare_comments(comment(),comment(52))
        self.backend.answers=[rich(decision='reply',text='one answer',source_ids=[ids[0]],read_ids=[ids[0]])]
        self.life.tick()
        rows=dict(self.store.db.execute('SELECT id,considered FROM channel_read_batches'))
        self.assertEqual(rows[ids[0]],1);self.assertEqual(rows[ids[1]],0)

    def test_old_root_and_old_pending_suggestion_survive_recent_window(self):
        ids=self.prepare_comments(comment())
        with self.store.lock,self.store.db:
            base=self.store.monitor()['posts'][0]
            for i in range(20):
                cid='extra-cycle-'+str(i)
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',
                    (cid,'garik','complete','done',self.store.get('revision'),json.dumps({'results':{}}),self.fixture.now))
                self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                    ('extra-post-'+str(i),cid,'garik','post',self.store.get('minute'),json.dumps({'text':'Other','source_ids':[]})))
        context=self.store.channel_context('garik')
        self.assertIn(ids[0],[e['id'] for e in context])
        original=next(e for e in context if e['id']==ids[0])['value']['post_id']
        self.assertIn(original,[e['id'] for e in context])

    def test_due_life_wins_over_a_new_comment_stream(self):
        post=self.root();self.schedule(0)
        self.receive(forward(50,post),comment())
        self.life.tick()
        cycle=self.store.monitor()['cycles'][0]
        self.assertEqual(cycle['phase'],'horizon')
        self.assertFalse(cycle['value'].get('channel_visit'))
        self.assertFalse(self.backend.answers)

    def test_short_visit_leaves_even_when_more_comments_wait(self):
        ids=self.prepare_comments(comment(),comment(52))
        self.backend.answers=[rich(read_ids=[ids[0]],leave=True)]
        self.life.tick();self.life.tick()
        self.assertEqual(self.life.current_phase,'away')
        self.assertGreater(self.store.get('channel_visit')['away_until'],self.fixture.now)
        self.assertEqual(self.store.db.execute('SELECT considered FROM channel_read_batches WHERE id=?',(ids[1],)).fetchone()[0],0)
        calls=len(self.backend.inputs);self.life.tick();self.assertEqual(calls,len(self.backend.inputs))

    def test_reader_keys_distinguish_identical_names_and_do_not_identify_anonymous_people(self):
        self.assertNotEqual(person_key({'user_id':1},GROUP['id']),person_key({'user_id':2},GROUP['id']))
        self.assertIn('anonymous',person_key({'user_id':None},GROUP['id']))

    def test_profile_is_source_backed_and_survives_reopening_and_window_eviction(self):
        ids=self.prepare_comments(comment())
        key=person_key({'user_id':42},GROUP['id'])
        person={'person_key':key,'impression':'Argued, then apologized.','source_ids':ids,
                'facts':[{'key':'apology','text':'Сказал, что извинился.','source_ids':ids}],'forget_fact_keys':[]}
        self.backend.answers=[rich(people=[person],read_ids=ids)]
        self.life.tick()
        from persona_runtime.channel_social import SocialMemory
        reopened=SocialMemory(self.store)
        saved=reopened.context()['reader_memory'][0]
        self.assertEqual(saved['impression'],'Argued, then apologized.')
        self.assertEqual(saved['facts'][0]['sources'],ids)
        self.assertIn('reader_memory',self.store.actor_view('garik'))
        with self.store.lock,self.store.db:self.store.register_actor('other','Other')
        self.assertFalse(reopened.context('other')['reader_memory'])

    def test_memory_with_invented_or_other_reader_evidence_is_rejected_before_accepting_text(self):
        ids=self.prepare_comments(comment())
        person={'person_key':'tg-user-42','impression':'False','source_ids':['invented'],
                'facts':[],'forget_fact_keys':[]}
        self.backend.answers=[rich(decision='reply',text='should not be accepted',source_ids=ids,people=[person])]
        self.life.tick()
        self.assertEqual(self.life.state()['status'],'error')
        self.assertFalse([p for p in self.store.monitor()['posts'] if p['value']['text']=='should not be accepted'])

    def test_promised_post_is_pending_only_after_ack_delivery_and_done_only_after_post_delivery(self):
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(decision='reply',text='good idea, I will write it',source_ids=ids,read_ids=ids,
            commitments=[{'key':'intro','kind':'post','action':'Write introduction','quote':'I will write it','source_ids':ids,'after_minutes':0}])]
        self.life.tick()
        row=self.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()
        self.assertEqual(row[0],'awaiting_delivery')
        self.life.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()[0],'pending')
        self.life.tick()
        self.backend.answers=[rich(decision='post',text='Here is the introduction',fulfills=['intro'])]
        self.life.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()[0],'pending')
        self.life.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()[0],'done')

    def test_failed_commit_retains_full_social_output_for_exact_review(self):
        ids=self.prepare_comments(comment())
        answer=rich(decision='reply',text='I will write it',source_ids=ids,read_ids=ids,
            commitments=[{'key':'intro','kind':'post','action':'Write introduction','quote':'I will write it','source_ids':ids,'after_minutes':0}])
        self.backend.answers=[answer]
        with patch.object(self.store,'complete',side_effect=ValueError('synthetic commit rejection')):
            self.life.tick()
        cycle=next(c for c in self.store.monitor()['cycles'] if c['status']=='blocked')
        self.assertEqual(cycle['value']['rejected']['answer'],answer)
        calls=len(self.backend.inputs)
        self.store.recover_channel(cycle['id'],extension=self.life.social)
        self.assertEqual(len(self.backend.inputs),calls)
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='intro'").fetchone()[0],'awaiting_delivery')

    def test_reply_can_cite_an_old_request_without_mistaking_it_for_second_addressee(self):
        ids=self.prepare_comments(comment(),comment(52),comment(53))
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE channel_read_batches SET considered=1 WHERE id=?',(ids[0],))
            attention=self.store.get('channel_attention');attention['focus_ids']=ids[1:];self.store.put('channel_attention',attention)
        answer=rich(decision='reply',text='I remember your earlier request.',source_ids=ids[:2],
                    read_ids=ids[1:],reaction={'audience_id':ids[2],'emoji':'👍'})
        self.backend.answers=[answer]
        with patch.object(self.store,'complete',side_effect=ValueError('synthetic previous target guard')):self.life.tick()
        cycle=next(c for c in self.store.monitor()['cycles'] if c['status']=='blocked')
        calls=len(self.backend.inputs)
        self.store.recover_channel(cycle['id'],extension=self.life.social)
        post=next(p for p in self.store.monitor()['posts'] if p['cycle_id']==cycle['id'])
        self.assertEqual(post['value']['audience_id'],ids[1])
        self.assertEqual(post['value']['source_ids'],ids[:2])
        self.assertEqual(len(self.backend.inputs),calls)
        self.assertEqual(self.store.db.execute('SELECT status FROM channel_reaction_jobs').fetchone()[0],'pending')
        self.assertTrue(self.backend.inputs[-1][2]['media_capabilities']['gallery_browser'])

    def test_two_current_addressees_are_still_ambiguous(self):
        ids=self.prepare_comments(comment(),comment(52))
        self.backend.answers=[rich(decision='reply',text='Ambiguous',source_ids=ids)]
        self.life.tick()
        self.assertEqual(self.life.state()['status'],'error')

    def test_eating_is_not_completed_by_saying_i_will_eat(self):
        ids=self.prepare_comments(comment())
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO personal_intentions VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('garik','eat','life','Eat soup','pending',json.dumps(ids),'',self.fixture.now,None,self.fixture.now))
        self.backend.answers=[rich(read_ids=ids,intent_updates=[{'key':'eat','status':'done','note':'Going to eat','source_ids':ids}])]
        self.life.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='eat'").fetchone()[0],'pending')
        self.assertEqual(self.life.state()['status'],'error')

    def test_eating_intention_can_close_after_occurred_outcome(self):
        ids=self.prepare_comments(comment())
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO personal_intentions VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('garik','eat','life','Eat soup','pending',json.dumps(ids),'',self.fixture.now,None,self.fixture.now))
            self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                ('meal-outcome',self.store.monitor()['posts'][0]['cycle_id'],'garik','outcome',self.store.get('minute'),
                 json.dumps({'observation':'Ate a bowl of soup.','visible_to':['garik']})))
        self.backend.answers=[rich(read_ids=ids,intent_updates=[{'key':'eat','status':'done','note':'Ate it','source_ids':['meal-outcome']}])]
        self.life.tick()
        self.assertEqual(self.store.db.execute("SELECT status FROM personal_intentions WHERE key='eat'").fetchone()[0],'done')
        self.assertEqual(self.life.state()['status'],'running')

    def test_reaction_can_replace_a_text_reply_and_gets_a_bot_api_receipt(self):
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(reaction={'audience_id':ids[0],'emoji':'🤣'},read_ids=ids)]
        self.life.tick();self.life.tick()
        reactions=[c for c in self.connection.api.calls if c[0]=='setMessageReaction']
        self.assertEqual(len(reactions),1);self.assertEqual(reactions[0][1]['message_id'],51)
        self.assertEqual(self.store.db.execute('SELECT status FROM channel_reaction_jobs').fetchone()[0],'sent')
        self.assertEqual(len(self.store.monitor()['posts']),1)

    def test_failed_reaction_capability_check_is_not_an_uncertain_send(self):
        from persona_runtime.telegram import TelegramError
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(reaction={'audience_id':ids[0],'emoji':'🤣'},read_ids=ids)]
        self.life.tick()
        original=self.connection.api.call
        def failed(method,*args,**kwargs):
            if method=='getChat':raise TelegramError('Synthetic preflight failure')
            return original(method,*args,**kwargs)
        self.connection.api.call=failed
        self.life.tick()
        self.assertEqual(self.store.get('life_status')['status'],'error')
        self.assertFalse(self.store.get('last_reaction_failure')['dispatch_recorded'])
        self.assertEqual(self.store.db.execute('SELECT status FROM channel_reaction_jobs').fetchone()[0],'pending')
        self.assertFalse(self.connection.db.execute('SELECT 1 FROM reactions').fetchone())
        self.assertFalse([c for c in self.connection.api.calls if c[0]=='setMessageReaction'])

    def test_failed_dispatched_reaction_stays_uncertain_and_is_not_requeued(self):
        ids=self.prepare_comments(comment())
        self.backend.answers=[rich(reaction={'audience_id':ids[0],'emoji':'🤣'},read_ids=ids)]
        self.life.tick();self.connection.api.fail_send=True;self.life.tick()
        self.assertTrue(self.store.get('last_reaction_failure')['dispatch_recorded'])
        self.assertEqual(self.store.db.execute('SELECT status FROM channel_reaction_jobs').fetchone()[0],'sending')
        self.assertEqual(self.connection.db.execute('SELECT status FROM reactions').fetchone()[0],'unknown')
        calls=len([c for c in self.connection.api.calls if c[0]=='setMessageReaction'])
        self.life.tick()
        self.assertEqual(len([c for c in self.connection.api.calls if c[0]=='setMessageReaction']),calls)

    def test_archived_keyword_fact_can_be_retrieved_beyond_recent_eight(self):
        ids=self.prepare_comments(comment())
        with self.store.lock,self.store.db:
            for i in range(20):
                self.store.db.execute('INSERT INTO reader_facts VALUES(?,?,?,?,?,?,?)',
                    ('garik','tg-user-42','fact-'+str(i),'СТАРАЯ ТЫКВА' if i==0 else 'New fact '+str(i),json.dumps(ids),1,i))
        facts=self.life.social.context(query='помнишь тыква')['reader_memory'][0]['facts']
        self.assertIn('СТАРАЯ ТЫКВА',[f['text'] for f in facts])

    def test_bootstrap_review_uses_saved_own_channel_only_without_sending_or_backstage_leak(self):
        ids=self.prepare_comments(comment())
        with self.store.lock,self.store.db:self.store.put('social_review_complete',False)
        self.backend.answers=[{'people':[],'intentions':[]}]
        sends=len([c for c in self.connection.api.calls if c[0]=='sendMessage'])
        self.life.tick()
        self.assertTrue(self.store.get('social_review_complete'))
        self.assertEqual(self.backend.inputs[-1][0],'social_review')
        self.assertNotIn('world_note',json.dumps(self.backend.inputs[-1][2]))
        self.assertEqual(len([c for c in self.connection.api.calls if c[0]=='sendMessage']),sends)


if __name__=='__main__':unittest.main()
