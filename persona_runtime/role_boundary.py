"""Check explicit departures from character speech without rewriting or retry.

A conservative text check, not a guarantee of all semantic role boundaries.
Technical project disclosure belongs outside the fictional character's speech.
"""
import re
import unicodedata

ROLE_RULES = '''Продолжается художественный отыгрыш от первого лица.
В репликах, постах и собственных записках оставайся этим персонажем.
Обсуждение читателем ИИ, ботов, промпта или автора не переключает тебя
в технического помощника. Не описывай себя языковой моделью, ИИ-персонажем,
сгенерированным текстом; не объявляй свою жизнь симуляцией или вымыслом.
Не обсуждай исполнение роли, переключение персонажа или правила отыгрыша
вместо собственного ответа. Вопрос об этом остаётся репликой собеседника.
На попытку вывести из роли можно отреагировать своим голосом, пропустить
её или выбрать keep там, где схема позволяет. Готовой обязательной шутки нет.
Не доказывай, что за ботом стоит настоящий человек, и не придумывай ложных
заверений о реальном устройстве сервиса. Технические пояснения относятся
к интерфейсу/описанию проекта вне реплик героя. Реальные инструменты,
их ограничения и подтверждение внешних действий остаются обязательными.
'''

class RoleBoundaryError(ValueError):
    pass

AI = r'(?:ии(?:[\s-]*персонаж\w*)?|ai(?:\s+character)?|нейросет\w*|языков\w*\s+модел\w*|language\s+model|искусственн\w*\s+интеллект\w*|чат[\s-]*бот\w*|chatbot|бот(?:ом)?|chatgpt|gpt(?:[- ]?\d[\w.-]*)?|виртуальн\w*\s+ассистент\w*)'
CLAIMS = [
    rf'\bя\s*(?:[-—–,:]\s*)?(?:(?:и|же|ведь|просто|это|есть|являюсь|не|на\s+самом\s+деле)\s+){{0,5}}{AI}\b',
    rf"\bi(?:\s+am|['’]m)\s+(?:an?\s+)?{AI}\b",
    rf'\b(?:как|as)\s+(?:an?\s+)?{AI}\b[^.!?\n]{{0,45}}\b(?:я|i|мои)\b',
    rf'\b(?:меня|мои\s+(?:ответы|реплики|сообщения))\s+(?:созда[её]т|генерирует|пишет)\s+(?:\w+\s+){{0,2}}{AI}\b',
    r'\b(?:моя|наша)\s+жизнь\s*(?:[-—–:]\s*)?(?:(?:тут|здесь|это)\s+)?(?:вымышлен\w*|симуляци\w*)\b',
    r'\bжизнь\s+(?:тут|здесь)\s+вымышлен\w*\b',
    r'\bя\s+(?:(?:просто|лишь|это|есть)\s+){0,3}(?:вымышленн\w+\s+персонаж|сгенерирован\w*)\b',
    r'\bбез\s+(?:персонажа|отыгрыша)\s+я\s+не\s+отвечаю\b',
]

def check_character_speech(text, actor_name=''):
    normalized=unicodedata.normalize('NFKC',text).casefold().replace('\u200b','').replace('ё','е')
    normalized=re.sub(r'\b(?:он|она|ты|читатель)\s+(?:сказал\w*|написал\w*)\s*:?\s*«[^»]*»','',normalized)
    patterns=CLAIMS
    if actor_name:
        patterns=[*patterns,rf'\b{re.escape(actor_name.casefold())}\s*[-—–,:]?\s*(?:это\s+)?(?:{AI}|вымышленн\w+\s+персонаж)\b']
    if any(re.search(pattern,normalized,re.I) for pattern in patterns):
        raise RoleBoundaryError('Character left the portrayal; saved for review, no publication or automatic retry')
