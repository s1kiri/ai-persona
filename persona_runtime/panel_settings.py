"""Owner settings for the current host; an unavailable peer never gets fake live state."""
from pathlib import Path

from .character_profile import CharacterProfile
from .profile_editor import editor_state


def character_settings(lab):
    life = getattr(lab, 'life', None)
    backend = getattr(lab.session, 'backend', None)
    actors = [{'id': lab.actor_id, 'name': lab.actor_name,
               'editable': life is not None,
               'controls': life.get('life_controls') if life else None,
               'model': getattr(backend, 'model', None),
               'reasoning_effort': getattr(backend, 'reasoning_effort', None),
               'panel_url': getattr(lab, 'telegram_url', None),
               'profile': editor_state(getattr(lab,'profile',None),paused=lab.store.get('paused'),busy=lab.busy) if life else None,
               'description': 'Настройки собственного канала и темпа жизни.' if life else
                              'Это лаборатория. Настройки канала появятся после подключения жизни.'}]
    if lab.actor_id in ('arkady', 'garik'):
        peer = 'garik' if lab.actor_id == 'arkady' else 'arkady'
        profile = CharacterProfile.load(Path(__file__).resolve().parents[1] / 'characters' / peer / 'character.json')
        actors.append({'id': peer, 'name': profile.name, 'editable': False, 'controls': None,
                       'model': profile.runtime.get('model'), 'reasoning_effort': profile.runtime.get('reasoning_effort'),
                       'panel_url': None, 'description': 'Не подключён к этому запуску. Для общей жизни перенесите его сохранённые данные в общий хост.'})
    return actors
