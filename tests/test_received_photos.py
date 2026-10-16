"""Actual image input routing with disposable stores and synthetic Telegram only."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.telegram import BotAPI,TelegramError
from persona_runtime.profile_editor import private_package
from persona_runtime.character_profile import CharacterProfile
from persona_runtime.world_photo import render_request
import test_shared_life as pair
import test_world_backend as transport
from test_channel_social import rich
from test_telegram import TOKEN

PNG=b'\x89PNG\r\n\x1a\nSYNTHETIC-PIXELS'

class ReceivedPhotoTests(unittest.TestCase):
    def setUp(self):
        self.f=pair.SharedLifeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_captionless_photos_reach_only_owning_actors_focus(self):
        f=self.f
        for actor in ('arkady','garik'):
            post=f.root_post(actor);f.forward(actor,post);c=f.connections[actor]
            root=c.db.execute('SELECT root_id FROM forwards').fetchone()[0]
            downloads=[]
            def download(file_id):downloads.append(file_id);return PNG,'.png'
            c.api.download_photo=download
            message={'message_id':501,'chat':c.api.group,'reply_to_message':{'message_id':root},
                'from':{'id':42,'first_name':'Reader'},'photo':[{'file_id':'small','width':10,'height':10},
                {'file_id':'large','width':100,'height':100}]}
            with c.lock,c.db:c.ingest({'message':message})
            life=f.lives[actor];self.assertEqual(life.import_comments(),1);self.assertEqual(life.import_comments(),0)
            self.assertEqual(downloads,['large'])
            life.focus();payload=life.social.enrich({'life':f.store.actor_view(actor)},actor)
            previews=payload['_comment_previews'];self.assertEqual(len(previews),1)
            self.assertIn('/'+actor+'/',previews[0]['image_path']);self.assertEqual(previews[0]['sha256'],hashlib.sha256(PNG).hexdigest())
            self.assertEqual(payload['attention']['focus_comments'][0]['value']['comments'][0]['text'],'')
            other='garik' if actor=='arkady' else 'arkady'
            other_view=f.store.actor_view(other)
            self.assertNotIn(previews[0]['image_path'],json.dumps(other_view))

    def test_failed_photo_read_is_recorded_without_blocking_life_or_sending(self):
        f=self.f;post=f.root_post('garik');f.forward('garik',post);c=f.connections['garik']
        root=c.db.execute('SELECT root_id FROM forwards').fetchone()[0]
        def failed(_):raise RuntimeError('Secret '+TOKEN)
        c.api.download_photo=failed
        with c.lock,c.db:c.ingest({'message':{'message_id':501,'chat':c.api.group,'message_thread_id':root,
            'from':{'id':42},'photo':[{'file_id':'file'}]}})
        before=len(c.api.calls);self.assertEqual(f.lives['garik'].import_comments(),1)
        value=c.comment_photo(c.api.group['id'],501);self.assertEqual(value['status'],'unavailable')
        self.assertNotIn(TOKEN,json.dumps(value));self.assertEqual(len(c.api.calls),before)
        self.assertEqual(f.lives['garik'].get('life_status')['status'],'running')

    def test_camera_profile_persists_and_does_not_change_other_actor(self):
        f=self.f;old=f.profiles['arkady'];style='2016–2018 smartphone, soft detail, low-light noise; no vintage filter.'
        new=private_package(f.root/'new-profile',old,old.persona,'gpt-6-luna','medium',photo_style=style)
        again=private_package(f.root/'next-profile',new,new.persona,'gpt-6-luna','medium')
        self.assertEqual(again.photo_style,style);self.assertNotEqual(old.digest,new.digest)
        f.lives['arkady'].lab.profile=new
        for actor in ('arkady','garik'):
            life=f.lives[actor];payload=life.social.enrich({'life':f.store.actor_view(actor)},actor)
            self.assertEqual(payload['photo_scene'].get('photo_style',''),style if actor=='arkady' else '')
        context={'actor_id':'arkady','subjects':[],'sources':[],'photo_style':style}
        context['subjects']=[{'id':'table','name':'Table'}];context['sources']=[{'id':'seen','observation':'On table'}]
        choice={'decision':'show','reason':'Show','subject_ids':['table'],'source_ids':['seen'],'framing':'From above','caption':''}
        self.assertIn(style,render_request(choice,context)['prompt'])

class NativeCommentPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.backend=transport.WorldBackend(self.temp.name);self.backend._start=lambda:0
        self.backend.workspace=Path(self.temp.name);self.backend.overrides={}
    def test_native_input_contains_pixels_bound_to_comment_and_rejects_tampering(self):
        path=Path(self.temp.name)/'photo.png';path.write_bytes(PNG)
        self.backend.client=transport.Client(rich())
        payload={'life':{'entities':[]},'_source_ids':['comment'],
            'attention':{'focus_ids':['comment'],'focus_comments':[{'id':'comment'}]},
            '_comment_previews':[{'audience_id':'comment','image_path':str(path),'sha256':hashlib.sha256(PNG).hexdigest()}]}
        self.backend.generate('live_channel','Synthetic role',payload)
        start=next(p for m,p in self.backend.client.calls if m=='turn/start')
        self.assertEqual([i['path'] for i in start['input'] if i['type']=='localImage'],[str(path)])
        self.assertIn('reader comment s1',start['input'][1]['text'])
        path.write_bytes(b'CHANGED');self.backend.client=transport.Client(rich())
        with self.assertRaises(ValueError):self.backend.generate('live_channel','Synthetic role',payload)
        self.assertFalse(any(m=='turn/start' for m,_ in self.backend.client.calls))

class DownloadTests(unittest.TestCase):
    def test_official_file_route_and_redacted_download_failure(self):
        class Opener:
            def __init__(self,fail=False):self.calls=[];self.fail=fail
            def open(self,request,**kwargs):
                self.calls.append(request.full_url)
                if len(self.calls)==1:return io.BytesIO(json.dumps({'ok':True,'result':{'file_path':'photos/file_1.png'}}).encode())
                if self.fail:raise RuntimeError(request.full_url)
                return io.BytesIO(PNG)
        opener=Opener();data,suffix=BotAPI(TOKEN,opener).download_photo('file')
        self.assertEqual((data,suffix),(PNG,'.png'));self.assertTrue(opener.calls[1].startswith('https://api.telegram.org/file/bot'))
        with self.assertRaises(TelegramError) as caught:BotAPI(TOKEN,Opener(True)).download_photo('file')
        self.assertNotIn(TOKEN,str(caught.exception))

if __name__=='__main__':unittest.main()
