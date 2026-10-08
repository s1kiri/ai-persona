#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expand optional prompt.json into a copyable personality prompt."""
import argparse
import json
from pathlib import Path
import sys

AGENTS = Path(__file__).resolve().parent.parent / 'agent'


def required_text(path):
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError('empty prompt file: %s' % path)
    return text


def prompt_file(home, descriptor):
    if isinstance(descriptor, str):
        return required_text(home / descriptor)
    source = required_text(home / descriptor['path'])
    entries = {}
    for line in source.splitlines():
        if line.startswith('- **'):
            expression = line.split('**', 2)[1]
            entries[expression] = line
    selected = []
    for expression in descriptor['expressions']:
        if expression not in entries:
            raise ValueError('missing lexicon expression %r in %s' % (
                expression, descriptor['path']))
        selected.append(entries[expression])
    return source.splitlines()[0] + '\n\n' + '\n'.join(selected)


def compose_persona(home, register=None):
    home = Path(home)
    persona = required_text(home / 'PERSONA.md')
    config_path = home / 'prompt.json'
    if not config_path.exists():
        if register is not None:
            raise ValueError('character has no configured speech registers: %s' % home.name)
        return persona + '\n'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    selected = register if register is not None else config['default_register']
    registers = config['registers']
    if selected not in registers:
        raise ValueError('unknown register %r; available: %s' % (selected, ', '.join(registers)))
    mode = registers[selected]
    instruction = mode['instruction'].strip()
    if not instruction:
        raise ValueError('empty register instruction: %s' % selected)
    sections = [persona, '# Контекст текущего разговора\n\n' + instruction]
    for descriptor in mode['files']:
        sections.append(prompt_file(home, descriptor))
    sections.append('# Действующий регистр\n\n' + instruction)
    return '\n\n'.join(sections) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('character')
    parser.add_argument('--register', help='register name from the character prompt.json')
    args = parser.parse_args()
    try:
        prompt = compose_persona(AGENTS / args.character, args.register)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('cannot compose persona: %s' % exc, file=sys.stderr)
        return 2
    sys.stdout.write(prompt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
