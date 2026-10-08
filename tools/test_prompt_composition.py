"""Integration checks for expanded prompts; no model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from compose_persona import AGENTS, compose_persona, prompt_file
from compose_session import compose

ROOT = AGENTS.parent


class PromptCompositionTests(unittest.TestCase):
    def test_registers_embed_actual_dictionary_contents(self):
        clean = compose_persona(AGENTS / 'garik')
        free = compose_persona(AGENTS / 'garik', 'free')
        plain = (ROOT / 'resources/lexicon/non-obscene.md').read_text().strip()
        obscene = (ROOT / 'resources/lexicon/obscene.md').read_text().strip()
        self.assertIn(plain, clean)
        self.assertIn(plain, free)
        self.assertNotIn(obscene, clean)
        self.assertIn(obscene, free)
        self.assertNotIn('# Живой голос в свободной беседе', clean)
        self.assertIn('# Живой голос в свободной беседе', free)
        self.assertTrue(clean.endswith('сохраняя свой характер.\n'))
        self.assertTrue(free.endswith('настройки с собеседником.\n'))

    def test_session_uses_selected_register_and_keeps_operator_message(self):
        prompt = compose('garik', 'Keep this task open.', 'free')
        self.assertLess(prompt.index('Keep this task open.'), prompt.index('# Гарик'))
        self.assertIn(compose_persona(AGENTS / 'garik', 'free').strip(), prompt)
        self.assertIn('--- WHERE YOU LEFT OFF ---', prompt)
        self.assertIn('--- THE RULES ---', prompt)

    def test_character_without_config_remains_unchanged(self):
        expected = (AGENTS / 'vesper/PERSONA.md').read_text().strip() + '\n'
        self.assertEqual(compose_persona(AGENTS / 'vesper'), expected)
        self.assertIn(expected.strip(), compose('vesper'))

    def test_missing_dictionary_is_an_error_instead_of_partial_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'PERSONA.md').write_text('A personality.')
            (home / 'prompt.json').write_text(json.dumps({
                'default_register': 'test', 'registers': {
                    'test': {'instruction': 'Speak plainly.', 'files': ['missing.md']}
                }
            }))
            with self.assertRaises(FileNotFoundError):
                compose_persona(home)

    def test_unknown_register_cli_fails_without_printing_partial_prompt(self):
        result = subprocess.run([
            sys.executable, str(ROOT / 'tools/compose_session.py'),
            'garik', '--register', 'misspelled'
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('unknown register', result.stderr)

    def test_copyable_exports_are_current(self):
        for register, filename in (
            ('non-obscene', 'garik-prompt-non-obscene.md'),
            ('free', 'garik-prompt-full.md'),
            ('non-obscene-chat', 'garik-prompt-chat-non-obscene.md'),
            ('free-chat', 'garik-prompt-chat.md')
        ):
            exported = (ROOT / 'resources/character' / filename).read_text(encoding='utf-8')
            self.assertEqual(exported, compose_persona(AGENTS / 'garik', register))

    def test_chat_scene_is_separate_from_actual_workplace_session(self):
        chat = compose_persona(AGENTS / 'garik', 'free-chat')
        clean_chat = compose_persona(AGENTS / 'garik', 'non-obscene-chat')
        self.assertIn('# Сейчас у тебя', chat)
        self.assertIn('# Сейчас у тебя', clean_chat)
        self.assertNotIn('# Сейчас у тебя', compose('garik', register='free'))
        self.assertIn('# Живой голос в свободной беседе', chat)
        self.assertNotIn('# Живой голос в свободной беседе', clean_chat)
        self.assertLess(len(chat), 18000)
        self.assertLess(len(clean_chat), 18000)

    def test_sample_selection_preserves_source_and_reports_missing_expression(self):
        home = AGENTS / 'garik'
        path = '../../resources/lexicon/non-obscene.md'
        sample = prompt_file(home, {'path': path, 'expressions': ['жучара']})
        entry = next(line for line in (home / path).read_text().splitlines()
                     if line.startswith('- **жучара**'))
        self.assertIn(entry, sample)
        self.assertNotIn('- **огонь**', sample)
        with self.assertRaises(ValueError):
            prompt_file(home, {'path': path, 'expressions': ['missing expression']})


if __name__ == '__main__':
    unittest.main()
