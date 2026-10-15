"""Stable subject identity supplies references, never cached output for a new job."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from persona_runtime.world_media import MediaReferenceLibrary, with_visual_references, ensure_new_photo
from test_world_image_backend import PNG


class VisualMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.library = MediaReferenceLibrary(self.root/'library')
        self.path = self.root/'reference.png'; self.path.write_bytes(PNG)
        self.image = {'image_path': str(self.path), 'sha256': hashlib.sha256(PNG).hexdigest(), 'subject_ids': ['shawarma']}

    def tearDown(self): self.library.close(); self.temp.cleanup()

    def test_approved_reference_is_found_by_world_id_after_reopening(self):
        self.library.register(self.image, 'garik', 'exterior', source='approved', visual_notes='red sign, narrow doorway')
        self.library.close(); self.library = MediaReferenceLibrary(self.root/'library')
        found = self.library.select('garik', ['shawarma'])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['view'], 'exterior')
        self.assertEqual(found[0]['visual_notes'], 'red sign, narrow doorway')
        self.assertEqual(Path(found[0]['image_path']).read_bytes(), PNG)
        self.path.unlink()
        self.assertEqual(len(self.library.select('garik', ['shawarma'])), 1)

    def test_foreign_actor_unknown_place_and_hidden_co_subject_do_not_leak(self):
        self.library.register(self.image, 'garik', 'exterior', source='approved')
        self.assertEqual(self.library.select('other', ['shawarma']), [])
        self.assertEqual(self.library.select('garik', ['different_place']), [])
        self.library.register({**self.image, 'subject_ids': ['shawarma', 'unknown_person']},
                              'garik', 'doorway', source='approved')
        self.assertEqual(self.library.select('garik', ['shawarma']), [])

    def test_multiple_views_and_hashes_are_kept_for_same_place(self):
        self.library.register(self.image, 'garik', 'exterior', source='approved')
        other = self.root/'inside.png'; other.write_bytes(PNG+b'fixture')
        self.library.register({**self.image, 'image_path': str(other),
                               'sha256': hashlib.sha256(other.read_bytes()).hexdigest()},
                              'garik', 'interior', source='channel', channel_post_id='local-post-1')
        entries = self.library.select('garik', ['shawarma'])
        self.assertEqual({e['view'] for e in entries}, {'interior', 'exterior'})
        self.assertEqual(len(self.library.select('garik', ['shawarma'], limit=1)), 1)

    def test_preview_is_not_automatically_a_canonical_reference(self):
        for kwargs in ({'source': 'draft'}, {'source': 'channel'}):
            with self.assertRaises(ValueError):
                self.library.register(self.image, 'garik', 'front', **kwargs)
        self.assertEqual(self.library.select('garik', ['shawarma']), [])

    def test_changed_reference_is_rejected_or_skipped(self):
        with self.assertRaises(ValueError):
            self.library.register({**self.image, 'sha256': 'wrong'}, 'garik', 'front', source='approved')
        self.library.register(self.image, 'garik', 'front', source='approved')
        entry = self.library.select('garik', ['shawarma'])[0]
        Path(entry['image_path']).write_bytes(b'changed')
        self.assertEqual(self.library.select('garik', ['shawarma']), [])

    def test_reference_guidance_preserves_identity_and_requested_new_view(self):
        self.library.register(self.image, 'garik', 'exterior', source='approved')
        original = {'prompt': 'New photograph from inside the shop, behind the counter.'}
        result = with_visual_references(original, self.library.select('garik', ['shawarma']))
        self.assertIn(original['prompt'], result['prompt'])
        self.assertIn('genuinely new camera shot', result['prompt'])
        self.assertIn('exterior', result['prompt'])
        self.assertEqual(original, {'prompt': 'New photograph from inside the shop, behind the counter.'})

    def test_exact_old_image_cannot_be_returned_as_new_photo(self):
        with self.assertRaisesRegex(ValueError, 'identical'):
            ensure_new_photo(self.image, [self.path])
        ensure_new_photo({'sha256': 'different'}, [self.path])
