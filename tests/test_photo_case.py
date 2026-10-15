"""An autonomous image case cannot pass on a prose claim or recovered receipt."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.photo_case import AutonomousPhotoCase
from persona_runtime.world_media import MediaReferenceLibrary
from test_world_image_backend import PNG


class Chooser:
    def __init__(self, choice): self.choice, self.calls = choice, []
    def generate(self, role, instructions, context):
        self.calls.append((role, instructions, context))
        return self.choice, {'input_tokens': 10}


class Renderer:
    def __init__(self, recovered=False, pixels=PNG): self.calls, self.recovered, self.pixels = [], recovered, pixels
    def render(self, request, destination, references):
        self.calls.append((request, references))
        destination.write_bytes(self.pixels)
        return {'image_path': str(destination), 'sha256': hashlib.sha256(self.pixels).hexdigest(),
                'native_receipt_received': not self.recovered}


class AutonomousPhotoCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = {'actor_id': 'garik', 'subjects': [{'id': 'map', 'kind': 'thing', 'name': 'Map',
                          'description': 'Folded map', 'condition': 'Unfolded on table'}],
                        'sources': [{'id': 'outcome', 'minute': 1, 'observation': 'Map on table'}]}
        self.choice = {'decision': 'show', 'reason': 'Want to show it', 'subject_ids': ['map'],
                       'source_ids': ['outcome'], 'framing': 'Above the table', 'caption': 'My words'}

    def tearDown(self): self.temp.cleanup()

    def test_model_choice_drives_tool_and_finished_case_does_not_replay(self):
        chooser, renderer = Chooser(self.choice), Renderer()
        case = AutonomousPhotoCase(self.root, chooser, renderer, 'Persona', {'map': '/reference.png'})
        result = case.run(self.context)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(chooser.calls[0][0], 'photo_choice')
        self.assertEqual(renderer.calls[0][1], ['/reference.png'])
        self.assertEqual(renderer.calls[0][0]['caption'], 'My words')
        self.assertFalse(result['parent_chat_image_tool'])
        self.assertEqual(case.run(self.context), result)
        self.assertEqual(len(chooser.calls), 1)
        self.assertEqual(len(renderer.calls), 1)

    def test_decline_never_starts_an_image(self):
        decline = {'decision': 'skip', 'reason': 'No', 'subject_ids': [], 'source_ids': [], 'framing': '', 'caption': ''}
        renderer = Renderer()
        result = AutonomousPhotoCase(self.root, Chooser(decline), renderer, 'Persona').run(self.context)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(renderer.calls, [])

    def test_unknown_subject_blocks_tool(self):
        renderer = Renderer()
        case = AutonomousPhotoCase(self.root, Chooser({**self.choice, 'subject_ids': ['future']}), renderer, 'Persona')
        with self.assertRaises(ValueError): case.run(self.context)
        self.assertEqual(renderer.calls, [])

    def test_recovery_cannot_pass_the_complete_transport_test_or_retry(self):
        chooser, renderer = Chooser(self.choice), Renderer(recovered=True)
        case = AutonomousPhotoCase(self.root, chooser, renderer, 'Persona')
        with self.assertRaisesRegex(ValueError, 'full native receipt'): case.run(self.context)
        self.assertEqual(json.loads((self.root/'case.json').read_text())['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'no replay'): case.run(self.context)
        self.assertEqual(len(renderer.calls), 1)

    def test_bad_artifact_hash_cannot_pass(self):
        renderer = Renderer()
        original = renderer.render
        def bad(*args, **kwargs): return {**original(*args, **kwargs), 'sha256': 'wrong'}
        renderer.render = bad
        with self.assertRaisesRegex(ValueError, 'does not match'):
            AutonomousPhotoCase(self.root, Chooser(self.choice), renderer, 'Persona').run(self.context)

    def test_library_supplies_prior_views_to_actor_and_references_to_tool(self):
        library = MediaReferenceLibrary(self.root/'library')
        try:
            old = self.root/'old.png'; old.write_bytes(PNG)
            library.register({'image_path': str(old), 'sha256': hashlib.sha256(PNG).hexdigest(), 'subject_ids': ['map']},
                             'garik', 'previous overhead view', source='approved')
            chooser, renderer = Chooser(self.choice), Renderer(pixels=PNG+b'new-frame-fixture')
            result = AutonomousPhotoCase(self.root/'case', chooser, renderer, 'Persona', reference_library=library).run(self.context)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(chooser.calls[0][2]['previous_photos'][0]['view'], 'previous overhead view')
            request, references = renderer.calls[0]
            self.assertEqual(len(references), 1)
            self.assertEqual(Path(references[0]).read_bytes(), PNG)
            self.assertIn('genuinely new camera shot', request['prompt'])
        finally: library.close()

    def test_returning_old_reference_cannot_pass_a_new_case(self):
        old = self.root/'old.png'; old.write_bytes(PNG)
        case = AutonomousPhotoCase(self.root/'new-case', Chooser(self.choice), Renderer(), 'Persona', {'map': str(old)})
        with self.assertRaisesRegex(ValueError, 'identical to an old reference'):
            case.run(self.context)
