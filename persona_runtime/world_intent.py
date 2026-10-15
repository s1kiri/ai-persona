"""Lossless routing of authored intent; metadata cannot silently omit clauses."""
from .protocol import obj
from .world_protocol import CHOICE_SCHEMA, array, string, enum, validate_shape

PART = obj({'text': string(900), 'role': enum(('attempt', 'private', 'later'))})
RECOVERY = obj({'cycle_id': string(80), 'decision': enum(('resume', 'defer', 'drop')),
                'note': string(300)})
INTENT_SCHEMA = obj({**{k: v for k, v in CHOICE_SCHEMA['properties'].items() if k != 'action'},
                     'parts': array(PART, 12), 'recovery': array(RECOVERY, 3)})


def adapt(value, original):
    validate_shape(value, INTENT_SCHEMA)
    if not value['parts'] or any(not p['text'] for p in value['parts']):
        raise ValueError('intent parts must be nonempty')
    if ''.join(p['text'] for p in value['parts']) != original:
        raise ValueError('intent partition must preserve the whole original text exactly')
    action = '\n'.join(p['text'] for p in value['parts'] if p['role'] == 'attempt').strip()
    if not action:
        raise ValueError('intent must contain an attempted action')
    ids = [r['cycle_id'] for r in value['recovery']]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate recovery decision')
    return {**{k: v for k, v in value.items() if k not in ('parts', 'recovery')},
            'action': action, 'intent_parts': value['parts'], 'recovery': value['recovery']}


INSTRUCTIONS = '''Ты технически переносишь уже написанную записку в структуру.
Не выбирай поступок за персонажа и не улучшай его осторожностью.
Разбей written_intent на последовательные части parts. Конкатенация text
ВСЕХ частей должна побуквенно равняться исходной записке: сохрани пробелы,
переносы, пунктуацию и порядок. Ничего не пропускай и не перефразируй.
role=attempt — всё, что он сейчас пытается сделать, включая ограничения,
условия, альтернативы и продолжения этой же попытки. «Если продадут,
принесу и попробую» принадлежит attempt вместе с просьбой продать.
role=private — лишь личное желание, оценка и настроение, не инструкция
поступка. role=later — явно отложенное отдельное занятие на будущее.
Не выдавай условный шаг текущей попытки за private или later.
Хост сам соберёт action из всех attempt-частей. Метаданные kind, desire,
entity_ids, reaction, undertakings, revisit_minutes следуют только из
записки. note пусто. Неизвестные сущности не добавляй. По умолчанию ждать30
минут, если срок не указан. Новую биографию и чужое согласие не придумывай.
Для research attempt содержит только выбранный вопрос/тему настоящего
поиска. Следующие действия по результату пока later — сначала нужен ответ.
unfinished_attempts — прежние попытки без результата. recovery указывает
только явно выраженное отношение в нынешней записке: resume продолжить,
defer отложить, drop бросить. Не назначай решение молча; пустой список
допустим. Используй только переданные cycle_id. Это новая попытка, а не
разрешение автоматически повторить прерванный вызов.
Верни объект схемы, без дополнительного текста.'''
