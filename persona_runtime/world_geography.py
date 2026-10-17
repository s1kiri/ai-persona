"""Observed fictional routes survive the recent-event window without revealing private places."""
import json
from .protocol import obj
from .world_protocol import array,enum,identifier,string,validate_shape


PLACE_LINK = obj({'id':string(80),'from_id':string(80),'to_id':string(80),
    'directions':string(700),'minutes':{'type':'integer','minimum':1,'maximum':1440},
    'bidirectional':{'type':'boolean'},'status':enum(('open','blocked')),
    'known_to':array(string(80),8)})
LINKS = array(PLACE_LINK,6)


def validate(links,state,entities):
    validate_shape(links,LINKS)
    places={e['id']:e for e in [*state['entities'],*entities]}
    actors={a['id'] for a in state['actors']};seen=set()
    old={link['id']:link for link in state.get('place_links',[])}
    for link in links:
        identifier(link['id'])
        if link['id'] in seen:raise ValueError('duplicate place link')
        seen.add(link['id'])
        if link['from_id']==link['to_id'] or not link['directions'].strip() or not link['known_to']:
            raise ValueError('route needs two places, concrete directions and observers')
        if not set(link['known_to'])<=actors:raise ValueError('unknown route observer')
        for endpoint in ('from_id','to_id'):
            place=places.get(link[endpoint])
            if not place or place['kind']!='place':raise ValueError('route endpoint must be a place')
            if not set(link['known_to'])<=set(place['known_to']):raise ValueError('route reveals an undiscovered place')
        if link['id'] in old:
            previous=old[link['id']]
            if any(link[k]!=previous[k] for k in ('from_id','to_id')):raise ValueError('route identity cannot change endpoints')
            if not set(previous['known_to'])<=set(link['known_to']):raise ValueError('known route cannot become unknown')


def extract(answer,payload,store,cycle,stage):
    links=answer.get('place_links',[])
    validate(links,payload['world'],answer.get('entities',[]))
    old={link['id']:set(link['known_to']) for link in payload['world'].get('place_links',[])}
    observers=set(answer.get('visible_to',[cycle['actor_id']]))
    # An outcome may share an observed route only with confirmed companions.
    if stage=='outcome' and cycle['value'].get('shared_window'):
        window=cycle['value']['shared_window']
        observers={cycle['actor_id'],*(set(window['present_actor_ids']) & set(store.nearby(cycle['actor_id'])))}
    if any(set(link['known_to'])-old.get(link['id'],set())-observers for link in links):
        raise ValueError('new route knowledge needs an observer of this event')
    if 'place_links' in answer:
        with store.lock,store.db:
            current=store.cycle(cycle['id'])
            current['value'].setdefault('geography_proposals',{})[stage]=links
            store.db.execute('UPDATE world_cycles SET value=? WHERE id=?',(json.dumps(current['value'],ensure_ascii=False),cycle['id']))
    return {k:v for k,v in answer.items() if k!='place_links'},links


def apply(store,links,cycle,stage):
    # Revalidate against current committed entities inside the outcome transaction.
    validate(links,store.state(),[])
    source=cycle['id']+'-'+stage
    for link in links:
        store.db.execute('INSERT OR REPLACE INTO world_place_links VALUES(?,?,?)',
            (link['id'],json.dumps(link,ensure_ascii=False),source))
    if links:cycle['value'].setdefault('geography_receipts',{})[stage]={'source_id':source,'links':links}


INSTRUCTIONS = '''
Мир вымышленный, и ты можешь создавать обычную конкретику: название улицы,
поворот, двор, магазин, бытовое имя. Неуказанный автором адрес не запрещает
выдуманную географию; реальные персональные адреса и данные не придумывай.
Автор разрешил придумать сам город: обычный российский миллионник с дворами,
улицами вроде Советской и Лесной, жилыми домами и местными заведениями.
Создай и сохрани город как place с устойчивым id и собственным названием;
конкретика вымысла не является конфиденциальными данными. Старое «не задано»
означает незаполненную деталь, а не запрет её разработать. Фото этого города
и улиц допустимы; фотография опирается на сохранённое описание и референсы.
Обычное дополнение карты без происшествия возможно при закрытом событийнoм
шлюзе: beat=progress, domain=city, surprise=0, без thread и без NPC/предметов.
Не рассказывай герою про авторство, фиксацию канона и вымышленность: ему
известен обычный город вокруг, название может быть на табличке или в речи.
Когда герой прочёл табличку, спросил дорогу или записал название, сообщи САМО
название/ответ, а не только факт чтения или записи. Сохрани новое место в
entities с устойчивым id, названием и known_to наблюдателей. Если выяснена
связь мест, запиши place_links: откуда, куда, понятный путь, время пешком,
двусторонность, доступность и кто это узнал. Это постоянная память района,
доступная и после ухода эпизода из недавней ленты. В entity_ids наблюдения
перечисляй только ID из entities, а не ID place_links. Старое название и связь
не переизобретай. Новая улица не требует обязательной отдельной истории.
Обычная удачная попытка может дать полный практический ответ за один эпизод.
Не превращай каждое бытовое дело в многосерийное уточнение и не сохраняй
неопределённость ради открытого вопроса. Реальное препятствие возможно,
но неизвестная деталь ещё не препятствие. Друзья с известным знакомым путём
не теряются заново; создание маршрута не означает фактического перемещения.
Мир отвечает за окружающую реальность, герой сам выбирает участие и затею.
'''
