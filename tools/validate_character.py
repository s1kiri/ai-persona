#!/usr/bin/env python3
"""Validate a package and its world import on disposable data; no network/model calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from persona_runtime.character_profile import CharacterProfile
from persona_runtime.world import WorldStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    profile = CharacterProfile.load(args.manifest)
    with tempfile.TemporaryDirectory(prefix='persona-package-check-') as directory:
        store = WorldStore(directory, seed_garik=False)
        try:
            profile.install(store)
            profile.install(store)
            view = store.actor_view(profile.actor_id)
            assert view['basis'] == {profile.actor_id: profile.canon}
        finally:
            store.close()
    print(json.dumps({'valid': True, 'actor_id': profile.actor_id, 'name': profile.name,
                      'entities': len(profile.environment), 'sha256': profile.digest,
                      'model_calls': 0, 'external_publications': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
