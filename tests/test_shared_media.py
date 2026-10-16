"""Cross-character visual evidence: public receipts and actual co-presence only."""
import hashlib
import json
import unittest
from pathlib import Path

from persona_runtime.channel_media import Gallery
from persona_runtime.shared_media import SharedPhotoReferences,published_photos,enrich_public_photos
from persona_runtime.world_backend import WorldBackend
from test_shared_life import SharedLifeTests


class SharedMediaTests(unittest.TestCase):
    def setUp(self):
        f=SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;self.store=f.store;self.gallery=Gallery(f.store)

    def photo(self, label, *, delivered=False, minute=None):
        s=self.store;path=s.directory/(label+'.png');path.write_bytes(b'\x89PNG\r\n\x1a\n'+label.encode())
        image={'image_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'subject_ids':['arkady_garage_yard']}
        gid=self.gallery.register(image,'arkady',label)
        if delivered:
            receipt={'status':'sent','chat_id':-12,'message_id':len(label)}
            value={'text':'own photograph','delivery':{'status':'sent','receipts':[receipt]},'photo':image}
            with s.lock,s.db:
                s.db.execute('INSERT INTO world_cycles VALUES(?,?,?,?,?,?,?)',(label,'arkady','complete','done',s.get('revision'),'{}',1.))
                s.db.execute('INSERT INTO world_events VALUES(?,?,?,?,?,?)',(label+'-post',label,'arkady','post',s.get('minute') if minute is None else minute,json.dumps(value)))
            self.gallery.delivered('arkady',gid,label+'-post',receipt)
        return gid

    def test_reference_only_from_received_public_photo_and_not_private_gallery(self):
        self.photo('private')
        gid=self.photo('public',delivered=True)
        library=SharedPhotoReferences(self.store.directory/'test-refs',self.store,self.store.get('minute'));self.addCleanup(library.close)
        refs=library.select('garik',['arkady_garage_yard'])
        self.assertEqual([r['id'] for r in refs],[gid])
        self.assertEqual(refs[0]['publisher_actor_id'],'arkady')
        self.assertEqual(refs[0]['source'],'peer_channel')
        self.assertEqual(self.gallery.catalog('garik',['arkady_garage_yard']),[])
        payload={'life':self.store.actor_view('garik')};enrich_public_photos(self.store,'garik',payload)
        self.assertFalse(payload['_published_previews'])
        self.f.scheduler.circulate_posts()
        payload={'life':self.store.actor_view('garik')};enrich_public_photos(self.store,'garik',payload)
        self.assertEqual(len(payload['_published_previews']),1)
        params={'input':[]};WorldBackend.add_published_previews(params,payload,payload['_published_previews'])
        self.assertEqual(params['input'][-1]['type'],'localImage')

    def test_future_retracted_or_wrong_receipt_never_becomes_a_reference(self):
        self.photo('future',delivered=True,minute=self.store.get('minute')+1)
        self.photo('removed',delivered=True)
        self.photo('wrong-receipt',delivered=True)
        with self.store.lock,self.store.db:
            row=self.store.db.execute("SELECT value FROM world_events WHERE id='removed-post'").fetchone()
            self.store.db.execute("UPDATE world_events SET value=? WHERE id='removed-post'",(json.dumps({**json.loads(row[0]),'retracted':True}),))
            self.store.db.execute("UPDATE gallery_deliveries SET message_id=999 WHERE post_id='wrong-receipt-post'")
        self.assertEqual(published_photos(self.store),[])

    def test_canonical_reference_does_not_place_remote_actor_in_photo_scene(self):
        life=self.f.lives['arkady'];life.references={'garik':'approved.png'}
        self.assertNotIn('garik',life.photo_subjects())
        self.assertIn('arkady',life.photo_subjects())
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE peer_positions SET place_id='arkady_garage_yard'")
        self.assertIn('garik',life.photo_subjects())

    def test_altered_public_asset_is_excluded_and_cannot_enter_native_input(self):
        self.photo('valid',delivered=True)
        item=published_photos(self.store)[0];Path(item['image_path']).write_bytes(b'changed')
        self.assertEqual(published_photos(self.store),[])
        with self.assertRaises(ValueError):
            WorldBackend.add_published_previews({'input':[]},{'published_photos':[{'post_id':item['post_id']}]},[item])


if __name__=='__main__':unittest.main()
