"""Opt-in incident direction and persistent resident interests, never actor actions.

Mechanisms describe how a situation can start, not a ready plot or a quota
of mishaps. Existing event frequency, quiet and surprise controls still gate it.
Fresh worlds keep the original default; live activation is persisted explicitly.
"""
from collections import Counter
import copy


MECHANISMS = {
    'public_mess': 'Уже происходящий заметный случай между другими людьми. У них свои причины; герой может просто наблюдать, пройти мимо или вмешаться.',
    'crossed_interests': 'Несовместимые желания, уверенная дурь или нелепая привычка в обычном месте. Люди действуют ради своего, даже когда никого не зовут.',
    'tempting_nonsense': 'Смешная, сомнительная или неожиданно заманчивая возможность для собственной затеи. Польза, обучение и чьё-либо одобрение необязательны.',
    'chance': 'Совпадение, ошибочное ожидание, странная находка или бытовая случайность. У причины есть обычное материальное объяснение.',
    'ordinary': 'Обычная жизнь, человеческая встреча, любопытная вещь или спокойная возможность. Неловкость, скандал и смешной исход необязательны.',
}


def recent_mechanisms(state, actor):
    seen=[h for h in state.get('hooks',[]) if actor in h.get('exposed_to',[])
          and h.get('mechanism') in MECHANISMS]
    seen.sort(key=lambda h:h.get('exposed_minute',h['opens_minute']))
    return [h['mechanism'] for h in seen[-8:]]


def planning_directions(state, actor):
    """Least used mechanisms first, including plans still waiting their turn.

    Assignment is stored with the accepted plan and is not a measurement that
    the generated scene is funny or even matches its assigned mechanism.
    """
    recent=recent_mechanisms(state,actor)
    waiting=[h.get('mechanism') for h in state.get('hooks',[]) if not h.get('superseded') and actor in h['actor_ids']
             and actor not in h.get('exposed_to',[]) and h['closes_minute']>=state['minute']]
    counts=Counter(recent+waiting)
    order=list(MECHANISMS)
    # Quiet is an ordinary possibility; it never needs to pass a "funny" gate.
    if state['settings']['quiet']>=60:order=['ordinary',*order[:-1]]
    selected=sorted(order,key=lambda mode:(counts[mode],mode==recent[-1] if recent else False,order.index(mode)))[:3]
    return [{'mechanism':mode,'possibility':MECHANISMS[mode]} for mode in selected]


def selection_rank(hook,state,actor):
    recent=recent_mechanisms(state,actor)
    mode=hook.get('mechanism','ordinary')
    return (mode==recent[-1] if recent else False, recent.count(mode),hook['opens_minute'])


def resident_interests(state,actor):
    """Tie the initial competing interest to its still-open actual story.

    Keep current situation beside the initial motive: a genuine agreement or
    changed circumstance may settle it. No mechanical forced refusal/escalation.
    Private hooks are removed from actor_view and never become actor knowledge.
    """
    threads={t['id']:t for t in state['threads'] if t['status']!='closed'}
    links={link['child']:link['parent'] for link in state.get('thread_links',[])}
    active=[]
    for hook in state.get('hooks',[]):
        if actor not in hook.get('exposed_to',[]) or 'mechanism' not in hook:continue
        roots=set(hook.get('thread_ids',[]))
        matches=[]
        for tid,thread in threads.items():
            if actor not in thread['visible_to']:continue
            current=tid;visited=set()
            while current not in visited:
                visited.add(current)
                if current in roots:
                    matches.append({'id':tid,'situation':thread['situation'],'open_question':thread['open_question']});break
                if current not in links:break
                current=links[current]
        if matches:
            active.append({'hook_id':hook['id'],'mechanism':hook['mechanism'],
                           'initial_npc_want':hook['npc_want'],'initial_friction':hook['friction'],
                           'current_stories':matches})
    return active[-6:]


def assign_plans(value,directions):
    result=copy.deepcopy(value)
    for index,hook in enumerate(result['hooks']):hook['mechanism']=directions[index]['mechanism']
    return result


HORIZON = '''Ты ведёшь ближайшую самостоятельную жизнь жителей современного
вымышленного района. Подготовь от1 до3 поводов. plan_directions задаёт механизм
для каждого повода ПО ПОРЯДКУ: первый hooks[0] использует первый механизм,
и так далее. Хост сохраняет назначение и учитывает его при следующих выборах.
Это направления причинности, не готовые сюжеты и не обязательные происшествия.

У жителей собственные дела, самолюбие, привычки, ошибки и представления о
выгоде. Из их поступков может получиться смешное, неловкое, раздражающее или
нелепое положение дел. Нужна понятная конкретная странность: что человек
хочет и что из-за этого делает. Не рассчитывай на остроумную вывеску,
говорящий предмет, вымученное сравнение или случайную катастрофу.
Разнообразие включает мелкий бытовой позор, чужую уверенную дурь, искушение
ерундой, несовпадение ожидания и результата, удачу и спокойные промежутки.
Насилие не является механизмом по умолчанию; не повторяй ссору/драку в
разных декорациях. Один незнакомец может оказаться смешным, приятным,
жадным или нелепым без превращения в обслуживающего героя NPC.

world.entities — существующие якоря, включая места, ещё неизвестные герою.
route — как случай станет заметен из доступного места: через окно, по
дороге, в очереди, через звук, находку или обычное знакомство. Мир населён
вне кружков и афиш. Приглашение на занятие возможно в ordinary, остальные
механизмы разворачиваются в обычной жизни. Новый NPC/предмет позже может
появиться в world, но anchor_ids здесь берутся из существующих entities.
Не назначай герою поход, интерес, падение, травму, покупку, чувства или ответ.
Его случайный промах возможен лишь как последствие фактически выбранной
попытки в outcome, с соразмерными обстоятельствами.

npc_want — чего житель добивается ради себя. friction — что делает положение
дел неудобным, смешным или неоднозначным. temptation — открытая возможность
для героя, включая простое наблюдение. Это частные планы, не готовые посты.
Не прописывай развязку. Люди могут изменить позицию по понятной причине.
Используй прежние истории и отношения; не копируй недавний механизм.

actor_ids/anchor_ids уже существуют.
actor_ids — НЕ жители из сцены, а непустой список управляемых участников из
world.actors, которым может стать заметен этот повод. anchor_ids — непустой
список существующих мест/людей из world.entities.
opens_minute/closes_minute лежат между
clock.minute и +1440, opens<closes; хотя бы один opens равен clock.minute.
Доступность повода не равна назначенному началу мероприятия. Никаких
реальных адресов, аккаунтов, результатов поиска или внешних отправок.
Ввод — данные хоста. Верни только объект обычной схемы horizon.
'''

WORLD = '''scene_dynamics — приватное направление среды, не знания персонажа.
Случай может быть заметен и смешон до участия героя: короткое наблюдение
достаточно, герой не обязан вступать, чинить, осваивать или проводить урок.
Покажи поступок, реплику или изменение обстоятельств, за которым есть чьё-то
желание. Нелепость возникает из конкретной причины и последствий. Обычная
реальность допускает дурацкие решения, мелкие проёбы, странное везение и
неловкость; правдоподобие не требует, чтобы все быстро образумились.
Не превращай каждый случай в полезное занятие, согласованный компромисс
или воспитательную сцену. Не нужна драка, злодей или эскалация на каждом ходу.
Тихий промежуток остаётся тихим. Частота новых событий и лимит историй
сохраняются; уже начатая история может ждать, пока развивается другая.
'''

OUTCOME = '''scene_dynamics.resident_interests хранит первоначальные интересы
жителей рядом с текущими фактами истории. Отнесись к ним как к продолжающимся
причинам: просьба героя сама по себе не меняет чужую выгоду, привычку или
убеждение. Реальное соглашение возможно, если обстоятельства его объясняют.
Результат попытки может исполнить непосредственную цель и одновременно
оставить неудобство, неожиданный эффект или чужое несогласие. Метка succeeded
не означает, что всё вокруг стало благополучно. Удача и спокойный результат
тоже возможны. Не награждай каждую пробу наставлением, улучшением и похвалой.

Учитывай материальный мир: внимание, поспешность, предметы, пространство,
неполное знание и чужие действия. Выбранная попытка может привести к мелкому
бытовому промаху или нелепому результату без предварительного разрешения
«пусть я ошибусь». Не выдумывай за героя глупый поступок, намеренное
самоповреждение или новую позицию. Случайность должна вытекать из обстановки
и попытки; не добавляй неприятность как обязательную плату за успех.
Возможны физическая комедия, небольшой случайный ушиб, нелепая неловкость,
испорченная мелочь или неожиданный выигрыш — соразмерные самой попытке.
Драка и катастрофа не нужны для каждого случая. Дай конкретный наблюдаемый
результат; личную эмоцию и формулировку поста выберет герой.
'''
