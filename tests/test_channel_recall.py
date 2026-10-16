"""Historical recall, privacy and one bounded pre-delivery self-review."""
import json
import unittest

from persona_runtime.channel_recall import ChannelRecall
import test_channel_social as fixtures


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SocialTests();self.fixture.setUp()
        self.store=self.fixture.store
        self.calls=[]
        def encoder(texts,query):
            self.calls.append((texts,query))
            return {'vectors':[[1.,0.] for _ in texts],'query':[1.,0.]}
        self.recall=ChannelRecall(self.store,encoder)

    def tearDown(self):self.fixture.tearDown()

    def root(self,text):
        self.fixture.root()
        post=self.store.monitor()['posts'][0]
        with self.store.lock,self.store.db:
            value={**post['value'],'text':text}
            self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value),post['id']))

    def test_old_semantic_match_survives_window_restart_and_has_sources(self):
        self.root('bass colour')
        self.fixture.life.social.recall=self.recall
        with self.store.lock,self.store.db:self.store.put('minute',3000)
        matches=self.recall.search('garik','Unrelated spelling, same semantic meaning.',publications_only=True)
        self.assertEqual(matches[0]['text'],'bass colour')
        self.assertEqual(matches[0]['stage'],'post')
        self.assertFalse(self.recall.recent('garik'))
        self.assertEqual(self.recall.report['mode'],'hybrid')
        restarted=ChannelRecall(self.store,self.recall.encoder)
        restarted.search('garik','another meaning',publications_only=True)
        self.assertEqual(self.calls[-1][0],[])  # persisted document vectors, query still encoded

    def test_retracted_foreign_unsent_and_backstage_do_not_enter_recall(self):
        self.root('visible')
        post=self.store.monitor()['posts'][0]
        with self.store.lock,self.store.db:
            value={**post['value'],'text':'RETRACTED','retracted':{'reason':'fixture'}}
            self.store.db.execute('UPDATE world_events SET value=? WHERE id=?',(json.dumps(value),post['id']))
            for eid,actor,stage,value in (
                ('foreign','other','experience',{'note':'FOREIGN'}),
                ('hidden','other','world',{'observation':'HIDDEN','visible_to':['other'],'world_note':'SECRET'}),
                ('observed','garik','outcome',{'observation':'KNOWN','world_note':'SECRET'}),
                ('unsent','garik','post',{'text':'UNSENT'})):
                cid='fixture-'+eid
                self.store.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',(cid,'garik','complete','done',0,'{}',0))
                self.store.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',(eid,cid,actor,stage,50,json.dumps(value)))
        documents=json.dumps(self.recall.documents('garik'))
        self.assertIn('KNOWN',documents)
        for secret in ('RETRACTED','FOREIGN','HIDDEN','SECRET','UNSENT'):self.assertNotIn(secret,documents)

    def test_virtual_cutoff_excludes_future_results_from_recall(self):
        self.root('Future publication')
        post=self.store.monitor()['posts'][0]
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE world_events SET minute=2000 WHERE id=?',(post['id'],))
        self.assertFalse(self.recall.search('garik','Future publication',at_minute=1000))
        self.assertFalse(self.recall.recent('garik',at_minute=1000))
        self.assertTrue(self.recall.search('garik','Future publication',at_minute=2000))

    def test_encoder_failure_is_explicit_lexical_fallback(self):
        self.root('studio rhythm')
        def failed(*args):raise RuntimeError('Unavailable')
        recall=ChannelRecall(self.store,failed)
        self.assertEqual(recall.search('garik','studio')[0]['text'],'studio rhythm')
        self.assertEqual(recall.report['mode'],'lexical_fallback')
        self.assertEqual(recall.report['error'],'local_encoder_unavailable')

    def test_one_recorded_review_can_keep_without_publication_or_side_effects(self):
        self.root('old studio rhythm')
        self.fixture.life.social.recall=self.recall
        backend=self.fixture.fixture.backend
        backend.answers=[fixtures.rich(decision='post',text='same studio rhythm'),fixtures.rich()]
        self.fixture.life.focus(own_channel=True)
        cid=self.fixture.life.channel_cycle()['id']
        self.fixture.fixture.session.step(cid)
        cycle=self.store.cycle(cid)
        self.assertEqual(cycle['status'],'complete')
        self.assertEqual(cycle['value']['results']['share']['decision'],'keep')
        self.assertEqual(cycle['value']['publication_review']['draft']['text'],'same studio rhythm')
        calls=list(self.store.db.execute('SELECT stage,status,usage FROM world_calls WHERE cycle_id=?',(cid,)))
        self.assertEqual({r['stage'] for r in calls},{'share','publication_review'})
        self.assertTrue(all(r['status']=='completed' for r in calls))
        self.assertTrue(all(json.loads(r['usage'])['output_tokens']==5 for r in calls))
        self.assertFalse(any(p['cycle_id']==cid for p in self.store.monitor()['posts']))
        self.assertEqual(len(backend.inputs),3)  # root, draft, one self-review

    def test_failed_review_blocks_without_replaying_or_losing_first_usage(self):
        self.root('old post')
        self.fixture.life.social.recall=self.recall
        backend=self.fixture.fixture.backend
        backend.answers=[fixtures.rich(decision='post',text='same topic'),RuntimeError('fixture failure')]
        self.fixture.life.focus(own_channel=True)
        cid=self.fixture.life.channel_cycle()['id']
        with self.assertRaises(RuntimeError):self.fixture.fixture.session.step(cid)
        self.assertEqual(self.store.cycle(cid)['status'],'blocked')
        self.assertFalse(any(p['cycle_id']==cid for p in self.store.monitor()['posts']))
        with self.assertRaises(Exception):self.fixture.fixture.session.step(cid)
        self.assertEqual(len(backend.inputs),3)

    def test_similarity_does_not_forbid_a_new_continuation_or_rewrite_final_text(self):
        self.root('first attempt')
        self.fixture.life.social.recall=self.recall
        backend=self.fixture.fixture.backend
        final='second attempt\n\nthis time it worked)'
        backend.answers=[fixtures.rich(decision='post',text='another attempt'),fixtures.rich(decision='post',text=final)]
        self.fixture.life.focus(own_channel=True)
        cid=self.fixture.life.channel_cycle()['id']
        self.fixture.fixture.session.step(cid)
        self.assertEqual(self.store.cycle(cid)['value']['results']['post'],final)
        self.assertEqual(self.store.monitor()['posts'][0]['value']['text'],final)
        self.assertEqual(self.store.cycle(cid)['value']['publication_review']['answer']['text'],final)


if __name__=='__main__':unittest.main()
