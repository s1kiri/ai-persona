"""Choose one profile for a standalone host without resetting its lived state."""
from .world import WorldConflict, WorldStore


def effective_profile(directory, requested):
    """Follow only an audited private editor revision belonging to this source profile."""
    import json
    from pathlib import Path
    import sqlite3
    from .character_profile import CharacterProfile
    directory = Path(directory).resolve()
    database = directory / 'world.sqlite3'
    if requested is None or not database.exists():
        return requested
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        row = db.execute('SELECT value FROM world_meta WHERE key=?', ('profile_overrides',)).fetchone()
    record = (json.loads(row[0]) if row else {}).get(requested.actor_id)
    if not record:
        return requested
    path = (directory / record['manifest']).resolve()
    if (not path.is_relative_to(directory / 'profiles') or
            requested.digest not in (record['base_digest'], record['digest'])):
        raise WorldConflict('source profile changed; review its private author revision first')
    profile = CharacterProfile.load(path)
    if profile.actor_id != requested.actor_id or profile.digest != record['digest']:
        raise WorldConflict('private author revision does not match its audit')
    return profile


def open_world(directory, profile=None):
    store = WorldStore(directory, seed_garik=profile is None)
    try:
        actor = profile.actor_id if profile else 'garik'
        binding = store.get('host_profile')
        if binding and (binding['actor_id'] != actor or binding['digest'] != (profile.digest if profile else None)):
            raise WorldConflict('data directory belongs to a different profile; use an explicit migration')
        if any(a['id'] != actor for a in store.state()['actors']):
            raise WorldConflict('standalone profile host requires its own world directory')
        if profile:
            installed = (store.get('character_packages') or {}).get(actor)
            if installed is None:
                profile.install(store)
            elif installed != profile.digest:
                raise WorldConflict('installed profile changed; explicit migration required')
        with store.lock, store.db:
            store.put('host_profile', {'actor_id': actor, 'name': profile.name if profile else 'Гарик',
                                       'digest': profile.digest if profile else None})
        return store
    except Exception:
        store.close()
        raise


def bind_connection(connection, actor, world_directory):
    """A bot connection belongs to one character and one delivery history."""
    from pathlib import Path
    from .telegram import TelegramError
    identity = {'actor_id': actor, 'world': str(Path(world_directory).resolve())}
    with connection.lock, connection.db:
        old = connection.get('host_profile')
        if old and old != identity:
            raise TelegramError('Это Telegram-подключение принадлежит другому герою или миру.')
        if (old is None and actor != 'garik' and connection.get('setup_actor') != actor and
                connection.db.execute('SELECT 1 FROM outbox').fetchone()):
            raise TelegramError('Для нового профиля нужна отдельная Telegram-папка без прежних отправок.')
        setup_actor = connection.get('setup_actor')
        if setup_actor and setup_actor != actor:
            raise TelegramError('Это подключение было настроено для другого героя.')
        connection.put('host_profile', identity)


def claim_setup(connection, actor):
    """Keep technical setup posts in their character's connection, before life exists."""
    from .telegram import TelegramError
    with connection.lock, connection.db:
        binding = connection.get('host_profile')
        old = connection.get('setup_actor')
        if (binding and binding['actor_id'] != actor) or (old and old != actor):
            raise TelegramError('Это подключение принадлежит другому герою.')
        if (not binding and not old and actor != 'garik' and
                connection.db.execute('SELECT 1 FROM outbox').fetchone()):
            raise TelegramError('Для настройки нового героя нужна отдельная Telegram-папка.')
        connection.put('setup_actor', actor)
