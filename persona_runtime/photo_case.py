"""Finite headless test: Garik's choice -> his native tool -> verified artifact.

This runner has no access to tools of a parent Codex chat. All inference and
image operations are performed by the configured child Codex processes.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

from .app_server import save_private
from .world_photo import PHOTO_CHOICE, render_request
from .world_media import MediaReferenceLibrary, with_visual_references, ensure_new_photo


class AutonomousPhotoCase:
    def __init__(self, directory, chooser, renderer, persona, references=None, observer=None, reference_library=None, cancel=None):
        self.directory = Path(directory)
        self.chooser, self.renderer, self.persona = chooser, renderer, persona
        self.references = references or {}
        self.observer = observer or (lambda value: None)
        self.reference_library = reference_library
        self.cancel = cancel

    def run(self, context, choice=None):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_path = self.directory/'case.json'
        if state_path.exists():
            prior = json.loads(state_path.read_text())
            if prior['status'] in ('passed', 'skipped'):
                return prior
            raise ValueError('case already started; explicit recovery required, no replay')
        fd = os.open(state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'status': 'started'}, stream)
        try:
            context = copy.deepcopy(context)
            if self.reference_library:
                previous = self.reference_library.select(context['actor_id'], [s['id'] for s in context['subjects']])
                context['previous_photos'] = [{k:e[k] for k in ('subject_ids','view','visual_notes','publisher_actor_id','source') if k in e} for e in previous]
            save_private(self.directory/'scene.json', context)
            self.observer({'phase': 'character_chooses', 'actor_id': context['actor_id']})
            if choice is None:
                choice, usage = self.chooser.generate('photo_choice', self.persona+'\n\n'+PHOTO_CHOICE, context,
                                                      **({'cancel': self.cancel} if self.cancel is not None else {}))
            else:
                usage={} # The character already selected it in the message decision.
            save_private(self.directory/'choice.json', {'choice': choice, 'usage': usage})
            request = render_request(choice, context)
            if request is None:
                result = {'status': 'skipped', 'choice': choice, 'choice_usage': usage,
                          'image_calls': 0, 'published': False}
            else:
                references = [self.references[i] for i in request['subject_ids'] if i in self.references]
                entries = self.reference_library.select(context['actor_id'], request['subject_ids']) if self.reference_library else []
                references = list(dict.fromkeys([*references, *(e['image_path'] for e in entries)]))
                request = with_visual_references(request, entries)
                save_private(self.directory/'request.json', request)
                save_private(self.directory/'used-references.json', references)
                self.observer({'phase': 'character_tool_generates', 'actor_id': context['actor_id'], 'choice': choice, 'references': len(references)})
                image = self.renderer.render(request, self.directory/'photo.png', references=references,
                                             **({'cancel': self.cancel} if self.cancel is not None else {}))
                path = Path(image['image_path'])
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != image['sha256']:
                    raise ValueError('image receipt does not match the saved file')
                if not image.get('native_receipt_received'):
                    # Recovery is useful, but it cannot pass this test of the
                    # whole corrected live transport and native completion.
                    raise ValueError('asset recovered but full native receipt is still missing')
                ensure_new_photo(image, references)
                result = {'status': 'passed', 'choice': choice, 'choice_usage': usage,
                          'image': image, 'image_calls': 1, 'published': False,
                          'parent_chat_image_tool': False}
            save_private(state_path, result)
            self.observer({'phase': result['status']})
            return result
        except Exception as error:
            save_private(state_path, {'status': 'failed', 'error_type': type(error).__name__,
                                      'message': str(error)[:500], 'automatic_retry': False})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--scene', required=True, type=Path)
    parser.add_argument('--persona', required=True, type=Path)
    parser.add_argument('--references', type=Path, help='JSON mapping subject IDs to local visual references')
    parser.add_argument('--reference-library', type=Path, help='persistent visual references indexed by world subject ID')
    args = parser.parse_args(argv)
    directory = args.data_dir.expanduser().resolve()
    root = Path(__file__).resolve().parent.parent
    if directory == root or (root in directory.parents and root/'.local' not in directory.parents):
        parser.error('repository test data must live under .local/')
    context = json.loads(args.scene.read_text())
    persona = args.persona.read_text()
    if not persona.strip(): parser.error('empty persona')
    refs = json.loads(args.references.read_text()) if args.references else {}
    from .proxy import SharedTransport
    from .world_backend import WorldBackend
    from .world_image_backend import CodexImageBackend
    owner = SharedTransport()
    chooser = WorldBackend(directory/'choice-transport', transport_factory=owner.borrow)
    renderer = CodexImageBackend(directory/'image-transport', transport_factory=owner.borrow)
    library = MediaReferenceLibrary(args.reference_library) if args.reference_library else None
    try:
        case = AutonomousPhotoCase(directory, chooser, renderer, persona, refs,
                                   observer=lambda v: print(json.dumps(v, ensure_ascii=False), flush=True),
                                   reference_library=library)
        result = case.run(context)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        chooser.close(); renderer.close(); owner.close()
        if library: library.close()


if __name__ == '__main__': main()
