"""Gallery ownership, native previews and real comment delivery on synthetic data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.channel_media import Gallery
from persona_runtime.world import WorldStore
from persona_runtime.world_backend import WorldBackend
from test_world_image_backend import PNG
from test_world_backend import Client
import test_channel_social as social_fixtures
from test_channel_social import rich
from test_telegram import GROUP, comment
from test_photo_case import Renderer


def asset(root, subjects=('home',), pixels=PNG):
    path=Path(root)/('source-'+hashlib.sha256(pixels).hexdigest()+'.png')
    path.write_bytes(pixels)
    return {'image_path':str(path),'sha256':hashlib.sha256(pixels).hexdigest(),'subject_ids':list(subjects)}


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=WorldStore(Path(self.temp.name)/'world')
        self.gallery=Gallery(self.store)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_owned_bounded_search_and_truthful_delivery_history(self):
        first=self.gallery.register(asset(self.store.directory),'garik','old apple photo',minute=1)
        for n in range(26):
            self.gallery.register(asset(self.store.directory,pixels=PNG+str(n).encode()),'garik','another photo',minute=n+2)
        self.assertEqual(self.gallery.catalog('garik',['home'],'apple')[0]['id'],first)
        self.assertNotIn('image_path',json.dumps(self.gallery.catalog('garik',['home'])))
        self.assertEqual(self.gallery.catalog('other',['home']),[])
        self.assertEqual(self.gallery.catalog('garik',[]),[])
        with self.assertRaises(ValueError):self.gallery.get('other',first,['home'])
        for n in range(8):self.gallery.delivered('garik',first,'post',{'chat_id':1,'message_id':n})
        self.assertEqual(len(self.gallery.catalog('garik',['home'],'apple')[0]['shared']),4)

    def test_foreign_assets_hidden_subjects_and_tampering_rejected(self):
        with self.assertRaises(ValueError):self.gallery.register(asset(self.temp.name),'garik','view')
        item=self.gallery.register(asset(self.store.directory,('home','secret')),'garik','view')
        with self.assertRaises(ValueError):self.gallery.get('garik',item,['home'])
        owned=self.gallery.get('garik',item,['home','secret'])
        Path(owned['image_path']).write_bytes(b'damaged')
        with self.assertRaises(ValueError):self.gallery.get('garik',item,['home','secret'])

    def test_prepared_generation_resumes_same_job_but_cannot_be_a_new_photo(self):
        image=asset(self.store.directory)
        first=self.gallery.register(image,'garik','first view',new_job='post-1')
        self.assertEqual(self.gallery.register(image,'garik','changed metadata',new_job='post-1'),first)
        self.assertEqual(self.gallery.get('garik',first,['home'])['view'],'first view')
        with self.assertRaisesRegex(ValueError,'already exists'):
            self.gallery.register(image,'garik','new view',new_job='post-2')
        self.assertEqual(self.gallery.catalog('garik',['home'])[0]['shared'],[])


class PreviewTests(unittest.TestCase):
    def test_actual_pixels_with_id_label_and_no_private_path_in_json(self):
        with tempfile.TemporaryDirectory() as directory:
            image=asset(directory)
            backend=WorldBackend(directory)
            backend._start=lambda:0;backend.workspace=Path(directory);backend.overrides={}
            backend.client=Client(rich())
            payload={'gallery':[{'id':'photo-1'}], '_gallery_previews':[{'id':'photo-1',**image}]}
            backend.generate('live_channel','Synthetic persona',payload)
            turn=next(p for m,p in backend.client.calls if m=='turn/start')
            self.assertNotIn(directory,turn['input'][0]['text'])
            self.assertEqual(turn['input'][1]['text'],'Gallery photo photo-1')
            self.assertEqual(turn['input'][2],{'type':'localImage','path':image['image_path']})
            backend.client=Client(rich());payload['_gallery_previews'][0]['sha256']='wrong'
            with self.assertRaises(ValueError):backend.generate('live_channel','Synthetic persona',payload)
            self.assertFalse(any(m=='turn/start' for m,p in backend.client.calls))


class PhotoReplyTests(unittest.TestCase):
    def setUp(self):
        self.social=social_fixtures.SocialTests();self.social.setUp()
        self.store,self.life,self.backend,self.connection=(self.social.store,self.social.life,self.social.backend,self.social.connection)
        image=asset(self.store.directory)
        self.photo=self.life.social.gallery.register(image,'garik','ordinary view')
        self.attachment={'mode':'existing','gallery_id':self.photo,'subject_ids':[],'source_ids':[],'framing':''}

    def tearDown(self):self.social.tearDown()

    def reply(self,text='',attachment=True):
        ids=self.social.prepare_comments(comment())
        self.backend.answers=[rich(decision='reply',text=text,source_ids=ids,read_ids=ids,
                                   attachment=self.attachment if attachment else None)]
        self.life.tick();self.life.tick()

    def test_photo_only_reply_is_real_sendphoto_to_comment_without_extra_model(self):
        self.reply()
        method,args,kwargs=self.connection.api.calls[-1]
        self.assertEqual(method,'sendPhoto');self.assertEqual(args['caption'],'')
        self.assertEqual(args['chat_id'],GROUP['id'])
        self.assertEqual(args['reply_parameters'],{'message_id':51,'allow_sending_without_reply':False})
        self.assertEqual(kwargs['photo'],PNG)
        self.assertEqual([s for s,_,_ in self.backend.inputs],['live_channel','live_channel'])
        gallery=self.life.social.gallery.catalog('garik',['home'])
        self.assertEqual(gallery[0]['shared'][0]['chat_id'],GROUP['id'])
        self.assertEqual(gallery[0]['view'],'ordinary view')
        self.life.deliver(self.store.monitor()['posts'][0])
        self.assertEqual(sum(c[0]=='sendPhoto' for c in self.connection.api.calls),1)

    def test_long_caption_keeps_both_text_and_photo_under_same_comment(self):
        self.reply('x'*1500)
        sends=[c for c in self.connection.api.calls if c[0] in ('sendMessage','sendPhoto')][-2:]
        self.assertEqual([c[0] for c in sends],['sendMessage','sendPhoto'])
        for _,args,_ in sends:
            self.assertEqual(args['chat_id'],GROUP['id']);self.assertEqual(args['reply_parameters']['message_id'],51)

    def test_no_photo_remains_a_valid_voluntary_choice(self):
        self.reply('no photo',attachment=False)
        self.assertEqual(self.connection.api.calls[-1][0],'sendMessage')
        self.assertFalse(any(c[0]=='sendPhoto' for c in self.connection.api.calls))

    def test_disabled_photos_rejects_attachment_without_external_send(self):
        with self.store.lock,self.store.db:
            controls=self.store.get('life_controls');controls['photos']=False;self.store.put('life_controls',controls)
        self.reply()
        self.assertEqual(self.life.state()['status'],'error')
        self.assertFalse(any(c[0]=='sendPhoto' for c in self.connection.api.calls))

    def test_new_photo_uses_selected_choice_once_even_after_pause(self):
        self.backend.answers=[rich(decision='post',text='my photo')]
        self.life.tick();self.life.tick()
        post=self.store.monitor()['posts'][0]
        attachment={'mode':'new','gallery_id':'','subject_ids':['home'],'source_ids':['moment'],'framing':'different view'}
        with self.store.lock,self.store.db:
            value=self.store.cycle(post['cycle_id'])['value'];value['social_output']['attachment']=attachment
            self.store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(value),post['cycle_id']))
        context={'actor_id':'garik','subjects':[{'id':'home','kind':'place','name':'home','description':'home','condition':'home'}],
                 'sources':[{'id':'moment','minute':1,'observation':'at home'}]}
        renderer=Renderer(pixels=PNG+b'new-frame')
        original=renderer.render
        def render(*args,**kwargs):
            result={**original(*args,**kwargs),'subject_ids':['home']}
            self.life.action(False)
            return result
        renderer.render=render;self.life.renderer=renderer
        before=len(self.backend.inputs)
        with patch('persona_runtime.world_photo.photo_context',return_value=context):
            self.life.deliver(post)
            self.assertFalse(any(c[0]=='sendPhoto' for c in self.connection.api.calls))
            self.assertFalse(self.store.db.execute('SELECT 1 FROM channel_jobs WHERE post_id=?',(post['id'],)).fetchone())
            self.life.action(True);self.life.deliver(post)
        self.assertEqual(len(renderer.calls),1)
        self.assertEqual(len(self.backend.inputs),before)
        self.assertEqual(sum(c[0]=='sendPhoto' for c in self.connection.api.calls),1)
        self.assertEqual(renderer.calls[0][0]['source_ids'],['moment'])

    def test_backfill_imports_only_delivered_photos_and_reconciles_receipts(self):
        self.reply()
        with self.store.lock,self.store.db:
            self.store.db.execute('DELETE FROM gallery_deliveries')
        self.life.social.gallery.backfill();self.life.social.gallery.backfill()
        photos=self.life.social.gallery.catalog('garik',['home'])
        self.assertEqual(len(photos),1);self.assertEqual(len(photos[0]['shared']),1)
        self.assertEqual(photos[0]['view'],'ordinary view')
