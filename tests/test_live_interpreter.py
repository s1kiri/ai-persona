"""The live entry point avoids Apple's observed native SQLite hang."""
from pathlib import Path
import tempfile
import unittest

from persona_runtime.channel_life import live_interpreter


class InterpreterTests(unittest.TestCase):
    def test_only_apple_clt_uses_the_existing_configured_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);python=root/'.venv/bin/python'
            python.parent.mkdir(parents=True);python.write_text('fixture')
            apple='/Library/Developer/CommandLineTools/usr/bin/python3'
            self.assertEqual(live_interpreter(root,apple,'darwin'),str(python))
            self.assertIsNone(live_interpreter(root,str(python),'darwin'))
            self.assertIsNone(live_interpreter(root,apple,'linux'))
            self.assertIsNone(live_interpreter(root,'/opt/homebrew/bin/python3','darwin'))

    def test_no_silent_fallback_to_the_known_bad_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                live_interpreter(directory,'/Library/Developer/CommandLineTools/usr/bin/python3','darwin')
