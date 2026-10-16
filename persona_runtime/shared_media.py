"""Public visual continuity without granting access to another actor's gallery."""
import json

from .channel_media import Gallery
from .world_media import MediaReferenceLibrary


def published_photos(store, *, owner=None, at_minute=None, include_replies=False):
    """Public channel photographs backed by an actual delivery receipt."""
    cutoff=store.get('minute') if at_minute is None else at_minute
    gallery=Gallery(store)
    rows=store.db.execute('''SELECT DISTINCT g.*,d.post_id,d.chat_id,d.message_id FROM gallery_images g
        JOIN gallery_deliveries d ON d.actor_id=g.actor_id AND d.image_id=g.id
        JOIN world_events e ON e.id=d.post_id
        WHERE e.stage='post' AND e.minute<=? ORDER BY e.rowid DESC''',(cutoff,)).fetchall()
    result=[]
    for row in rows:
        if owner is not None and row['actor_id']!=owner:continue
        event=json.loads(store.db.execute('SELECT value FROM world_events WHERE id=?',(row['post_id'],)).fetchone()[0])
        if event.get('retracted') or event.get('reply_to') and not include_replies or event.get('delivery',{}).get('status')!='sent':continue
        receipts=event['delivery'].get('receipts',[])
        if not any(r.get('chat_id')==row['chat_id'] and r.get('message_id')==row['message_id'] for r in receipts):continue
        # A gallery receipt alone must not turn a different asset into this post's photo.
        if (event.get('photo') or {}).get('sha256')!=row['sha256']:continue
        try:item=gallery.get(row['actor_id'],row['id'],json.loads(row['subject_ids']))
        except (ValueError,OSError):continue
        result.append({**item,'post_id':row['post_id'],'publisher_actor_id':row['actor_id']})
    return result


class SharedPhotoReferences:
    def __init__(self, directory, store, at_minute):
        self.own=MediaReferenceLibrary(directory)
        self.store,self.at_minute=store,at_minute

    def close(self):self.own.close()

    def select(self, actor_id, subject_ids, limit=3):
        result=self.own.select(actor_id,subject_ids,limit)
        wanted=set(subject_ids);seen={item['sha256'] for item in result}
        for item in published_photos(self.store,at_minute=self.at_minute,include_replies=True):
            if len(result)>=limit:break
            if item['actor_id']==actor_id or item['sha256'] in seen or not set(item['subject_ids'])<=wanted:continue
            result.append({**item,'visual_notes':item['caption'],'source':'peer_channel',
                           'channel_post_id':item['post_id']})
            seen.add(item['sha256'])
        return result


def enrich_public_photos(store, actor, payload):
    """Show received public pictures as public evidence, never as owned gallery entries."""
    peer=payload.get('peer_context') or payload.get('life',{}).get('peer_context',{})
    received={e['value'].get('media_post_id') or e['value'].get('post_id') for e in peer.get('events',[])
              if e['value'].get('kind') in ('public_post','comment') and not e['value'].get('outbound')}
    photos=[p for p in published_photos(store,at_minute=payload.get('life',{}).get('minute'),include_replies=True)
            if p['publisher_actor_id']!=actor and p['post_id'] in received][:2]
    payload['published_photos']=[{k:p[k] for k in ('post_id','publisher_actor_id','subject_ids','view','caption')} for p in photos]
    payload['_published_previews']=[{k:p[k] for k in ('post_id','image_path','sha256')} for p in photos]
    return payload
