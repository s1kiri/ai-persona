"""Publication guard checks shape without using actual credentials or private data."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tools'))
from check_publication import forbidden,secret_like


class PublicationTests(unittest.TestCase):
    def test_runtime_files_are_blocked_and_templates_remain_allowed(self):
        for path in ('.local/world.sqlite3','secrets/password.json','x/.env.local','x/live.log','agent/other/inbox.jsonl'):
            self.assertTrue(forbidden(path),path)
        for path in ('characters/_template/character.json','.env.example','agent/vesper/inbox.jsonl','tests/fixtures/messages.jsonl'):
            self.assertFalse(forbidden(path),path)

    def test_secret_shapes_are_detected_without_literal_keys_in_source(self):
        self.assertTrue(secret_like(('12345678'+':'+'a'*35).encode()))
        self.assertTrue(secret_like(('sk-'+'a'*32).encode()))
        self.assertFalse(secret_like(b'Use your own BotFather token in the local panel.'))


if __name__=='__main__':unittest.main()
