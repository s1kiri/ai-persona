"""A role incident is retained for audit but never sent or taught as history."""
import json
import unittest

from persona_runtime.role_boundary import check_character_speech,RoleBoundaryError
import test_channel_social as fixtures


class BoundaryTests(unittest.TestCase):
    def test_explicit_self_disclosures_blocked_but_ai_topics_allowed(self):
        for text in ['так я и есть ИИ-персонаж. Гарик. жизнь тут вымышленная',
                     'Я — языковая модель.','Как ИИ-персонаж я не ем.',
                     'Мои ответы генерирует нейросеть.','Моя жизнь — симуляция.',
                     'Гарик — вымышленный персонаж.',"I'm an AI character.",'I am a language model.', 'Я ChatGPT.']:
            with self.subTest(text=text),self.assertRaises(RoleBoundaryError):check_character_speech(text,'Гарик')
        for text in ['почитал про ИИ, любопытно.','ты опять про ИИ спрашиваешь?',
                     'я чиню электронику по ночам.','он написал: «я ИИ-персонаж». ну и что?',
                     'я как робот второй час хожу туда-сюда.','блять']:
            with self.subTest(text=text):check_character_speech(text,'Гарик')


class PublicationTests(unittest.TestCase):
    def setUp(self):self.social=fixtures.SocialTests();self.social.setUp()
    def tearDown(self):self.social.tearDown()

    def test_bad_generation_retains_usage_without_post_or_delivery(self):
        f=self.social
        f.backend.answers=[fixtures.rich(decision='post',text='Я ИИ-персонаж.')]
        f.life.tick();f.life.tick()
        self.assertEqual(f.life.state()['status'],'error')
        self.assertEqual(f.connection.state()['outbox'],[])
        self.assertEqual(f.store.monitor()['posts'],[])
        cycle=f.store.monitor()['cycles'][0]
        self.assertEqual(cycle['value']['rejected']['error_type'],'RoleBoundaryError')
        self.assertEqual(cycle['value']['rejected']['answer']['text'],'Я ИИ-персонаж.')
        self.assertEqual(f.store.monitor()['calls'][0]['usage']['input_tokens'],20)

    def test_legacy_prepared_text_is_blocked_before_job_or_photo(self):
        f=self.social
        post={'actor_id':'garik','id':'old-prepared','value':{'text':'я языковая модель'}}
        with self.assertRaises(RoleBoundaryError):f.life.deliver(post)
        self.assertFalse(f.store.db.execute('SELECT 1 FROM channel_jobs').fetchone())
        self.assertFalse(f.connection.state()['outbox'])

    def test_retracted_post_is_absent_from_model_context_and_sources(self):
        f=self.social;f.root();post=f.store.monitor()['posts'][0]
        with f.store.lock,f.store.db:
            value=post['value'];value['retracted']={'receipt':{'status':'deleted'}}
            f.store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value),post['id']))
        self.assertNotIn(post['id'],f.store.public_evidence_ids('garik'))
        self.assertNotIn(post['id'],f.life.social.evidence())
        self.assertFalse(f.store.channel_context('garik'))
        payload=f.life.social.enrich({'life':f.store.actor_view('garik')},'garik')
        self.assertNotIn(post['value']['text'],payload['previous_posts'])
