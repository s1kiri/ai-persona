"""Typed wire routes bind peer decisions to real recipients and actual channel owners."""
import copy
from pathlib import Path
import tempfile
import unittest

from persona_runtime.shared_protocol import peer_io
from persona_runtime.world_backend import WorldBackend
from persona_runtime.world_protocol import validate_shape
from test_shared_life import peer_answer
from test_world_backend import Client


def payload():
    return {'available_source_ids':['incoming-comment','foreign-post','owner-answer'],
        'life':{'entities':[{'id':'yard','kind':'place'}]},
        'peer_context':{'unread_ids':['incoming-comment'],'available_actor_ids':['garik'],
            'nearby_actor_ids':[],'invitations':[],
            'events':[{'id':'incoming-comment','value':{'kind':'comment','sender_actor_id':'garik',
                'outbound':False,'post_id':'arkady-own-post'}}]}}


class PeerRouteTests(unittest.TestCase):
    def branches(self,value):return peer_io(value)[0]['properties']['choice']['anyOf']

    def test_comment_on_own_channel_does_not_offer_peer_comment_route(self):
        branches=self.branches(payload())
        self.assertEqual({b['properties']['decision']['enum'][0] for b in branches},{'keep','message','invite'})
        for branch in branches:
            if branch['properties']['decision']['enum']!=['keep']:
                self.assertEqual(branch['properties']['recipient_actor_id']['enum'],['garik'])
                with self.assertRaises(ValueError):validate_shape(peer_answer(decision='message',text='Own words'),branch)

    def test_received_foreign_post_binds_comment_owner_post_and_proof(self):
        value=payload();value['peer_context']['events'].append({'id':'foreign-post','value':{
            'kind':'public_post','sender_actor_id':'garik','outbound':False,'post_id':'garik-root'}})
        branch=next(b for b in self.branches(value) if b['properties']['decision']['enum']==['comment'])
        p=branch['properties'];self.assertEqual(p['post_id']['enum'],['garik-root'])
        self.assertEqual(p['recipient_actor_id']['enum'],['garik']);self.assertEqual(p['source_ids']['items']['enum'],['s2'])
        answer=peer_answer(decision='comment',recipient_actor_id='garik',text='Own words',post_id='garik-root',source_ids=['s2'])
        validate_shape(answer,branch)
        for changes in ({'recipient_actor_id':None},{'post_id':'arkady-own-post'},{'source_ids':['s1']}):
            with self.assertRaises(ValueError):validate_shape({**answer,**changes},branch)

    def test_owner_reply_proof_can_continue_comment_thread(self):
        value=payload();value['peer_context']['events'].append({'id':'owner-answer','value':{
            'kind':'comment','sender_actor_id':'garik','outbound':False,'post_id':'garik-root','media_post_id':'garik-answer'}})
        branch=next(b for b in self.branches(value) if b['properties']['decision']['enum']==['comment'])
        self.assertEqual(branch['properties']['source_ids']['items']['enum'],['s3'])

    def test_say_and_invitation_replies_exist_only_for_observed_context(self):
        value=payload();value['peer_context']['nearby_actor_ids']=['garik']
        value['peer_context']['invitations']=[{'id':'invite-1','status':'pending','sender':'garik','place_id':'yard'}]
        branches=self.branches(value)
        self.assertIn('say',{b['properties']['decision']['enum'][0] for b in branches})
        for branch in branches:
            if branch['properties']['decision']['enum'][0] in ('accept','decline'):
                self.assertEqual(branch['properties']['invitation_id']['enum'],['invite-1'])

    def test_backend_unwraps_route_without_rewriting_spoken_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=WorldBackend(tmp);backend._start=lambda:None;backend.workspace=Path(tmp);backend.overrides={}
            answer=peer_answer(decision='message',recipient_actor_id='garik',text='s1',source_ids=['s1'])
            backend.client=Client({'choice':answer})
            result,_=backend.generate('peer_choice','Synthetic role',payload())
            self.assertEqual(result['text'],'s1');self.assertEqual(result['source_ids'],['incoming-comment'])
            start=next(p for m,p in backend.client.calls if m=='turn/start')
            self.assertIn('anyOf',start['outputSchema']['properties']['choice'])


if __name__=='__main__':unittest.main()
