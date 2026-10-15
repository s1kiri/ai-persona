"""Fresh memory -> voluntary text -> synthetic delivery -> real-style comment."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.channel_life import ChannelLife, AwakeGuard, fresh_start, split_text
from persona_runtime.world import WorldStore, WorldSession, WorldConflict
from persona_runtime.world_lab import Laboratory
from test_world import Backend
from test_telegram import Connection, API, TOKEN, CHANNEL, GROUP, forward, comment


class LifeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorldStore(self.root/'life')
        self.connection = Connection(self.root/'telegram', API)
        self.connection.configure(TOKEN)
        self.connection.bind(CHANNEL['id'])
        self.now = 1792064000.0
        fresh_start(self.store, self.connection, now=self.now)
        self.backend = Backend()
        self.session = WorldSession(self.store,self.backend,{'garik':'Synthetic persona'},'Speech',
                                    personal_life=True,active_world=True,autonomous_posts=True,direct_channel=True,
                                    continuous=True,external_channel=True)
        self.lab = Laboratory(self.store,self.session)
        self.life = ChannelLife(self.lab,self.connection,'Synthetic persona',now=lambda:self.now)
        self.life.action(True)

    def tearDown(self):
        self.store.close();self.connection.close();self.temp.cleanup()

    def bootstrap(self, decision='post',text='first thought'):
        self.backend.answers=[{'decision':decision,'text':text if decision!='keep' else '', 'source_ids':[]}]
        self.life.tick();self.life.tick()
        self.now+=60
        self.life.tick()

    def receive(self, *messages):
        offset=self.connection.get('offset',0)
        self.connection.api.batch=[{'update_id':offset+i,'message':m} for i,m in enumerate(messages)]
        self.connection.poll()

    def test_fresh_start_keeps_canon_but_has_no_previous_experience_or_test_comments(self):
        self.assertFalse(self.store.monitor()['notes'])
        self.assertFalse(self.store.state()['history'])
        self.assertFalse(self.store.state()['threads'])
        self.assertIn('шаурмичной',self.store.get('basis')['garik'])
        self.assertIn('уже создан',self.store.actor_view('garik')['channel_connection']['explanation'])
        self.assertNotIn(TOKEN,json.dumps(self.store.actor_view('garik')))
        with self.assertRaises(WorldConflict):fresh_start(self.store,self.connection,now=self.now)

    def test_first_call_is_own_channel_desire_not_external_world_or_forced_post(self):
        self.bootstrap('keep')
        self.assertEqual([i[0] for i in self.backend.inputs],['channel'])
        self.assertIn('starting_desire',self.backend.inputs[0][2]['life'])
        self.assertFalse(self.connection.state()['outbox'])
        self.assertFalse(self.store.monitor()['posts'])
        self.assertEqual(self.life.state()['status'],'running')
        self.now+=7200
        self.life.tick()
        self.assertEqual(self.store.monitor()['cycles'][0]['phase'],'horizon')
        self.assertFalse(self.connection.state()['outbox'])

    def test_exact_text_receipt_and_fulfilled_start_no_repeat(self):
        self.bootstrap(text='блять\n\n  вот так')
        post=self.store.monitor()['posts'][0]
        self.assertEqual(post['value']['text'],'блять\n\n  вот так')
        self.assertFalse(post['value']['draft'])
        self.assertEqual(post['value']['delivery']['status'],'sent')
        self.assertTrue(self.store.get('channel_start')['fulfilled'])
        count=len(self.connection.api.calls)
        self.life.deliver(post)
        self.assertEqual(len(self.connection.api.calls),count)

    def test_only_comments_on_this_lifes_posts_enter_memory_and_can_get_reply(self):
        self.bootstrap()
        tg_post=self.connection.state()['outbox'][0]['message_id']
        self.receive(forward(50,tg_post),comment(),forward(70,999),comment(71,forward(70,999)))
        self.assertEqual(self.life.import_comments(),1)
        self.assertEqual(self.life.import_comments(),0)
        audience=[e for e in self.store.channel_context('garik') if e['stage']=='audience'][0]
        self.assertFalse(audience['value']['simulated'])
        self.assertEqual(audience['value']['post_id'],self.store.monitor()['posts'][0]['id'])
        self.backend.answers=[{'decision':'reply','text':'да норм','source_ids':[audience['id']]}]
        self.life.tick();self.now+=60;self.life.tick();self.life.tick()
        sends=[call for call in self.connection.api.calls if call[0]=='sendMessage']
        self.assertEqual(len(sends),2)
        self.assertEqual(sends[-1][1]['chat_id'],GROUP['id'])
        self.assertEqual(sends[-1][1]['reply_parameters']['message_id'],51)

    def test_pause_prevents_delivery_and_resume_does_not_repeat_model_decision(self):
        self.backend.answers=[{'decision':'post','text':'paused text','source_ids':[]}]
        self.life.tick();self.life.tick()
        self.life.action(False);self.now+=120
        self.life.tick()
        self.assertFalse(self.connection.state()['outbox'])
        self.life.action(True);self.life.tick()
        self.assertEqual(len(self.backend.inputs),1)
        self.assertEqual(self.connection.state()['outbox'][0]['status'],'sent')

    def test_future_action_time_waits_before_next_model_call_or_delivery(self):
        self.life.tick()
        with self.store.lock,self.store.db:
            self.store.put('minute',self.life.minute_now()+45)
            row=self.store.monitor()['cycles'][0]
            value=row['value'];value['bootstrap']=False
            self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(value),row['id']))
        self.life.tick()
        self.assertFalse(self.backend.inputs)
        self.assertEqual(self.life.current_phase,'living')

    def test_send_failure_blocks_life_and_resume_without_duplicate_or_lost_text(self):
        self.connection.api.fail_send=True
        self.bootstrap()
        self.assertEqual(self.life.state()['status'],'error')
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'],'first thought')
        with self.assertRaises(Exception):self.life.action(True)
        count=len(self.connection.api.calls);self.life.tick()
        self.assertEqual(count,len(self.connection.api.calls))

    def test_owner_confirmed_absence_uses_new_attempt_and_preserves_original_unknown_receipt(self):
        self.connection.api.fail_send=True;self.bootstrap()
        post=self.store.monitor()['posts'][0]
        self.life.confirm_absent(post['id'])
        self.connection.api.fail_send=False;self.life.action(True);self.life.tick()
        outbox=self.connection.state()['outbox']
        self.assertEqual({row['status'] for row in outbox},{'sent','unknown'})
        self.assertEqual(len(self.backend.inputs),1)
        self.assertTrue(self.store.get('delivery_repairs'))
        self.assertTrue(any(':review1:' in row['key'] for row in outbox))

    def test_cadence_controls_are_not_publication_schedule_and_roundtrip(self):
        self.life.configure({'wake_minutes':30,'photos':False})
        self.assertEqual(self.life.state()['controls']['wake_minutes'],30)
        self.assertNotIn('post_every',json.dumps(self.life.state()))
        self.life.configure({'sharing_appetite':80})
        self.assertEqual(self.life.state()['controls']['sharing_appetite'],80)
        for invalid in (-1,101,True):
            with self.assertRaises(ValueError):self.life.configure({'sharing_appetite':invalid})
        for wrong in ({'wake_minutes':0,'photos':False},{'wake_minutes':30,'photos':'yes'}):
            with self.assertRaises(ValueError):self.life.configure(wrong)

    def test_episode_pace_guides_choice_without_clamping_actual_duration(self):
        from test_world import choice,outcome
        from test_world_personal import quiet
        self.life.configure({'episode_minutes':10,'wake_minutes':5})
        self.store.set_settings({**self.store.get('settings'),'events_per_hour':0},0)
        cycle=self.session.prepare(minutes=0,draw=1)
        minute=self.store.get('minute')
        self.backend.answers=[quiet(),'Rest.',choice(action='Rest.'),outcome(elapsed_minutes=45)]
        for _ in range(4):self.session.step(cycle['id'])
        calls={stage:payload for stage,_,payload in self.backend.inputs}
        self.assertEqual(calls['impulse']['episode_pace']['target_minutes'],10)
        self.assertTrue(calls['outcome']['episode_pace']['soft_target'])
        self.assertEqual(self.store.get('minute'),minute+45)
        self.assertNotIn('episode_pace',calls['world'])
        for wrong in (0,95,True,'15'):
            with self.assertRaises(ValueError):self.life.configure({'episode_minutes':wrong})

    def test_optional_photo_is_sent_through_bot_and_only_delivered_image_becomes_reference(self):
        self.backend.answers=[{'decision':'post','text':'look at this','source_ids':[]}]
        self.life.tick();self.life.tick();self.now+=60
        post=self.store.monitor()['posts'][0]
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',
                ('synthetic-outcome',post['cycle_id'],'garik','outcome',self.store.get('minute'),
                 json.dumps({'observation':'At home, an actual visible scene.','visible_to':['garik']})))
        image_path=self.root/'synthetic-photo.png';image_path.write_bytes(b'synthetic image')
        image={'image_path':str(image_path),'sha256':hashlib.sha256(image_path.read_bytes()).hexdigest(),'subject_ids':['home']}
        class Case:
            def __init__(self,*args,**kwargs):pass
            def run(self,context):
                return {'status':'passed','choice':{'framing':'new view','caption':''},'image':image}
        self.life.renderer=object();self.life.photo_factory=Case
        self.life.tick()
        self.assertEqual(self.connection.api.calls[-1][0],'sendPhoto')
        self.assertEqual(self.connection.api.calls[-1][1]['caption'],'look at this')
        self.assertEqual(self.store.monitor()['posts'][0]['value']['photo']['sha256'],image['sha256'])
        from persona_runtime.world_media import MediaReferenceLibrary
        library=MediaReferenceLibrary(self.store.directory/'visual-memory')
        try:self.assertEqual(library.select('garik',['home'])[0]['source'],'channel')
        finally:library.close()

    def test_long_post_split_preserves_all_characters(self):
        text='  literal\n\n'+('word '*2200)+'\nEND'
        chunks=split_text(text)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(c)<=3500 for c in chunks))

    def test_awake_guard_tied_to_process_released_on_pause(self):
        class Child:
            pid=765432
            ended=False
            def poll(self):return 0 if self.ended else None
            def terminate(self):self.ended=True
            def wait(self,timeout):return 0
        child=Child()
        with patch('persona_runtime.channel_life.sys.platform','darwin'),patch('persona_runtime.channel_life.subprocess.Popen',return_value=child) as launch:
            guard=AwakeGuard(self.life,hours=1);guard.tick()
            args=launch.call_args.args[0]
            self.assertIn('-i',args);self.assertIn('-w',args);self.assertIn('-t',args)
            self.assertTrue(self.store.get('awake_status')['active'])
            self.life.action(False);guard.tick()
            self.assertTrue(child.ended)
            self.assertFalse(self.store.get('awake_status')['active'])

    def test_process_awake_guard_has_no_timeout_and_holds_on_pause_until_exit(self):
        child=unittest.mock.Mock();child.pid=765433;child.poll.return_value=None
        with patch('persona_runtime.channel_life.sys.platform','darwin'),patch('persona_runtime.channel_life.subprocess.Popen',return_value=child) as launch:
            guard=AwakeGuard(self.life,hours=0);guard.tick()
            args=launch.call_args.args[0]
            self.assertIn('-i',args);self.assertIn('-s',args);self.assertIn('-w',args);self.assertNotIn('-t',args)
            self.life.action(False);guard.tick()
            self.assertIsNone(self.store.get('awake_status')['until'])
            child.terminate.assert_not_called()
            guard.close();child.terminate.assert_called_once()


if __name__=='__main__':unittest.main()
