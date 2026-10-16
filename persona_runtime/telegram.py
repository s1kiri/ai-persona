"""Local Bot API transport. Credentials never enter model context or diagnostics.

This module does not run the character. Receiving an update and publishing an
outbox item are separate, persistent operations; an uncertain send is not retried.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class TelegramError(RuntimeError):
    def __init__(self, message, code=None, *, transport=None):
        super().__init__(message)
        self.code = code
        self.transport = transport


def checked_token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}:[A-Za-z0-9_-]{30,128}', value.strip()):
        raise TelegramError('Не похоже на токен BotFather. Скопируй его целиком.')
    return value.strip()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.telegram-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BotAPI:
    METHODS = {'getMe', 'getWebhookInfo', 'getUpdates', 'getChat', 'getChatMember', 'sendMessage', 'sendPhoto', 'setMessageReaction', 'deleteMessage'}

    def __init__(self, token, opener=None):
        self._token = checked_token(token)
        self.last_failure = None
        # Explicit transport: do not inherit unrelated proxy environment variables.
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def call(self, method, payload=None, photo=None):
        if method not in self.METHODS:
            raise TelegramError('Этот метод Telegram не подключён.')
        value = payload or {}
        content_type = 'application/json'
        body = json.dumps(value, ensure_ascii=False).encode()
        if photo is not None:
            if method != 'sendPhoto' or not isinstance(photo, bytes) or not 0 < len(photo) <= 10_000_000:
                raise TelegramError('Некорректная фотография.')
            boundary = 'Garik' + secrets.token_hex(16)
            parts = []
            for key, item in value.items():
                rendered = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n' + rendered + '\r\n').encode())
            parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="photo.png"\r\nContent-Type: image/png\r\n\r\n').encode(), photo,
                      (f'\r\n--{boundary}--\r\n').encode()]
            body = b''.join(parts)
            content_type = 'multipart/form-data; boundary=' + boundary
        request = Request('https://api.telegram.org/bot' + self._token + '/' + method,
                          body, {'Content-Type': content_type})
        status = None
        result = None
        try:
            try:
                response = self.opener.open(request, timeout=30)
            except HTTPError as error:
                status = error.code
                response = error
            with response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError('response too large')
            result = json.loads(raw)
            if isinstance(result, dict) and result.get('ok') is True and status is None:
                return result['result']
            if isinstance(result, dict) and type(result.get('error_code')) is int:
                status = result['error_code']
        except Exception as error:
            # urllib exception strings contain the URL and therefore the token.
            failure = {'type': type(error).__name__, 'status': status,
                       'method': method, 'at': time.time()}
            reason=getattr(error,'reason',None)
            if reason is not None:
                failure['reason_type']=type(reason).__name__
                if type(getattr(reason,'errno',None)) is int:failure['errno']=reason.errno
            self.last_failure = failure
            raise TelegramError('Не удалось связаться с Telegram. Проверь интернет; токен скрыт.',
                                transport=dict(failure)) from None
        messages = {401: 'Telegram не принял токен. Проверь его в BotFather.',
                    403: 'Telegram отказал в доступе. Проверь, что бот добавлен и получил права.',
                    409: 'Бот уже получает сообщения в другом процессе или через webhook. Второе подключение остановлено.',
                    429: 'Telegram просит подождать. Отправка автоматически не повторяется.',
                    400: 'Telegram отклонил запрос. Проверь выбранный канал, обсуждение и права бота.'}
        self.last_failure = {'type': 'telegram_response', 'status': status}
        raise TelegramError(messages.get(status, 'Telegram вернул ошибку. Запрос автоматически не повторяется.'), status)


def chat_summary(chat):
    return {k: chat[k] for k in ('id', 'type', 'title', 'username') if k in chat}


class TelegramConnection:
    def __init__(self, directory, api_factory=BotAPI):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.owner = os.open(self.directory / 'telegram.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.owner)
            raise TelegramError('Подключение Telegram уже открыто другим процессом.') from None
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.operation = threading.Lock()
        self.api_factory = api_factory
        self.api = None
        self.error = None
        self.poll_blocked = False
        self.stopping = threading.Event()
        self.worker = None
        self.path = self.directory / 'telegram.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY,received REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(chat_id INTEGER NOT NULL,id INTEGER NOT NULL,
                root_id INTEGER,text TEXT NOT NULL,speaker TEXT NOT NULL,kind TEXT NOT NULL,
                user_id INTEGER,received REAL NOT NULL,PRIMARY KEY(chat_id,id));
            CREATE TABLE IF NOT EXISTS forwards(channel_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,root_id INTEGER NOT NULL,PRIMARY KEY(channel_id,post_id));
            CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,
                status TEXT NOT NULL,message_id INTEGER,text TEXT NOT NULL,error TEXT,created REAL NOT NULL,
                fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reactions(key TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,emoji TEXT NOT NULL,status TEXT NOT NULL,error TEXT);
            CREATE TABLE IF NOT EXISTS deletions(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                status TEXT NOT NULL,created REAL NOT NULL,error TEXT,
                PRIMARY KEY(chat_id,message_id));
        ''')
        with self.db:
            self.db.execute("UPDATE outbox SET status='unknown',error=? WHERE status='sending'",
                            ('Процесс остановился до квитанции. Сначала проверь канал; автоматического повтора нет.',))
            self.db.execute("UPDATE reactions SET status='unknown',error='process stopped before receipt' WHERE status='sending'")
            binding = self.get('binding')
            if binding:
                # Repair the initial receiver's known Telegram compatibility sender.
                # This ID belongs to GroupAnonymousBot, not a real reader identity.
                repaired = self.db.execute("UPDATE messages SET kind='comment',speaker=?,user_id=NULL "
                                           "WHERE chat_id=? AND kind='bot' AND user_id=1087968824 AND root_id IS NOT NULL",
                                           (binding['discussion'].get('title', 'Группа') + ' · анонимный администратор',
                                            binding['discussion']['id'])).rowcount
                if repaired:
                    self.put('anonymous_sender_repair', {'messages': repaired, 'at': time.time()})
        token_path = self.directory / 'bot-token.json'
        if token_path.exists():
            if token_path.is_symlink() or token_path.stat().st_mode & 0o077:
                self.close()
                raise TelegramError('У файла токена слишком широкие права. Нужен доступ только владельцу.')
            try:
                self.api = api_factory(checked_token(json.loads(token_path.read_text())['token']))
            except Exception:
                self.close()
                raise TelegramError('Не удалось прочитать локальный токен.') from None

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def configure(self, token):
        with self.operation:
            client = self.api_factory(checked_token(token))
            bot = client.call('getMe')
            if bot.get('is_bot') is not True or type(bot.get('id')) is not int:
                raise TelegramError('Telegram не подтвердил учётную запись бота.')
            if client.call('getWebhookInfo').get('url'):
                raise TelegramError('У бота настроен webhook другого приложения. Сначала отключи прежнее подключение.')
            with self.lock, self.db:
                previous = self.get('bot')
                if previous and previous['id'] != bot['id']:
                    raise TelegramError('Здесь уже настроен другой бот. Для другого бота нужна отдельная папка подключения.')
                private_json(self.directory / 'bot-token.json', {'token': checked_token(token)})
                self.put('bot', {k: bot[k] for k in ('id', 'username', 'first_name') if k in bot})
                self.api = client
                self.error, self.poll_blocked = None, False
        self.start()
        return self.state()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        def run():
            failures = 0
            while not self.stopping.is_set():
                if self.api is None or self.poll_blocked:
                    self.stopping.wait(1)
                    continue
                try:
                    self.poll(timeout=15)
                    failures = 0
                except TelegramError as error:
                    self.error = str(error)
                    if error.code in (401, 403, 409):
                        self.poll_blocked = True
                    failures += 1
                except Exception:
                    self.error = 'Приём сообщений остановлен из-за локальной ошибки. Нужна проверка подключения.'
                    self.poll_blocked = True
                self.stopping.wait(min(30, 2 ** min(failures, 5)) if failures else .5)
        self.worker = threading.Thread(target=run, name='telegram-receive', daemon=True)
        self.worker.start()

    def poll(self, timeout=0):
        with self.poll_lock:
            with self.lock:
                client, offset = self.api, self.get('offset', 0)
            if client is None:
                raise TelegramError('Сначала сохрани токен бота.')
            updates = client.call('getUpdates', {'offset': offset, 'limit': 100, 'timeout': timeout,
                                                'allowed_updates': ['message', 'channel_post', 'my_chat_member']})
            if not isinstance(updates, list):
                raise TelegramError('Telegram вернул некорректную пачку сообщений.')
            with self.lock, self.db:
                if self.api is not client:
                    return
                for update in sorted(updates, key=lambda u: u['update_id']):
                    update_id = update['update_id']
                    if type(update_id) is not int or update_id < offset:
                        continue
                    if not self.db.execute('SELECT 1 FROM updates WHERE id=?', (update_id,)).fetchone():
                        self.ingest(update)
                        self.db.execute('INSERT INTO updates VALUES(?,?)', (update_id, time.time()))
                    offset = max(offset, update_id + 1)
                self.put('offset', offset)
                self.put('last_poll', time.time())
                self.error = None

    def ingest(self, update):
        """Caller commits update processing and its receive cursor atomically."""
        message = update.get('message') or update.get('channel_post')
        member = update.get('my_chat_member')
        chat = (message or member or {}).get('chat', {})
        if chat.get('type') in ('channel', 'supergroup', 'group') and type(chat.get('id')) is int:
            self.db.execute('INSERT OR REPLACE INTO chats VALUES(?,?)',
                            (chat['id'], json.dumps(chat_summary(chat), ensure_ascii=False)))
        binding = self.get('binding')
        if not message or not binding or chat.get('id') not in (binding['channel']['id'], binding['discussion']['id']):
            return
        if type(message.get('message_id')) is not int:
            return
        channel_id, group_id = binding['channel']['id'], binding['discussion']['id']
        origin = message.get('forward_origin', {})
        if (chat['id'] == group_id and message.get('is_automatic_forward') is True and
                origin.get('type') == 'channel' and origin.get('chat', {}).get('id') == channel_id and
                type(origin.get('message_id')) is int):
            self.db.execute('INSERT OR REPLACE INTO forwards VALUES(?,?,?,?)',
                            (channel_id, origin['message_id'], group_id, message['message_id']))
            kind, root_id = 'forward', message['message_id']
        elif chat['id'] == channel_id:
            kind, root_id = 'post', None
        else:
            parent = message.get('reply_to_message', {})
            parent_id = parent.get('message_id')
            root = self.db.execute('SELECT root_id FROM forwards WHERE group_id=? AND root_id=?', (group_id, parent_id)).fetchone()
            if not root:
                root = self.db.execute('SELECT root_id FROM messages WHERE chat_id=? AND id=? AND root_id IS NOT NULL',
                                       (group_id, parent_id)).fetchone()
            if not root and parent.get('is_automatic_forward') is True:
                parent_origin = parent.get('forward_origin', {})
                if parent_origin.get('type') == 'channel' and parent_origin.get('chat', {}).get('id') == channel_id:
                    self.db.execute('INSERT OR IGNORE INTO forwards VALUES(?,?,?,?)',
                                    (channel_id, parent_origin['message_id'], group_id, parent_id))
                    root = (parent_id,)
            # Some discussion updates provide the root through message_thread_id.
            # Accept it only after mapping an actual auto-forward, never as a forum topic.
            if not root and type(message.get('message_thread_id')) is int:
                root = self.db.execute('SELECT root_id FROM forwards WHERE group_id=? AND root_id=?',
                                       (group_id, message['message_thread_id'])).fetchone()
            root_id = root[0] if root else None
            sender = message.get('from', {})
            # sender_chat takes precedence: Telegram uses a fake bot in `from`
            # for anonymous administrators and people speaking as a channel.
            on_behalf = bool(message.get('sender_chat'))
            kind = ('own' if sender.get('id') == self.get('bot')['id'] and not on_behalf else
                    ('comment' if root_id else 'group') if on_behalf else
                    'bot' if sender.get('is_bot') else 'comment' if root_id else 'group')
        sender = message.get('from', {})
        sender_chat = message.get('sender_chat')
        speaker = ((sender_chat.get('title') or 'От имени чата') if sender_chat else sender.get('first_name') or 'Читатель')
        self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)',
                        (chat['id'], message['message_id'], root_id, message.get('text') or message.get('caption') or '',
                         speaker, kind, None if sender_chat else sender.get('id'), time.time()))

    def bind(self, channel):
        if isinstance(channel, str) and re.fullmatch(r'-[0-9]{1,20}', channel):
            channel = int(channel)
        if not ((type(channel) is int and channel < 0) or
                (isinstance(channel, str) and re.fullmatch(r'@[A-Za-z0-9_]{5,32}', channel))):
            raise TelegramError('Выбери найденный канал или введи его @username / числовой ID.')
        with self.operation:
            if not self.api:
                raise TelegramError('Сначала сохрани токен бота.')
            chat = self.api.call('getChat', {'chat_id': channel})
            if chat.get('type') != 'channel' or type(chat.get('linked_chat_id')) is not int:
                raise TelegramError('У выбранного канала нет привязанного обсуждения. Добавь его в настройках Telegram.')
            group = self.api.call('getChat', {'chat_id': chat['linked_chat_id']})
            if group.get('type') != 'supergroup' or group.get('linked_chat_id') != chat['id']:
                raise TelegramError('Telegram не подтвердил связь канала с этой группой.')
            bot = self.get('bot')
            channel_rights = self.api.call('getChatMember', {'chat_id': chat['id'], 'user_id': bot['id']})
            group_rights = self.api.call('getChatMember', {'chat_id': group['id'], 'user_id': bot['id']})
            if channel_rights.get('status') != 'administrator' or not channel_rights.get('can_post_messages'):
                raise TelegramError('Выдай боту права администратора канала с публикацией сообщений.')
            if group_rights.get('status') != 'administrator':
                raise TelegramError('Выдай боту права администратора группы обсуждения, чтобы он видел комментарии.')
            binding = {'channel': chat_summary(chat), 'discussion': chat_summary(group), 'verified_at': time.time()}
            with self.lock, self.db:
                previous = self.get('binding')
                if previous and previous['channel']['id'] != chat['id']:
                    raise TelegramError('Здесь уже выбран другой канал. Смена требует отдельного подключения.')
                self.put('binding', binding)
        return self.state()

    def send(self, key, text, reply_to=None, photo=None):
        """Publish once to the verified channel or a known comment in its group."""
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', key):
            raise TelegramError('Некорректный ключ отправки.')
        if not isinstance(text, str) or (photo is None and not text.strip()) or len(text) > (1024 if photo else 4096):
            raise TelegramError('Текст слишком длинный или пустой.')
        with self.lock, self.db:
            if not self.api or not (binding := self.get('binding')):
                raise TelegramError('Сначала выбери канал и проверь права.')
            chat_id = binding['channel']['id']
            payload = {'chat_id': chat_id}
            if reply_to is not None:
                if type(reply_to) is not int:
                    raise TelegramError('Нужен ID полученного комментария.')
                chat_id = binding['discussion']['id']
                comment = self.db.execute("SELECT root_id FROM messages WHERE chat_id=? AND id=? AND kind='comment'", (chat_id, reply_to)).fetchone()
                if not comment or not comment[0]:
                    raise TelegramError('Этот комментарий не привязан к посту выбранного канала.')
                payload.update(chat_id=chat_id, reply_parameters={'message_id': reply_to, 'allow_sending_without_reply': False})
            payload['caption' if photo else 'text'] = text
            if photo is None:
                payload['link_preview_options'] = {'is_disabled': True}
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + (photo or b'')).hexdigest()
            existing = self.db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise TelegramError('Этот ключ уже принадлежит другому сообщению. Новая отправка заблокирована.')
                if existing['status'] == 'sent':
                    return dict(existing)
                raise TelegramError('Эта отправка уже начата или не подтверждена. Сначала проверь её в Telegram; повтор заблокирован.')
            self.db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)', (key, chat_id, 'sending', None, text, None, time.time(), fingerprint))
            client = self.api
        try:
            receipt = client.call('sendPhoto' if photo else 'sendMessage', payload, **({'photo': photo} if photo else {}))
            if receipt.get('chat', {}).get('id') != chat_id or type(receipt.get('message_id')) is not int or receipt['message_id'] <= 0:
                raise TelegramError('Telegram не подтвердил адрес доставки. Повтор заблокирован.')
        except Exception as error:
            with self.lock, self.db:
                self.db.execute("UPDATE outbox SET status='unknown',error=? WHERE key=?",
                                ('Квитанция не получена. Проверь Telegram; автоматического повтора нет.', key))
                self.put('last_send_failure', {'type': type(error).__name__,
                    'code': error.code if isinstance(error, TelegramError) else None,
                    'transport': getattr(client, 'last_failure', None)})
            if isinstance(error, TelegramError):
                raise
            raise TelegramError('Квитанция не получена. Проверь Telegram; автоматического повтора нет.') from None
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET status='sent',message_id=? WHERE key=?", (receipt['message_id'], key))
            if reply_to is not None:
                self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?)',
                                (chat_id, receipt['message_id'], comment[0], text, 'Гарик', 'own', self.get('bot')['id'], time.time()))
            return dict(self.db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone())

    def delete_own(self, chat_id, message_id):
        """Explicit operator action only; receipt must belong to our sent outbox."""
        with self.operation,self.lock,self.db:
            sent=self.db.execute("SELECT 1 FROM outbox WHERE chat_id=? AND message_id=? AND status='sent'",(chat_id,message_id)).fetchone()
            if not sent or not self.api:raise TelegramError('Удалять можно только своё подтверждённое сообщение.')
            prior=self.db.execute('SELECT status FROM deletions WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
            if prior:
                if prior[0]=='deleted':return {'chat_id':chat_id,'message_id':message_id,'status':'deleted'}
                raise TelegramError('Предыдущее удаление не подтверждено; автоматического повтора нет.')
            self.db.execute('INSERT INTO deletions VALUES(?,?,?,?,?)',(chat_id,message_id,'started',time.time(),None))
        try:
            result=self.api.call('deleteMessage',{'chat_id':chat_id,'message_id':message_id})
            if result is not True:raise TelegramError('Telegram не подтвердил удаление.')
        except Exception:
            with self.lock,self.db:
                self.db.execute("UPDATE deletions SET status='unknown',error='receipt missing; no automatic retry' WHERE chat_id=? AND message_id=?",(chat_id,message_id))
            raise TelegramError('Удаление не подтверждено; повтор заблокирован.') from None
        with self.lock,self.db:
            self.db.execute("UPDATE deletions SET status='deleted' WHERE chat_id=? AND message_id=?",(chat_id,message_id))
        return {'chat_id':chat_id,'message_id':message_id,'status':'deleted'}

    def state(self):
        with self.lock:
            return {'configured': self.api is not None, 'bot': self.get('bot'), 'binding': self.get('binding'),
                    'candidates': [json.loads(r[0]) for r in self.db.execute('SELECT value FROM chats ORDER BY id')
                                   if json.loads(r[0])['type'] == 'channel'],
                    'messages': [dict(r) for r in self.db.execute('SELECT * FROM messages ORDER BY received DESC,id DESC LIMIT 30')],
                    'outbox': [dict(r) for r in self.db.execute('SELECT * FROM outbox ORDER BY created DESC LIMIT 20')],
                    'last_poll': self.get('last_poll'), 'error': self.error, 'receive_blocked': self.poll_blocked,
                    'character_enabled': False, 'autopublish': False}

    def reaction_options(self):
        binding=self.get('binding')
        if not self.api or not binding:return []
        group=self.api.call('getChat',{'chat_id':binding['discussion']['id']})
        common=['👍','👎','❤','🔥','😁','🤔','🤯','🤬','😢','🤮','💩','🤡','🥱','🤣','🤨','😐','🖕','😈','👀','🤝','😎','😡']
        if 'available_reactions' not in group:return common
        return [r['emoji'] for r in group['available_reactions'] if r.get('type')=='emoji' and r.get('emoji') in common]

    def react(self,key,message_id,emoji):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}',key):
            raise TelegramError('Некорректный ключ реакции.')
        with self.lock,self.db:
            binding=self.get('binding')
            if not self.api or not binding or type(message_id) is not int:
                raise TelegramError('Нет подключённого обсуждения или полученного сообщения.')
            group=binding['discussion']['id']
            comment=self.db.execute("SELECT root_id FROM messages WHERE chat_id=? AND id=? AND kind='comment'",(group,message_id)).fetchone()
            if not comment or not comment[0]:raise TelegramError('Реакция доступна только на полученный комментарий канала.')
            old=self.db.execute('SELECT * FROM reactions WHERE key=?',(key,)).fetchone()
            if old:
                if old['chat_id']!=group or old['message_id']!=message_id or old['emoji']!=emoji:raise TelegramError('Ключ реакции уже использован.')
                if old['status']=='sent':return dict(old)
                raise TelegramError('Реакция не подтверждена; автоматического повтора нет.')
        if emoji not in self.reaction_options():raise TelegramError('Эта реакция недоступна в обсуждении.')
        with self.lock,self.db:
            # Claim is atomic even if two callers both checked capabilities.
            if self.db.execute('SELECT 1 FROM reactions WHERE key=?',(key,)).fetchone():
                raise TelegramError('Реакция уже выполняется.')
            self.db.execute('INSERT INTO reactions VALUES(?,?,?,?,?,?)',(key,group,message_id,emoji,'sending',None))
        try:
            result=self.api.call('setMessageReaction',{'chat_id':group,'message_id':message_id,'reaction':[{'type':'emoji','emoji':emoji}]})
            if result is not True:raise TelegramError('Telegram не подтвердил реакцию.')
        except Exception:
            with self.lock,self.db:self.db.execute("UPDATE reactions SET status='unknown',error='receipt missing; no replay' WHERE key=?",(key,))
            raise TelegramError('Реакция не подтверждена; автоматического повтора нет.') from None
        with self.lock,self.db:
            self.db.execute("UPDATE reactions SET status='sent' WHERE key=?",(key,))
            return dict(self.db.execute('SELECT * FROM reactions WHERE key=?',(key,)).fetchone())

    def close(self):
        self.stopping.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(35)
        self.db.close()
        os.close(self.owner)
