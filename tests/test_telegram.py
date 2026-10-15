"""Synthetic Bot API receipts; never use a real bot, credential or channel."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from persona_runtime.telegram import BotAPI, TelegramConnection, TelegramError

TOKEN = '123456:' + 'a' * 35
CHANNEL = {'id': -10001, 'type': 'channel', 'title': '<b>Test</b>', 'linked_chat_id': -10002}
GROUP = {'id': -10002, 'type': 'supergroup', 'title': 'Test comments', 'linked_chat_id': -10001}


class API:
    def __init__(self, token):
        self.calls = []
        self.batch = []
        self.fail_send = False
        self.webhook = ''
        self.rights = True

    def call(self, method, payload=None, **kwargs):
        self.calls.append((method, payload, kwargs))
        if method == 'getMe':
            return {'id': 123456, 'is_bot': True, 'username': 'synthetic_bot', 'first_name': 'Synthetic'}
        if method == 'getWebhookInfo':
            return {'url': self.webhook}
        if method == 'getUpdates':
            return self.batch
        if method == 'getChat':
            return dict(CHANNEL if payload['chat_id'] == CHANNEL['id'] else GROUP)
        if method == 'getChatMember':
            return {'status': 'administrator' if self.rights else 'member', 'can_post_messages': self.rights}
        if method in ('sendMessage', 'sendPhoto'):
            if self.fail_send:
                raise RuntimeError('sensitive URL must not escape: ' + TOKEN)
            return {'message_id': 100 + len(self.calls), 'chat': {'id': payload['chat_id']}}
        if method=='setMessageReaction':
            if self.fail_send:raise RuntimeError('synthetic reaction timeout')
            return True
        raise AssertionError(method)


class Connection(TelegramConnection):
    def start(self):
        pass


def forward(mid=50, post=10):
    return {'message_id': mid, 'chat': GROUP, 'is_automatic_forward': True,
            'forward_origin': {'type': 'channel', 'chat': CHANNEL, 'message_id': post}, 'text': 'Post'}


def comment(mid=51, parent=None):
    return {'message_id': mid, 'chat': GROUP, 'reply_to_message': parent or forward(),
            'from': {'id': 42, 'first_name': '<script>Reader</script>', 'is_bot': False}, 'text': '<img onerror=alert(1)>'}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = Connection(self.temp.name, API)
        self.connection.configure(TOKEN)
        self.api = self.connection.api

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def bind(self):
        self.connection.bind(CHANNEL['id'])

    def receive(self, *messages):
        offset = self.connection.get('offset', 0)
        self.api.batch = [{'update_id': offset + i, 'message': message} for i, message in enumerate(messages)]
        self.connection.poll()

    def test_token_is_private_absent_from_state_database_and_logs(self):
        path = Path(self.temp.name) / 'bot-token.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.temp.name).stat().st_mode & 0o777, 0o700)
        self.assertNotIn(TOKEN, json.dumps(self.connection.state()))
        self.assertNotIn(TOKEN.encode(), self.connection.path.read_bytes())
        self.assertEqual([c[0] for c in self.api.calls], ['getMe', 'getWebhookInfo'])
        self.assertFalse(self.connection.state()['autopublish'])

    def test_existing_webhook_not_removed_and_invalid_token_not_saved(self):
        with self.assertRaises(TelegramError):
            self.connection.configure('bad')
        client = API(TOKEN)
        client.webhook = 'https://private.example/secret'
        self.connection.api_factory = lambda token: client
        with self.assertRaises(TelegramError):
            self.connection.configure(TOKEN)
        self.assertNotIn('deleteWebhook', [c[0] for c in client.calls])

    def test_discovery_ignores_private_messages_then_checks_link_and_both_rights(self):
        self.receive({'message_id': 1, 'chat': {'id': 42, 'type': 'private'}, 'text': 'private secret'},
                     {'message_id': 10, 'chat': CHANNEL, 'text': 'channel discovery'})
        state = self.connection.state()
        self.assertEqual(len(state['candidates']), 1)
        self.assertFalse(state['messages'])
        self.assertNotIn('private secret', self.connection.path.read_bytes().decode(errors='ignore'))
        self.api.rights = False
        with self.assertRaises(TelegramError):
            self.bind()
        self.assertIsNone(self.connection.get('binding'))
        self.api.rights = True
        self.bind()
        self.assertEqual(self.connection.get('binding')['discussion']['id'], GROUP['id'])

    def test_updates_deduplicate_cursor_and_route_nested_comments_to_same_post(self):
        self.bind()
        self.receive(forward(), comment(), comment(52, {'message_id': 51}))
        offset = self.connection.get('offset')
        self.connection.poll()
        self.assertEqual(self.connection.get('offset'), offset)
        self.assertEqual(len(self.connection.state()['messages']), 3)
        self.assertEqual({m['root_id'] for m in self.connection.state()['messages']}, {50})
        self.assertEqual(self.api.calls[-1][1]['offset'], offset)

    def test_reply_uses_received_message_in_discussion_not_forum_topic_and_no_repeat(self):
        self.bind()
        self.receive(comment())
        first = self.connection.send('reply:51', 'literal <b>text</b>', reply_to=51)
        calls = len(self.api.calls)
        again = self.connection.send('reply:51', 'literal <b>text</b>', reply_to=51)
        self.assertEqual(first['message_id'], again['message_id'])
        self.assertEqual(calls, len(self.api.calls))
        sent = self.api.calls[-1][1]
        self.assertEqual(sent['chat_id'], GROUP['id'])
        self.assertEqual(sent['reply_parameters'], {'message_id': 51, 'allow_sending_without_reply': False})
        self.assertNotIn('message_thread_id', sent)
        self.assertNotIn('parse_mode', sent)
        with self.assertRaises(TelegramError):
            self.connection.send('reply:51', 'different text', reply_to=51)
        with self.assertRaises(TelegramError):
            self.connection.send('reply:999', 'not a known comment', reply_to=999)

    def test_bot_and_other_group_messages_do_not_become_reader_comments(self):
        self.bind()
        other = comment(55)
        other['from'] = {'id': 999, 'is_bot': True}
        wrong_group = comment(56)
        wrong_group['chat'] = {'id': -10009, 'type': 'supergroup'}
        self.receive(other, wrong_group, {'message_id': 58, 'chat': GROUP, 'text': 'ordinary group message'})
        self.assertFalse([m for m in self.connection.state()['messages'] if m['kind'] == 'comment'])
        with self.assertRaises(TelegramError):
            self.connection.send('bot:55', 'no bot loop', reply_to=55)

    def test_anonymous_administrator_is_a_comment_despite_fake_bot_sender(self):
        self.bind()
        message = comment()
        message.update(sender_chat=GROUP, **{'from': {'id': 1087968824, 'is_bot': True, 'first_name': 'Group'}})
        self.receive(message)
        received = self.connection.state()['messages'][0]
        self.assertEqual(received['kind'], 'comment')
        self.assertEqual(received['root_id'], 50)
        self.assertEqual(received['speaker'], GROUP['title'])
        self.assertIsNone(received['user_id'])
        self.connection.send('reply:anonymous', 'reply', reply_to=51)

    def test_known_legacy_anonymous_comment_repaired_without_network_or_reception_replay(self):
        self.bind()
        self.receive(comment())
        with self.connection.db:
            self.connection.db.execute("UPDATE messages SET kind='bot',speaker='Group',user_id=1087968824 WHERE id=51")
        offset = self.connection.get('offset')
        self.connection.close()
        self.connection = Connection(self.temp.name, API)
        received = self.connection.state()['messages'][0]
        self.assertEqual(received['kind'], 'comment')
        self.assertIsNone(received['user_id'])
        self.assertEqual(self.connection.get('offset'), offset)
        self.assertFalse(self.connection.api.calls)

    def test_uncertain_send_stays_blocked_across_restart_without_token_in_exception(self):
        self.bind()
        self.api.fail_send = True
        with self.assertRaises(TelegramError) as caught:
            self.connection.send('post:1', 'hello')
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(self.connection.state()['outbox'][0]['status'], 'unknown')
        self.connection.close()
        self.connection = Connection(self.temp.name, API)
        with self.assertRaises(TelegramError):
            self.connection.send('post:1', 'hello')
        self.assertFalse(self.connection.api.calls)

    def test_sent_post_and_photo_receipts_survive_restart_no_new_network(self):
        self.bind()
        self.connection.send('post:1', 'hello')
        self.connection.send('photo:1', 'caption', photo=b'synthetic png bytes')
        self.assertEqual(self.api.calls[-1][0], 'sendPhoto')
        self.assertEqual(self.api.calls[-1][2]['photo'], b'synthetic png bytes')
        self.connection.close()
        self.connection = Connection(self.temp.name, API)
        self.connection.send('post:1', 'hello')
        self.assertFalse(self.connection.api.calls)

    def test_second_owner_cannot_consume_same_update_stream(self):
        with self.assertRaises(TelegramError):
            Connection(self.temp.name, API)

    def test_reaction_targets_known_comment_and_duplicate_does_not_touch_network(self):
        self.bind();self.receive(comment())
        first=self.connection.react('reaction:51',51,'🤣')
        calls=len(self.api.calls)
        self.assertEqual(self.connection.react('reaction:51',51,'🤣')['status'],'sent')
        self.assertEqual(calls,len(self.api.calls))
        self.assertEqual(first['message_id'],51)
        self.assertEqual(self.api.calls[-1][1]['reaction'],[{'type':'emoji','emoji':'🤣'}])
        with self.assertRaises(TelegramError):self.connection.react('reaction:999',999,'🤣')

    def test_failed_reaction_does_not_replay_after_restart(self):
        self.bind();self.receive(comment());self.api.fail_send=True
        with self.assertRaises(TelegramError):self.connection.react('reaction:51',51,'👍')
        self.connection.close();self.connection=Connection(self.temp.name,API)
        with self.assertRaises(TelegramError):self.connection.react('reaction:51',51,'👍')
        self.assertFalse(self.connection.api.calls)

    def test_unavailable_or_paid_reaction_not_dispatched(self):
        self.bind();self.receive(comment())
        original=self.api.call
        def call(method,payload=None,**kwargs):
            if method=='getChat':return {**GROUP,'available_reactions':[{'type':'emoji','emoji':'👍'}]}
            return original(method,payload,**kwargs)
        self.api.call=call
        with self.assertRaises(TelegramError):self.connection.react('reaction:51',51,'🤣')
        with self.assertRaises(TelegramError):self.connection.react('reaction:51',51,'⭐')
        self.assertFalse([c for c in self.api.calls if c[0]=='setMessageReaction'])


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def open(self, request, **kwargs):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


class HTTPTests(unittest.TestCase):
    def test_only_official_https_endpoint_and_no_redirect_or_proxy_inheritance(self):
        opener = Opener({'ok': True, 'result': {'id': 1}})
        api = BotAPI(TOKEN, opener)
        self.assertEqual(api.call('getMe')['id'], 1)
        self.assertTrue(opener.requests[0].full_url.startswith('https://api.telegram.org/bot'))
        with self.assertRaises(TelegramError):
            api.call('deleteWebhook')
        self.assertEqual(len(opener.requests), 1)

    def test_raw_network_errors_and_telegram_descriptions_never_escape(self):
        for opener in (Opener(error=RuntimeError(TOKEN)), Opener({'ok': False, 'error_code': 401, 'description': TOKEN}),
                       Opener(error=HTTPError('https://api.telegram.org/bot'+TOKEN, 403, TOKEN, {}, io.BytesIO(b'{}')))):
            with self.assertRaises(TelegramError) as caught:
                BotAPI(TOKEN, opener).call('getMe')
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_multipart_photo_contains_bytes_and_nested_reply_parameters(self):
        opener = Opener({'ok': True, 'result': {'message_id': 1}})
        BotAPI(TOKEN, opener).call('sendPhoto', {'chat_id': -1, 'caption': 'caption',
                                              'reply_parameters': {'message_id': 10}}, photo=b'PNGbytes')
        body = opener.requests[0].data
        self.assertIn(b'PNGbytes', body)
        self.assertIn(b'{"message_id": 10}', body)
        self.assertNotIn(TOKEN.encode(), body)


if __name__ == '__main__':
    unittest.main()
