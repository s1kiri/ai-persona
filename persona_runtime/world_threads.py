"""Route a renewed matter to a new thread without overwriting its old ending."""
import copy


def branch_closed_threads(value, state):
    value = copy.deepcopy(value)
    proposed_ids = [t['id'] for t in value['threads']]
    if len(proposed_ids) != len(set(proposed_ids)):
        raise ValueError('duplicate proposed thread update')
    closed = {t['id'] for t in state['threads'] if t['status'] == 'closed'}
    reserved = {t['id'] for t in state['threads']} | {t['id'] for t in value['threads']}
    links = []
    for thread in value['threads']:
        parent = thread['id']
        if parent not in closed or thread['status'] == 'closed':
            continue
        index = 1
        while (child := parent[:65] + '-return-' + str(index)) in reserved:
            index += 1
        reserved.add(child)
        thread['id'] = child
        if value.get('thread_id') == parent:
            value['thread_id'] = child
        links.append({'child': child, 'parent': parent})
    return value, links
