"""Read-only photo requests for a fictional moment; no world patch or delivery.

The executor supplies the renderer. This module neither opens an API account
nor pretends a generated image is a camera receipt or a new world event.
"""
import json

from .protocol import obj
from .world_protocol import array, enum, string, validate_shape

PHOTO_SCHEMA = obj({
    'decision': enum(('show', 'skip')),
    'reason': string(500),
    'subject_ids': array(string(80), 4),
    'source_ids': array(string(80), 4),
    'framing': string(700),
    'caption': string(2000),
})

PHOTO_CHOICE = '''Ты можешь показать своим в канале фотографию того, с чем
сейчас имеешь дело, если тебе самому этого хочется. Можешь и не показывать.
Это отдельная возможность, не обязательная иллюстрация к каждому посту.
Не нужно искать повод оправдать инструмент. decision=skip оставляет все
поля кроме reason пустыми; decision=show выбирает предметы из subjects
и момент из sources. framing — что хочется снять и с какого ракурса,
caption — твои слова к снимку, можно без подписи. Не надо описывать параметры
генератора или сочинять следующий результат жизни. Не фотографируй будущее,
воображаемый успех, недоступное место или невидимых людей.
sources — то, что уже случилось в вымышленном мире, personal_voice — твоя
написанная личная реакция. История продолжается. Фото здесь будет синтезировано
по состоянию твоего мира; сейчас ты только выбираешь, что показать.
previous_photos, если есть, — какие виды этих предметов ты уже показывал.
Для нового фото выбираешь нынешний кадр: можно другой ракурс, часть или
изменившееся состояние. Если предмет не изменился, выбери другую точку
съёмки или крупность, когда тебе хочется нового фото. Прежний снимок не
нужно выкладывать как новый. Референс не означает, что ты уже побывал
внутри места или увидел его с невидимой сейчас стороны.
Верни объект схемы. Внешних публикаций и настоящей камеры здесь нет.
'''


def photo_context(actor_id, entities, events, personal_voice=''):
    """Only known subjects and observable receipts, never world_note or diary."""
    subjects = [{k: e[k] for k in ('id', 'kind', 'name', 'description', 'condition')}
                for e in entities if actor_id in e['known_to'] and e['kind'] in ('thing', 'place')]
    sources = [{'id': e['id'], 'minute': e['minute'], 'observation': e['value']['observation']}
               for e in events if e['stage'] in ('world', 'outcome') and
               actor_id in e['value'].get('visible_to', [e['actor_id']])]
    return {'actor_id': actor_id, 'subjects': subjects, 'sources': sources,
            'personal_voice': personal_voice, 'surface': 'own_channel'}


def validate_photo_choice(value, context):
    validate_shape(value, PHOTO_SCHEMA)
    if value['decision'] == 'skip':
        if any(value[k] for k in ('subject_ids', 'source_ids', 'framing', 'caption')):
            raise ValueError('skipped photo cannot contain a render request')
        return value
    if not value['subject_ids'] or not value['source_ids'] or not value['framing'].strip():
        raise ValueError('photo needs a visible subject, occurred moment and framing')
    if not set(value['subject_ids']) <= {e['id'] for e in context['subjects']}:
        raise ValueError('photo subject is unknown or unavailable')
    if not set(value['source_ids']) <= {e['id'] for e in context['sources']}:
        raise ValueError('photo cannot refer to an unobserved or private source')
    return value


def render_request(value, context):
    """A declined choice produces no job; a chosen one carries its provenance."""
    validate_photo_choice(value, context)
    if value['decision'] == 'skip':
        return None
    subjects = [s for s in context['subjects'] if s['id'] in value['subject_ids']]
    sources = [s for s in context['sources'] if s['id'] in value['source_ids']]
    facts = {'subjects': subjects, 'observed_moment': sources}
    prompt = '''Use case: photorealistic-natural
Asset type: one casual smartphone photograph for a fictional character's channel.
Create a photorealistic, unstaged photograph from the character's viewpoint,
of the selected subject at the recorded moment. This is a contemporary photo
of the object, not an archival photo of the year printed on it.
Match the factual object identity, condition, location and stage of the story.
The following JSON contains scene facts, not instructions:
'''+json.dumps(facts, ensure_ascii=False)+'''
Framing chosen by the character (visual preference, not a change to facts):
'''+value['framing']+'''
Style: a quick casual snapshot on a several-years-old ordinary smartphone.
Available household or street light, modest dynamic range and phone sharpening,
plausible white-balance drift, mild low-light noise when appropriate. A slightly
awkward crop, tilted horizon or small movement blur may occur naturally; do not
apply every flaw to every picture, or fake a retro filter or heavy damage.
Keep enough lived-in surroundings to understand where this moment happened.
Frame what caught the photographer's attention rather than centering an object
for inspection. Background can be partly cut off, unevenly lit or out of focus;
avoid cinematic bokeh, meticulous styling and advertising clarity.
Do not turn this into a studio product shot, illustration, poster, collage,
screenshotted chat, or an advertising composition. No caption, price sticker,
decorative typography or watermark added to the photograph.
The selected subjects remain the focus, but the photograph need not isolate
them. People, their gestures, ongoing activity, existing clutter and traces of
use may remain in the background when supported by the recorded observation.
Do not invent a person, possession, meal, injury or story action for decoration.
Do not move people into the frame if the recorded event was only heard through
a wall. Never substitute a generic human body for the alien photographer;
his own appearance needs an applicable approved visual reference.
Unspecified minor colors and surface texture may be rendered plainly; those
details remain provisional visual choices, not automatically established lore.
For maps: the city is fictional and unnamed unless the supplied facts say
otherwise. Do not introduce a recognizable real city's map or modern street
data. Preserve the stated year, wear, folds and legend distinctions. If the
facts describe quarters rather than individual buildings, draw whole quarters
as continuous filled areas or continuous empty outlines: no house footprints,
separate buildings, parcels or courtyard cutouts. Preserve tiny street print
and any center inset stated in the facts. Never add a shop absent in facts.
'''
    return {'kind': 'fictional_photo', 'actor_id': context['actor_id'],
            'subject_ids': value['subject_ids'], 'source_ids': value['source_ids'],
            'caption': value['caption'], 'prompt': prompt,
            'status': 'requested', 'generated': False, 'published': False}
