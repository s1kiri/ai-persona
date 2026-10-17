"""Two real actor queues in one disposable world, synthetic model and Bot API only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.character_profile import CharacterProfile
from persona_runtime.channel_social import SocialMemory
from persona_runtime.profile_host import claim_setup
from persona_runtime.shared_host import initialize_world, attach_channel, identity
from persona_runtime.shared_protocol import PEER_CHOICE
from persona_runtime.shared_scheduler import SharedScheduler, SharedChannelLife
from persona_runtime.shared_world import SharedWorldStore, SharedTimeline
from persona_runtime.telegram import TelegramError
from persona_runtime.world import WorldSession, WorldConflict
from persona_runtime.world_lab import Laboratory
from persona_runtime.world_protocol import validate_shape
from test_world import Backend, choice, outcome
from test_world_personal import quiet, experience
from test_channel_social import rich
from test_telegram import API, Connection, TOKEN

ROOT=Path(__file__).resolve().parents[1]


class PairAPI(API):
    counter=100
    def __init__(self,token,number):
        super().__init__(token);self.number=number
        self.channel={'id':-10000-number*10,'type':'channel','title':'Channel '+str(number),'linked_chat_id':-10001-number*10}
        self.group={'id':self.channel['linked_chat_id'],'type':'supergroup','title':'Discussion '+str(number),'linked_chat_id':self.channel['id']}
    def call(self,method,payload=None,**kwargs):
        if method=='getMe':
            return {'id':100000+self.number,'is_bot':True,'username':'actor_'+str(self.number),'first_name':'Actor'}
        if method=='getChat':return dict(self.channel if payload['chat_id']==self.channel['id'] else self.group)
        if method in ('sendMessage','sendPhoto'):
            result=super().call(method,payload,**kwargs);PairAPI.counter+=1;result['message_id']=PairAPI.counter;return result
        return super().call(method,payload,**kwargs)


def peer_answer(**changes):
    answer={'decision':'keep','recipient_actor_id':None,'text':'','source_ids':[],'read_ids':[],
            'invitation_id':None,'place_id':None,'post_id':None,
            'activity':'keep','commitment':None,'commitment_updates':[]}
    answer.update(changes);return answer


class SharedLifeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=1800000000.;self.store=SharedWorldStore(self.root/'world');self.addCleanup(lambda:self.store.close())
        self.profiles={a:CharacterProfile.load(ROOT/'characters'/a/'character.json') for a in ('arkady','garik')}
        self.config={'version':1,'data_dir':self.root/'world','port':0,'shared_places':['arkady_garage_yard'],
            'actors':[{'profile':p,'telegram_dir':self.root/a,'setup_port':0,'initial_place':'arkady_garage' if a=='arkady' else 'home'} for a,p in self.profiles.items()]}
        initialize_world(self.store,self.config,now=self.now)
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':0},0)
        self.connections={};self.backends={};self.lives={}
        for number,item in enumerate(self.config['actors'],1):
            p=item['profile'];actor=p.actor_id
            c=Connection(item['telegram_dir'],lambda token,n=number:PairAPI(token,n));self.addCleanup(c.close)
            c.configure(TOKEN);c.bind(c.api.channel['id']);claim_setup(c,actor);attach_channel(self.store,item,c,self.now)
            b=Backend();session=WorldSession(self.store,b,{actor:p.persona},p.speech_contract,personal_life=True,active_world=True,
                autonomous_posts=True,direct_channel=True,external_channel=True,continuous=True,timeline=SharedTimeline(self.store))
            lab=Laboratory(self.store,session);lab.actor_id,lab.actor_name=actor,p.name
            life=SharedChannelLife(lab,c,p.persona,now=lambda:self.now,social_enabled=True,actor_id=actor)
            self.connections[actor]=c;self.backends[actor]=b;self.lives[actor]=life
            with self.store.lock,self.store.db:
                life.put('social_review_complete',True);life.put('bootstrap_prepared',True)
                life.put('peer_next_check',self.now+100000)
                status=life.get('life_status');status['next_wake']=self.now+100000;life.put('life_status',status)
            life.action(True)
        self.scheduler=SharedScheduler(self.store,self.lives,now=lambda:self.now)

    def plan(self,actor,minutes=30,destination=None,intent='Заняться своим делом.',commitment_ids=None):
        life=self.lives[actor];b=self.backends[actor]
        observed=quiet();observed.update(entity_ids=['arkady_garage'] if actor=='arkady' else ['home'],visible_to=[actor])
        b.answers += [observed,intent,{**choice(action=intent,entity_ids=[]), 'duration_minutes':minutes,'destination_id':destination,'commitment_ids':commitment_ids or []}]
        cycle=life.lab.session.prepare(actor,minutes=0,draw=1)
        for _ in range(3):life.lab.session.step(cycle['id'])
        self.scheduler._run_cycle(self.store.cycle(cycle['id']))
        self.assertEqual(self.store.cycle(cycle['id'])['status'],'waiting')
        return cycle['id']

    def finish(self,actor,cid,minutes,**changes):
        final_place=changes.pop('final_place_id',self.store.cycle(cid)['value']['shared_window']['destination_id'])
        self.backends[actor].answers += [{**outcome(elapsed_minutes=minutes,time_costs=[{'activity':'Занятие','minutes':minutes}],**changes),'final_place_id':final_place},
            {**experience(cid+'-outcome',concerns=[]),'intent_updates':[]},rich()]
        for _ in range(12):
            self.scheduler.tick()
            if self.store.cycle(cid)['status']=='complete':break
        self.assertEqual(self.store.cycle(cid)['status'],'complete',self.lives[actor].state())

    def job(self,actor,answer):
        b=self.backends[actor];b.answers.append(answer);self.scheduler.peer_turn(actor)
        row=self.store.db.execute('SELECT * FROM peer_jobs WHERE sender=? ORDER BY rowid DESC LIMIT 1',(actor,)).fetchone()
        self.scheduler.deliver_peer(row);return row['id']

    def root_post(self,actor,text='A delivered public post'):
        life=self.lives[actor];self.backends[actor].answers.append(rich(decision='post',text=text))
        life.focus(own_channel=True);cycle=life.channel_cycle();life.lab.session.step(cycle['id'])
        row=self.store.db.execute("SELECT * FROM world_events WHERE cycle_id=? AND stage='post'",(cycle['id'],)).fetchone()
        post={**dict(row),'value':json.loads(row['value'])};life.deliver(post)
        return post['id']

    def forward(self,actor,post_id):
        c=self.connections[actor];v=json.loads(self.store.db.execute('SELECT value FROM world_events WHERE id=?',(post_id,)).fetchone()[0])
        mid=v['delivery']['receipts'][0]['message_id']
        with c.lock,c.db:c.db.execute('INSERT OR IGNORE INTO forwards VALUES(?,?,?,?)',(c.api.channel['id'],mid,c.api.group['id'],mid+10000))

    def test_long_action_does_not_advance_clock_or_block_the_other_actor(self):
        before=self.store.get('minute');a=self.plan('arkady',120)
        self.assertEqual(self.store.get('minute'),before)
        self.assertNotIn('outcome',self.store.cycle(a)['value']['results'])
        g=self.plan('garik',5)
        self.now+=5*60;self.finish('garik',g,5,observation='GARIK-RESULT')
        self.assertEqual(self.store.get('minute'),before+5)
        self.assertEqual(self.store.cycle(a)['status'],'waiting')
        self.assertNotIn('GARIK-RESULT',json.dumps(self.store.actor_view('arkady')))
        self.assertEqual([i[0] for i in self.backends['garik'].inputs],['world','impulse','timed_choice','outcome','live_experience','live_channel'])

    def test_peer_messages_are_once_only_and_never_copy_private_sources(self):
        source=self.store.relay('arkady','garik','message','Hello.',source_ids=['arkady_memory_body'],delivery_id='one')
        same=self.store.relay('arkady','garik','message','Hello.',source_ids=['arkady_memory_body'],delivery_id='one')
        self.assertEqual(source,same)
        received=self.store.peer_context('garik')['events'][0]['value']
        self.assertNotIn('source_ids',received)
        own=self.store.peer_context('arkady')['events'][0]['value'];self.assertEqual(own['source_ids'],['arkady_memory_body'])
        self.assertNotIn('arkady_memory_body',self.lives['garik'].social.evidence('garik'))
        self.assertIn(source,self.lives['garik'].social.evidence('garik'))
        with self.assertRaises(WorldConflict):self.store.relay('arkady','garik','message','Different.',delivery_id='one')

    def test_received_words_can_source_an_own_post_about_the_other_actor(self):
        source=self.store.relay('arkady','garik','message','Предлагаю посмотреть приёмник.')
        life=self.lives['garik'];life.focus(own_channel=True);cycle=life.channel_cycle()
        self.backends['garik'].answers=[rich(decision='post',text='Аркадий зовёт смотреть приёмник. Ещё не решил, пойду ли.',source_ids=[source])]
        life.lab.session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'],'complete')
        payload=self.backends['garik'].inputs[-1][2]
        self.assertIn('Предлагаю посмотреть',json.dumps(payload,ensure_ascii=False))
        self.assertNotIn(self.profiles['arkady'].persona,self.backends['garik'].inputs[-1][1])

    def test_invitation_requires_recipient_choice_and_separate_arrival(self):
        job=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Заходи в проезд.',place_id='arkady_garage_yard'))
        inv=dict(self.store.db.execute('SELECT * FROM peer_invitations WHERE id=?',(job,)).fetchone())
        self.assertEqual(inv['status'],'pending');self.assertEqual(self.store.nearby('arkady'),[])
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Да, зайду.',invitation_id=job,place_id='arkady_garage_yard',source_ids=[inv['source_id']]))
        self.assertEqual(self.store.db.execute('SELECT status FROM peer_invitations WHERE id=?',(job,)).fetchone()[0],'accepted')
        self.assertEqual(self.store.nearby('arkady'),[])
        a=self.plan('arkady',2,'arkady_garage_yard','Выйду в проезд у гаража.')
        g=self.plan('garik',5,'arkady_garage_yard','Пойду в проезд у гаража Аркадия.')
        self.now+=2*60;self.finish('arkady',a,2)
        self.assertEqual(self.store.nearby('arkady'),[])
        self.now+=3*60;self.finish('garik',g,5)
        self.assertEqual(self.store.nearby('arkady'),['garik'])
        meeting=self.store.peer_context('garik')['invitations'][0]
        self.assertEqual(meeting['status'],'met');self.assertEqual(meeting['proposal'],'Заходи в проезд.')
        self.assertEqual(meeting['responses'],[{'actor_id':'garik','text':'Да, зайду.'}])
        self.job('arkady',peer_answer(decision='say',recipient_actor_id='garik',text='Привет. Рад, что дошёл.'))
        self.assertIn('Рад, что дошёл',json.dumps(self.store.peer_context('garik'),ensure_ascii=False))

    def test_refusal_stays_refusal_and_remote_scene_is_rejected(self):
        job=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Приходи.',place_id='arkady_garage_yard'))
        self.job('garik',peer_answer(decision='decline',recipient_actor_id='arkady',text='Сегодня нет.',invitation_id=job))
        self.assertEqual(self.store.db.execute('SELECT status FROM peer_invitations WHERE id=?',(job,)).fetchone()[0],'declined')
        payload={'life':self.store.actor_view('arkady'),'peer_context':self.store.peer_context('arkady'),'available_source_ids':[]}
        with self.assertRaises(ValueError):self.scheduler.validate_peer('arkady',peer_answer(decision='say',recipient_actor_id='garik',text='Hello'),payload)
        with self.assertRaises(ValueError):self.scheduler.validate_peer('arkady',peer_answer(decision='accept',recipient_actor_id='garik',text='Yes',invitation_id=job),payload)

    def test_idle_peer_check_needs_new_words_own_life_or_meeting_state(self):
        actor='garik';life=self.lives[actor]
        with self.store.lock,self.store.db:life.put('peer_next_check',self.now)
        self.backends[actor].answers=[peer_answer()];self.scheduler.tick()
        calls=len(self.backends[actor].inputs)
        self.now+=301
        for _ in range(3):self.scheduler.tick()
        self.assertEqual(len(self.backends[actor].inputs),calls)
        self.store.relay('arkady',actor,'message','У меня появилась мысль.')
        self.backends[actor].answers=[peer_answer(read_ids=self.store.peer_context(actor)['unread_ids'])]
        self.now+=301;self.scheduler.tick()
        self.assertEqual(len(self.backends[actor].inputs),calls+1)
        self.assertEqual(self.backends[actor].inputs[-1][2]['surface'],'peer_conversation')
        self.now+=301
        for _ in range(3):self.scheduler.tick()
        self.assertEqual(len(self.backends[actor].inputs),calls+1)

    def test_failed_research_has_no_facts_and_finishes_as_failed_attempt_without_retry(self):
        from persona_runtime.codex import CodexError
        cid=self.plan('garik',15,intent='Поищу сведения по вымышленному вопросу.')
        with self.store.lock,self.store.db:
            value=self.store.cycle(cid)['value'];value['results']['choice']['kind']='research'
            self.store.db.execute("UPDATE world_cycles SET status='ready',phase='research',value=? WHERE id=?",(json.dumps(value),cid))
        self.lives['garik'].lab.session.research_enabled=True
        self.backends['garik'].answers=[CodexError('synthetic read failure')]
        self.scheduler._run_cycle(self.store.cycle(cid))
        cycle=self.store.cycle(cid);self.assertEqual(cycle['status'],'waiting');self.assertEqual(cycle['phase'],'outcome')
        self.assertEqual(cycle['value']['results']['research_failure']['facts'],[])
        self.assertNotIn('research',cycle['value']['results'])
        call=dict(self.store.db.execute("SELECT * FROM world_calls WHERE cycle_id=? AND stage='research'",(cid,)).fetchone())
        self.assertEqual(call['status'],'failed')
        self.now+=15*60;self.finish('garik',cid,15,status='failed',observation='Поиск прервался; подтверждённых сведений нет.')
        self.assertEqual(sum(role=='research' for role,_,_ in self.backends['garik'].inputs),1)
        payload=next(p for role,_,p in self.backends['garik'].inputs if role=='outcome')
        self.assertEqual(payload['research_failure']['facts'],[]);self.assertNotIn('research_receipt',payload)
        after=dict(self.store.db.execute('SELECT * FROM world_calls WHERE id=?',(call['id'],)).fetchone())
        self.assertEqual(call,after)

    def test_cancelled_research_is_not_automatically_resolved(self):
        from persona_runtime.codex import CodexCancelled
        cid=self.plan('garik',15,intent='Поищу сведения.')
        with self.store.lock,self.store.db:
            value=self.store.cycle(cid)['value'];value['results']['choice']['kind']='research'
            self.store.db.execute("UPDATE world_cycles SET status='ready',phase='research',value=? WHERE id=?",(json.dumps(value),cid))
        self.lives['garik'].lab.session.research_enabled=True;self.backends['garik'].answers=[CodexCancelled('synthetic cancellation')]
        with self.assertRaises(CodexCancelled):self.scheduler._run_cycle(self.store.cycle(cid))
        self.assertEqual(self.store.cycle(cid)['status'],'blocked')
        self.assertNotIn('research_failure',self.store.cycle(cid)['value']['results'])

    def test_confirmed_comment_consumes_its_source_even_without_read_ids(self):
        post=self.root_post('arkady');self.forward('arkady',post);self.scheduler.circulate_posts()
        source=self.store.peer_context('garik')['unread_ids'][0]
        self.job('garik',peer_answer(decision='comment',recipient_actor_id='arkady',text='Один ответ.',post_id=post,source_ids=[source]))
        self.assertNotIn(source,self.store.peer_context('garik')['unread_ids'])
        payload={'life':self.store.actor_view('garik'),'peer_context':self.store.peer_context('garik'),'available_source_ids':[source]}
        with self.assertRaises(ValueError):self.scheduler.validate_peer('garik',peer_answer(decision='comment',recipient_actor_id='arkady',text='Опять.',post_id=post,source_ids=[source]),payload)

    def test_public_posts_circulate_only_after_receipt_and_only_once(self):
        post=self.root_post('arkady');self.scheduler.circulate_posts();self.scheduler.circulate_posts()
        received=[e for e in self.store.peer_context('garik')['events'] if e['value']['kind']=='public_post']
        self.assertEqual(len(received),1);self.assertEqual(received[0]['value']['post_id'],post)
        self.assertNotIn('source_ids',received[0]['value'])

    def test_cross_channel_comment_is_sent_by_its_author_and_can_receive_a_reply(self):
        post=self.root_post('arkady');self.forward('arkady',post);self.scheduler.circulate_posts()
        event=next(e for e in self.store.peer_context('garik')['events'] if e['value']['kind']=='public_post')
        self.job('garik',peer_answer(decision='comment',recipient_actor_id='arkady',text='Это я одобряю.',post_id=post,source_ids=[event['id']]))
        c=self.connections['garik'];sent=c.state()['outbox'][0]
        self.assertEqual(sent['chat_id'],self.connections['arkady'].api.group['id'])
        self.assertEqual(sent['status'],'sent')
        life=self.lives['arkady'];life.focus();source=life.get('channel_attention')['focus_ids'][0]
        self.backends['arkady'].answers=[rich(decision='reply',text='Я знал, что тебе понравится.',source_ids=[source],read_ids=[source])]
        cycle=life.channel_cycle();life.lab.session.step(cycle['id'])
        row=self.store.db.execute("SELECT * FROM world_events WHERE cycle_id=? AND stage='post'",(cycle['id'],)).fetchone()
        life.deliver({**dict(row),'value':json.loads(row['value'])})
        self.assertEqual(self.connections['arkady'].state()['outbox'][0]['status'],'sent')

    def test_unknown_peer_delivery_freezes_only_peer_lane_without_retry(self):
        post=self.root_post('arkady');self.forward('arkady',post);self.scheduler.circulate_posts()
        source=self.store.peer_context('garik')['events'][0]['id']
        self.backends['garik'].answers=[peer_answer(decision='comment',recipient_actor_id='arkady',text='Reply',post_id=post,source_ids=[source])]
        self.scheduler.peer_turn('garik');self.connections['garik'].api.fail_send=True
        self.scheduler.tick()
        self.assertEqual(self.lives['garik'].get('life_status')['status'],'running')
        self.assertEqual(self.lives['garik'].state()['peer_delivery_issue']['status'],'unknown')
        self.assertFalse(self.store.get('paused'));self.assertEqual(self.lives['arkady'].get('life_status')['status'],'running')
        before=len(self.connections['garik'].api.calls)
        for _ in range(4):self.scheduler.tick()
        self.assertEqual(before,len(self.connections['garik'].api.calls))
        self.assertEqual(self.store.db.execute('SELECT status FROM peer_jobs').fetchone()[0],'unknown')
        self.connections['garik'].api.fail_send=False
        self.root_post('garik','Own channel continues independently')
        self.lives['garik'].action(False);self.lives['garik'].action(True)
        calls=len(self.backends['garik'].inputs);self.now+=301
        for _ in range(4):self.scheduler.tick()
        self.assertEqual(len(self.backends['garik'].inputs),calls)
        self.assertEqual(self.lives['garik'].get('life_status')['status'],'running')
        self.assertEqual(self.store.db.execute("SELECT status FROM peer_jobs WHERE sender='garik'").fetchone()[0],'unknown')

    def test_comments_and_queues_are_isolated_even_with_equal_message_numbers(self):
        for actor in self.lives:
            post=self.root_post(actor);self.forward(actor,post);c=self.connections[actor]
            root=c.db.execute('SELECT root_id FROM forwards').fetchone()[0]
            with c.lock,c.db:c.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)',(c.api.group['id'],999,root,'Own reader','Reader','comment',42,self.now))
            self.lives[actor].import_comments();self.lives[actor].focus()
        a=self.lives['arkady'].get('channel_attention')['focus_ids'];g=self.lives['garik'].get('channel_attention')['focus_ids']
        self.assertEqual(len(a),1);self.assertEqual(len(g),1);self.assertFalse(set(a)&set(g))
        # Both have nonempty focus: Arkady's addressee must not be filtered by Garik's visit.
        for actor,source in [('arkady',a[0]),('garik',g[0])]:
            life=self.lives[actor]
            self.backends[actor].answers=[rich(decision='reply',text='Own reply '+actor,source_ids=[source],read_ids=[source])]
            cycle=life.channel_cycle();life.lab.session.step(cycle['id'])
            row=self.store.db.execute("SELECT * FROM world_events WHERE cycle_id=? AND stage='post'",(cycle['id'],)).fetchone()
            self.assertEqual(json.loads(row['value'])['audience_id'],source)
            life.deliver({**dict(row),'value':json.loads(row['value'])})
            self.assertEqual(life.connection.state()['outbox'][0]['chat_id'],life.connection.api.group['id'])

    def test_waiting_plan_survives_restart_and_resolution_uses_current_world(self):
        cid=self.plan('arkady',10);original=copy.deepcopy(self.store.cycle(cid)['value']);path=self.store.directory
        self.store.close();self.store=SharedWorldStore(path)
        self.assertEqual(self.store.cycle(cid)['status'],'waiting')
        self.assertEqual(self.store.cycle(cid)['value'],original)
        self.assertNotIn('outcome',self.store.cycle(cid)['value']['results'])
        self.assertEqual(len(self.store.monitor()['calls']),3)

    def test_common_physical_result_is_visible_without_the_other_actors_diary(self):
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE peer_positions SET place_id=?',('arkady_garage_yard',))
        cid=self.plan('arkady',2,'arkady_garage_yard');self.now+=120
        self.finish('arkady',cid,2,observation='SHARED-PHYSICAL-RESULT')
        view=json.dumps(self.store.actor_view('garik'),ensure_ascii=False)
        self.assertIn('SHARED-PHYSICAL-RESULT',view)
        self.assertNotIn('MY-EXPERIENCE',view)
        self.assertIn(cid+'-outcome',self.lives['garik'].social.evidence('garik'))

    def test_distant_character_cannot_be_added_to_an_observation_audience(self):
        life=self.lives['arkady'];cycle=life.lab.session.prepare('arkady',minutes=0,draw=1)
        observed=quiet();observed.update(visible_to=['arkady','garik'],entity_ids=['arkady_garage_yard'])
        self.backends['arkady'].answers=[observed]
        with self.assertRaises(ValueError):life.lab.session.step(cycle['id'])
        self.assertEqual(self.store.cycle(cycle['id'])['status'],'blocked')


    def test_meeting_can_be_replaced_and_needs_fresh_consent(self):
        first=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Встретимся во дворе.',place_id='arkady_garage_yard'))
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Приду.',invitation_id=first))
        old_promise=self.store.peer_context('garik')['own_commitments'][0]
        second=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Передумал: заходи в гараж.',place_id='arkady_garage',invitation_id=first))
        context=self.store.peer_context('garik')
        self.assertEqual([(i['id'],i['status']) for i in context['invitations']],[(second,'pending')])
        self.assertEqual(context['meeting_history'][-1]['status'],'superseded')
        self.assertFalse(context['own_commitments'])
        self.assertEqual(context['commitment_history'][0]['id'],old_promise['id'])
        with self.assertRaises(ValueError):
            self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Иду на старую.',invitation_id=first))

    def test_arrival_promise_is_linked_to_attempt_not_to_words_and_meeting_closes_on_departure(self):
        invite=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Заходи.',place_id='arkady_garage'))
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Иду.',invitation_id=invite))
        promise=self.store.peer_context('garik')['own_commitments'][0]
        self.assertEqual(promise['status'],'pending')
        cid=self.plan('garik',5,'arkady_garage','Дойду до гаража.')
        context=self.store.peer_context('garik')
        self.assertEqual(context['own_commitments'][0]['attempt_ids'],[cid])
        self.assertEqual(context['invitations'][0]['status'],'accepted')
        self.assertNotEqual(self.store.nearby('garik'),['arkady'])
        self.now+=300;self.finish('garik',cid,5)
        self.assertEqual(self.store.peer_context('garik')['invitations'][0]['status'],'met')
        self.assertEqual(self.store.peer_context('garik')['commitment_history'][0]['status'],'fulfilled')
        leave=self.plan('garik',2,'home','Вернусь домой.');self.now+=120;self.finish('garik',leave,2)
        self.assertEqual(self.store.peer_context('arkady')['invitations'],[])
        self.assertEqual(self.store.peer_context('arkady')['meeting_history'][0]['status'],'completed')

    def test_delivery_creates_private_promise_and_only_linked_outcome_can_fulfill_it(self):
        answer=peer_answer(decision='message',recipient_actor_id='garik',text='Проверю крепление.',
            commitment={'action':'Проверить крепление.','destination_id':None})
        self.backends['arkady'].answers.append(answer);self.scheduler.peer_turn('arkady')
        self.assertFalse(self.store.peer_context('arkady')['own_commitments'])
        job=self.store.db.execute("SELECT * FROM peer_jobs WHERE sender='arkady'").fetchone()
        self.scheduler.deliver_peer(job)
        promise=self.store.peer_context('arkady')['own_commitments'][0]
        self.assertFalse(self.store.peer_context('garik')['own_commitments'])
        self.assertNotIn(promise['id'],json.dumps(self.store.actor_view('garik')))
        cid=self.plan('arkady',2,'arkady_garage','Проверю крепление.',[promise['id']])
        self.assertEqual(self.store.peer_context('arkady')['own_commitments'][0]['status'],'in_progress')
        self.now+=120;self.finish('arkady',cid,2)
        self.assertEqual(self.store.peer_context('arkady')['own_commitments'][0]['status'],'needs_review')
        self.backends['arkady'].answers.append(peer_answer(commitment_updates=[{'id':promise['id'],'status':'fulfilled','source_id':cid+'-outcome'}]))
        self.scheduler.peer_turn('arkady')
        self.assertFalse(self.store.peer_context('arkady')['own_commitments'])
        self.assertEqual(self.store.peer_context('arkady')['commitment_history'][0]['status'],'fulfilled')

    def test_message_can_interrupt_elapsed_prefix_then_actor_really_goes_to_changed_meeting(self):
        original=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Приходи во двор.',place_id='arkady_garage_yard'))
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Позже зайду.',invitation_id=original))
        cid=self.plan('garik',60,'home','Почитаю дома, потом схожу к другу.')
        original_window=copy.deepcopy(self.store.cycle(cid)['value']['shared_window'])
        self.now+=600;self.scheduler.advance()
        changed=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Во дворе дождь, заходи прямо в гараж.',place_id='arkady_garage',invitation_id=original))
        source=self.store.peer_context('garik')['invitations'][0]['source_id']
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Ладно, книжку отложу и приду в гараж.',invitation_id=changed,
                                  activity='reconsider',source_ids=[source],read_ids=[source]))
        amended=self.store.cycle(cid)
        self.assertEqual(amended['value']['original_shared_window'],original_window)
        self.assertEqual(amended['value']['shared_window']['duration_minutes'],10)
        self.assertNotIn('outcome',amended['value']['results'])
        self.finish('garik',cid,10,observation='Отложил книгу после сообщения, пока дома.')
        self.assertEqual(self.store.peer_context('garik')['place_id'],'home')
        sent=self.backends['garik'].inputs[-3][2]
        self.assertIn('interrupted_action',sent)
        self.assertEqual(sent['shared_action_window']['duration_minutes'],10)
        self.assertNotIn(source,json.dumps(sent['shared_scene']))
        self.assertEqual(sent['shared_scene']['meetings'][0]['id'],original)
        self.assertLessEqual(self.lives['garik'].get('life_status')['next_wake'],self.now)
        arrival=self.plan('garik',5,'arkady_garage','Схожу к Аркадию в гараж.')
        self.now+=300;self.finish('garik',arrival,5)
        self.assertEqual(self.store.nearby('garik'),['arkady'])
        self.assertEqual(self.store.peer_context('garik')['invitations'][0]['status'],'met')
        self.assertFalse(self.store.peer_context('garik')['own_commitments'])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM world_calls WHERE cycle_id=? AND stage='outcome'",(cid,)).fetchone()[0],1)

    def test_actor_can_keep_action_and_reconsider_requires_received_cause(self):
        cid=self.plan('garik',60,'home');window=copy.deepcopy(self.store.cycle(cid)['value']['shared_window'])
        self.now+=600;self.scheduler.advance()
        source=self.store.relay('arkady','garik','message','Можешь зайти?')
        self.backends['garik'].answers.append(peer_answer(source_ids=[source],read_ids=[source]))
        self.scheduler.peer_turn('garik')
        self.assertEqual(self.store.cycle(cid)['value']['shared_window'],window)
        self.backends['garik'].answers.append(peer_answer(activity='reconsider'))
        with self.assertRaises(ValueError):self.scheduler.peer_turn('garik')
        self.assertEqual(self.store.cycle(cid)['value']['shared_window'],window)

    def test_agreement_history_obeys_frozen_time_and_survives_reopen(self):
        invite=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Заходи.',place_id='arkady_garage'))
        minute=self.store.get('minute')
        self.now+=60;self.scheduler.advance()
        self.job('garik',peer_answer(decision='accept',recipient_actor_id='arkady',text='Приду.',invitation_id=invite))
        self.assertEqual(self.store.peer_context('garik',minute)['invitations'][0]['status'],'pending')
        self.assertFalse(self.store.peer_context('garik',minute)['own_commitments'])
        self.now+=60;self.scheduler.advance()
        self.job('arkady',peer_answer(decision='cancel',recipient_actor_id='garik',text='Сегодня отмена.',invitation_id=invite))
        self.assertEqual(self.store.peer_context('garik',minute+1)['invitations'][0]['status'],'accepted')
        from persona_runtime.shared_agency import view
        self.assertEqual(view(self.store,'garik',minute+2)[0][0]['status'],'cancelled')
        import sqlite3
        backup_dir=self.root/'reopened';backup_dir.mkdir()
        backup=sqlite3.connect(backup_dir/'world.sqlite3')
        self.store.db.backup(backup);backup.close()
        reopened=SharedWorldStore(backup_dir)
        try:self.assertEqual(reopened.peer_context('garik')['meeting_history'][0]['status'],'cancelled')
        finally:reopened.close()

    def test_unknown_peer_delivery_does_not_activate_or_replay_a_promise(self):
        self.backends['garik'].answers.append(peer_answer(decision='message',recipient_actor_id='arkady',text='Проверю.',
            commitment={'action':'Проверить крепление.','destination_id':None}))
        self.scheduler.peer_turn('garik')
        with self.store.db:self.store.db.execute("UPDATE peer_jobs SET status='unknown' WHERE sender='garik'")
        row=self.store.db.execute("SELECT * FROM peer_jobs WHERE sender='garik'").fetchone()
        self.assertFalse(self.scheduler.deliver_peer(row))
        self.assertFalse(self.store.peer_context('garik')['own_commitments'])
        self.assertFalse(self.store.db.execute('SELECT 1 FROM peer_deliveries').fetchone())

    def test_cancel_before_pending_acceptance_delivery_does_not_reopen_meeting(self):
        invite=self.job('arkady',peer_answer(decision='invite',recipient_actor_id='garik',text='Заходи.',place_id='arkady_garage'))
        self.backends['garik'].answers.append(peer_answer(decision='accept',recipient_actor_id='arkady',text='Иду.',invitation_id=invite))
        self.scheduler.peer_turn('garik')
        self.job('arkady',peer_answer(decision='cancel',recipient_actor_id='garik',text='Планы поменялись.',invitation_id=invite))
        row=self.store.db.execute("SELECT * FROM peer_jobs WHERE sender='garik'").fetchone()
        self.scheduler.deliver_peer(row)
        self.assertEqual(self.store.db.execute('SELECT status FROM peer_jobs WHERE id=?',(row['id'],)).fetchone()[0],'obsolete')
        self.assertFalse(self.store.peer_context('garik')['own_commitments'])
        self.assertFalse(self.store.peer_context('arkady')['invitations'])

    def test_fulfillment_cannot_use_words_instead_of_linked_outcome(self):
        self.job('garik',peer_answer(decision='message',recipient_actor_id='arkady',text='Сделаю.',
            commitment={'action':'Проверить крепление.','destination_id':None}))
        context=self.store.peer_context('garik');promise=context['own_commitments'][0]
        source=context['events'][-1]['id']
        answer=peer_answer(commitment_updates=[{'id':promise['id'],'status':'fulfilled','source_id':source}])
        payload={'peer_context':context,'life':self.store.actor_view('garik'),'available_source_ids':[source]}
        with self.assertRaises(ValueError):self.scheduler.validate_peer('garik',answer,payload)
        self.assertEqual(self.store.peer_context('garik')['own_commitments'][0]['status'],'pending')

    def test_due_action_can_no_longer_be_reconsidered(self):
        cid=self.plan('garik',2,'home');self.now+=120;self.scheduler.advance()
        source=self.store.relay('arkady','garik','message','Заходи.')
        context=self.store.peer_context('garik')
        self.assertIsNone(context['reconsider_action'])
        payload={'peer_context':context,'life':self.store.actor_view('garik'),'available_source_ids':[source]}
        with self.assertRaises(ValueError):self.scheduler.validate_peer('garik',peer_answer(activity='reconsider',source_ids=[source]),payload)
        self.assertNotIn('interruption',self.store.cycle(cid)['value'])

    def test_received_trigger_is_not_played_retroactively_when_read_later(self):
        cid=self.plan('garik',60,'home','Почитаю дома до сообщения друга.')
        self.now+=180;self.scheduler.advance()
        source=self.store.relay('arkady','garik','message','Я освободился, заходи сейчас.')
        self.now+=120;self.scheduler.advance()
        self.backends['garik'].answers.append(peer_answer(activity='reconsider',source_ids=[source],read_ids=[source]))
        self.scheduler.peer_turn('garik')
        cycle=self.store.cycle(cid)
        payload=SharedTimeline(self.store).enrich('outcome',{'world':self.store.state()},cycle)
        self.assertEqual(payload['shared_action_window']['duration_minutes'],5)
        self.assertNotIn(source,json.dumps(payload['shared_scene']))
        own=SharedTimeline(self.store).enrich('experience',{},cycle)
        self.assertEqual(own['action_timing']['interruption']['at_minute'],self.store.get('minute'))

    def test_rejected_adapter_can_be_explicitly_released_without_action_or_replay(self):
        from test_world_intent import routed
        life=self.lives['arkady'];life.lab.session.lossless_intentions=True
        observed=quiet();observed.update(visible_to=['arkady'],entity_ids=['arkady_garage'])
        self.backends['arkady'].answers=[observed,'Пойду в гараж.',{**routed([('attempt','Пойдув гараж.')]),'duration_minutes':5,'destination_id':'arkady_garage'}]
        cid=life.lab.session.prepare('arkady',minutes=0,draw=1)['id']
        life.lab.session.step(cid);life.lab.session.step(cid)
        with self.assertRaises(ValueError):life.lab.session.step(cid)
        calls=[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls')]
        positions=self.store.state()['positions']
        with self.assertRaises(WorldConflict):self.store.release_rejected_choice(cid,'Explicit test review')
        for item in self.lives.values():item.action(False)
        self.store.release_rejected_choice(cid,'Explicit test review')
        self.store.release_rejected_choice(cid,'Idempotent repeated review')
        self.assertEqual(self.store.cycle(cid)['status'],'abandoned')
        self.assertNotIn('choice',self.store.cycle(cid)['value']['results'])
        self.assertEqual(calls,[tuple(r) for r in self.store.db.execute('SELECT * FROM world_calls')])
        self.assertEqual(positions,self.store.state()['positions'])
        self.assertEqual(len(self.store.get('rejected_choice_reviews')),1)


class PeerProtocolTests(unittest.TestCase):
    def test_source_handles_are_scoped_and_do_not_rewrite_spoken_text(self):
        from persona_runtime.shared_protocol import peer_io
        from persona_runtime.channel_social import map_sources
        payload={'available_source_ids':['actual-source'],'life':{'entities':[]},
                 'peer_context':{'unread_ids':['actual-source'],'available_actor_ids':['garik'],
                                 'nearby_actor_ids':[],'invitations':[],'events':[]}}
        schema,outgoing,decode=peer_io(payload)
        schema=next(b for b in schema['properties']['choice']['anyOf'] if b['properties']['decision']['enum']==['message'])
        self.assertEqual(outgoing['peer_context']['unread_ids'],['s1'])
        value=peer_answer(decision='message',recipient_actor_id='garik',text='s1',source_ids=['s1'],read_ids=['s1'])
        validate_shape(value,schema)
        self.assertEqual(map_sources(value,decode)['text'],'s1')
        self.assertEqual(map_sources(value,decode)['source_ids'],['actual-source'])
        value['source_ids']=['invented']
        with self.assertRaises(ValueError):validate_shape(value,schema)


if __name__=='__main__':unittest.main()
