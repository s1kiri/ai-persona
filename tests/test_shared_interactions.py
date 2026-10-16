"""A delivered answer returns to the other bot without relying on bot polling."""
import json
import unittest

import test_shared_life as fixtures
from test_channel_social import rich


class SharedCommentExchangeTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.SharedLifeTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f

    def start_exchange(self):
        f=self.f;root=f.root_post('arkady');f.forward('arkady',root);f.scheduler.circulate_posts()
        source=f.store.peer_context('garik')['events'][0]['id']
        f.job('garik',fixtures.peer_answer(decision='comment',recipient_actor_id='arkady',text='Ты это серьёзно?',post_id=root,source_ids=[source]))
        audience=f.store.db.execute("SELECT id FROM world_events WHERE actor_id='arkady' AND stage='audience'").fetchone()[0]
        return root,audience

    def owner_reply(self,audience):
        f=self.f;life=f.lives['arkady'];f.backends['arkady'].answers.append(rich(decision='reply',text='Да, а ты что предлагаешь?',source_ids=[audience]))
        life.focus();cycle=life.channel_cycle();life.lab.session.step(cycle['id'])
        row=f.store.db.execute("SELECT * FROM world_events WHERE cycle_id=? AND stage='post'",(cycle['id'],)).fetchone()
        post={**dict(row),'value':json.loads(row['value'])};life.deliver(post)
        return post['id']

    def test_owner_answer_is_delivered_once_to_peer_and_allows_next_comment(self):
        f=self.f;root,audience=self.start_exchange();self.owner_reply(audience)
        f.scheduler.circulate_posts();f.scheduler.circulate_posts()
        replies=[e for e in f.store.peer_context('garik')['events'] if e['value']['kind']=='comment' and not e['value']['outbound']]
        self.assertEqual(len(replies),1);self.assertEqual(replies[0]['value']['text'],'Да, а ты что предлагаешь?')
        self.assertEqual(replies[0]['value']['post_id'],root)
        self.assertEqual(f.store.peer_context('arkady')['unread_ids'],[])
        f.job('garik',fixtures.peer_answer(decision='comment',recipient_actor_id='arkady',text='Сейчас подумаю.',post_id=root,source_ids=[replies[0]['id']]))
        self.assertEqual(f.store.db.execute("SELECT COUNT(*) FROM peer_jobs WHERE status='sent'").fetchone()[0],2)

    def test_human_with_same_display_name_does_not_receive_or_create_peer_delivery(self):
        f=self.f;root,audience=self.start_exchange()
        with f.connections['arkady'].lock,f.connections['arkady'].db:
            f.connections['arkady'].db.execute("UPDATE messages SET user_id=42 WHERE kind='comment'")
        self.owner_reply(audience);f.scheduler.circulate_posts()
        self.assertFalse([e for e in f.store.peer_context('garik')['events'] if e['value']['kind']=='comment' and not e['value']['outbound']])


if __name__=='__main__':unittest.main()
