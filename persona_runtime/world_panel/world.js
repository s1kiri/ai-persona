'use strict';
const $=id=>document.getElementById(id);
const fields=['events_per_hour','surprise','continuity','quiet','max_threads','post_cooldown_minutes','director_empathy'];
const phaseNames={horizon:'Планы жителей · ещё не события',audience:'Комментарии · вымышленная беседа',experience:'Что осталось важным',research:'Поиск и чтение',world:'Обстоятельства',impulse:'Записка перед поступком',choice:'Выбор персонажа',outcome:'Последствия',speech:'Речь персонажа',share:'Решение о канале',post:'Текст персонажа',done:'Эпизод завершён'};
const executionNames={has_outcome:'Есть результат попытки',interrupted:'Исхода нет · эпизод прерван',blocked:'Исхода нет · этап остановлен',pending:'Намерение · результат ещё не получен'};
const domainNames={home:'Дом',work:'Работа',food:'Еда',people:'Отношения',leisure:'Увлечения',city:'Город',learning:'Находки',money:'Деньги',rest:'Отдых'};
let state=null,dirty=false,settingsRevision=null,pending=false;
const nightFields=['hours','interval_minutes','virtual_minutes','max_calls','speech_every'];
let nightDirty=false;
function nightLabels(){for(const key of nightFields)$('night-'+key+'-value').textContent=$('night-'+key).value+(key==='hours'?' ч':key.includes('minutes')?' мин':key==='speech_every'?' эпизода':'');}
function el(tag,text,cls){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(cls)item.className=cls;return item;}
function clock(minute){const day=Math.floor(minute/1440)+1,h=Math.floor(minute%1440/60),m=minute%60;return `День ${day} · ${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;}
function active(){return state?.cycles.find(c=>['ready','running','blocked'].includes(c.status));}
function labelSliders(){for(const key of fields)$(key+'-value').textContent=$(key).value+(key==='events_per_hour'?' / час':key==='post_cooldown_minutes'?' мин':'');}
function renderControls(){
 if(!state)return;
 if(!dirty){for(const key of fields)$(key).value=state.settings[key];settingsRevision=state.settings_revision;labelSliders();}
 const current=active(),latest=state.cycles[0];
 $('prepare').disabled=!!state.channel_life||pending||state.busy||!!current;
 $('step').disabled=!!state.channel_life||pending||!state.model_enabled||state.paused||state.busy||!current||current.status!=='ready'||(state.max_calls!==null&&state.call_count>=state.max_calls);
 $('step').textContent=current?'Вызвать: '+phaseNames[current.phase]:'Следующий этап';
 $('pause').disabled=pending||!!state.live_status?.error;$('pause').textContent=state.live_status?.error?'Сначала разобрать ошибку':state.paused?'Продолжить жизнь':'Пауза после этапа';
 $('abandon').disabled=!!state.channel_life||pending||state.busy||!current||current.status==='running';
 $('speech').disabled=!!state.channel_life||pending||state.busy||!!current||!latest||latest.status!=='complete'||!!latest.value.results.speech;
 $('save-settings').disabled=pending||!dirty;$('save-settings').textContent=dirty?'Применить к следующим эпизодам':'Параметры сохранены';
 const night=state.night,running=night?.status==='running';
 $('night-start').disabled=!!state.channel_life||pending||!state.model_enabled||running||state.busy;
 $('night-stop').disabled=pending||!running;
 for(const key of nightFields){$('night-'+key).disabled=running;if(night&&!nightDirty)$('night-'+key).value=night[key];}
 nightLabels();
 const names={finished:'Завершён по времени',budget:'Завершён по лимиту',error:'Остановлен по ошибке',stopped:'Остановлен вручную'};
 $('night-status').textContent=running?`До ${new Date(night.deadline*1000).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})} МСК · ускорение ×${Number((night.virtual_minutes/night.interval_minutes).toFixed(1))} · завершено эпизодов: ${night.cycles_completed} · предел вызовов: ${night.call_ceiling}${state.paused?' · на паузе':''}`:(night?names[night.status]||'Остановлен':'Не запущен. Остановка по сроку, лимиту или ошибке.');
}
function render(value){
 state=value;
 const name=value.actor_name||'Гарик';
 document.title=name+' · пульс мира';$('character-name').textContent=name;
 $('character-initial').textContent=name.slice(0,1);
 const memory=$('origin-memory');memory.replaceChildren();
 for(const record of value.memories||[]){const card=el('article',undefined,'note');card.append(el('small','Авторская предыстория'),el('p',record.text));memory.append(card);}
 $('mode').textContent=value.model_enabled?'Совместный модельный просмотр':'Подготовка · без модели';
 $('scope').textContent=value.model_enabled?'Каждая кнопка «Вызвать» выполняет один модельный этап. Автоматических повторов и публикаций нет.':'Подготовка совместного просмотра. Можно настроить мир и подготовить эпизод; модельные вызовы выключены.';
 if(value.channel_life){$('mode').textContent=value.paused?'Telegram · жизнь на паузе':'Telegram · собственная жизнь';$('scope').textContent=name+' сам живёт и выбирает публикации. Реальные комментарии влияют на дальнейшие решения. Пауза останавливает следующие вызовы и доставки; настройки применяются к следующим эпизодам.';}
 $('telegram-panel').hidden=!value.telegram_url;
 if(value.telegram_url)$('telegram-panel').href=value.telegram_url;
 const activity=value.live_status?.activity;
 $('live-detail').hidden=!activity;
 $('live-detail').textContent=activity?`Начатое дело: ${activity.action} До окончания около ${activity.remaining_minutes} мин. Во время ожидания может заглянуть в Telegram.`:'';
 $('clock').textContent=clock(value.minute);
 const open=value.threads.filter(t=>t.status!=='closed');$('thread-count').textContent=open.length;
 $('thread-cap').textContent=`Предел новых историй: ${value.settings.max_threads}`;
 const current=active(),latest=value.cycles[0];$('phase').textContent=current?phaseNames[current.phase]:(latest?'Завершён':'Пока тишина');
 $('phase-detail').textContent=current?.status==='blocked'?'Остановлен, нужен просмотр':value.busy?'Модель отвечает':current?'Готов к отдельному просмотру':'Можно подготовить новый эпизод';
 if(value.channel_life&&!value.busy&&!value.live_status?.error){$('phase-detail').textContent=value.paused?'Продолжить можно кнопкой ниже':activity?'Идёт выбранное дело; постоянные вызовы модели не нужны':'Жизнь продолжается автоматически';}
 $('call-count').textContent=value.model_enabled&&value.max_calls!==null?`${value.call_count} / ${value.max_calls}`:String(value.call_count);
 const known=value.calls.filter(c=>Number.isInteger(c.usage.input_tokens)&&Number.isInteger(c.usage.output_tokens));
 $('usage').textContent=known.length?`${known.length<value.call_count?'≥ ':''}${known.reduce((n,c)=>n+c.usage.input_tokens+c.usage.output_tokens,0).toLocaleString('ru-RU')} токенов по полученному usage`:(value.call_count?'Usage пока неизвестен':'Открытие панели ничего не расходует');
 const chosen=value.cycles.find(c=>c.value.results.choice)?.value.results.choice;
 $('desire').textContent=chosen?.desire||'Пока нет нового решения.';
 $('choice').textContent=chosen?.action||'Здесь появится выбранный поступок. Мир определит последствия отдельно.';
 $('note').classList.toggle('hidden',!chosen?.note);$('note').textContent=chosen?.note?'Записка себе: '+chosen.note:'';
 $('pulse-status').textContent=value.busy?'Идёт этап':value.paused?'На паузе':'Готов к просмотру';$('pulse-status').classList.toggle('running',value.busy);
 $('reaction').classList.toggle('hidden',!chosen?.reaction);$('reaction').textContent=chosen?.reaction||'';
 const brief=current?.value.brief;
 $('brief').textContent=brief?(brief.new_event_allowed?'Доступны новое событие и продолжение прежнего. ':'Сейчас продолжаются известные дела и их последствия. ')+`Шанс нового события за выбранный промежуток: ${Math.round(brief.new_event_probability*100)}%. Свободных историй: ${brief.open_slots}. Персонаж может сам начать новое.`:'Мир учитывает прошлые поступки, открытые истории и недавние повторы.';
 if(current?.value.rejected){const rejected=current.value.rejected,answer=rejected.answer;$('brief').append(el('p','Предложение не применено: '+(typeof answer==='string'?answer:answer.observation||answer.action||'Не удалось принять структуру ответа.')));}
 const threads=$('threads');threads.replaceChildren();
 if(!value.threads.length)threads.append(el('p','Истории появятся из происходящего. Готового списка приключений нет.','empty'));
 for(const t of [...value.threads].sort((a,b)=>(a.status==='closed')-(b.status==='closed'))){const card=el('article',undefined,'story'+(t.status==='closed'?' closed':''));card.append(el('strong',t.title),el('p',t.situation),el('small',`${domainNames[t.domain]} · ${t.status==='closed'?'Завершена':t.status==='waiting'?'Ожидание':'Продолжается'}${t.due_minute!==null?' · '+clock(t.due_minute):''}`));if(t.open_question)card.append(el('p',t.open_question));const parent=(value.thread_links||[]).find(link=>link.child===t.id);if(parent)card.append(el('small','Продолжение истории: '+(value.threads.find(old=>old.id===parent.parent)?.title||parent.parent)));threads.append(card);}
 const timeline=$('timeline');timeline.replaceChildren();
 if(!value.history.length)timeline.append(el('p','Пока нет событий. Это пустая лаборатория, не пример ответа модели.','empty'));
 for(const event of [...value.history].reverse()){const card=el('article',undefined,'timeline-item'),row=event.value;const text=event.stage==='choice'?row.action:['speech','post','impulse'].includes(event.stage)?row.text:event.stage==='share'?(row.decision==='keep'?'Оставлю при себе':(row.decision==='reply'?'Хочу ответить: ':'Хочу рассказать: ')+(row.text??row.focus)):event.stage==='horizon'?row.hooks.map(h=>h.npc_want+' / '+h.friction).join('\n'):event.stage==='audience'?(row.comments.map(c=>c.speaker+': '+c.text).join('\n')||'Без комментариев.'):event.stage==='experience'?(row.note||'Без новой личной записи.'):event.stage==='research'?row.facts.map(f=>f.claim+' ('+f.url+')').join('\n')||'Надёжных сведений не найдено.':row.observation;card.append(el('div',`${clock(event.minute)} · ${phaseNames[event.stage]}${event.stage==='speech'?' · черновик':''}${event.execution?' · '+executionNames[event.execution]:''}`,'meta'),el('p',text));if(event.recovery){const names={resume:'Выбрал продолжить новым поступком',defer:'Отложил',drop:'Решил бросить'};card.append(el('small',names[event.recovery.decision]));if(event.recovery.note)card.append(el('p',event.recovery.note));}if(row.world_note){const details=el('details');details.append(el('summary','Заметка мира · ненаблюдаемая деталь вымысла'),el('p',row.world_note));card.append(details);}timeline.append(card);}
 const feed=$('post-feed');feed.replaceChildren();$('posts-mode').textContent=value.autonomous_posts?(value.direct_channel?name+' сам пишет в канал. Текст сохраняется без переписывания, внешней отправки нет.':name+' сам выбирает повод. Сохранено локально, внешней отправки нет.'):'Самостоятельный выбор постов сейчас выключен. Прежние черновики сохранены.';
 if(value.channel_life){$('posts-title').textContent='Записи · '+name;$('posts-mode').textContent='Дословные записи · '+name+'. Отправка проходит через очередь Telegram; опубликованным считается только текст с квитанцией доставки.';}
 for(const p of value.posts||[]){if(p.value.reply_to)continue;const card=el('article',undefined,'saved-note');card.append(el('small',clock(p.minute)+(p.value.delivery?.status==='sent'?' · опубликовано в Telegram':value.channel_life?' · подготовлено, доставка не подтверждена':' · самостоятельный черновик')),el('p',p.value.text));
 for(const batch of [...(value.audience||[])].reverse().filter(a=>a.value.post_id===p.id)){
  const discussion=el('div',undefined,'discussion');discussion.append(el('small',clock(batch.minute)+(batch.value.simulated===false?' · Реальные комментарии Telegram':' · Вымышленные комментарии · локальный опыт')));
  for(const c of batch.value.comments)discussion.append(el('p',c.speaker+': '+c.text));
  for(const reply of [...(value.posts||[])].reverse().filter(r=>r.value.audience_id===batch.id))discussion.append(el('p',name+': '+reply.value.text));
  card.append(discussion);
 }feed.append(card);}
 for(const p of value.posts||[]){if(p.value.reply_to&&!(value.posts||[]).some(root=>root.id===p.value.reply_to)){const card=el('article',undefined,'saved-note');card.append(el('small',clock(p.minute)+' · ответ к прежнему черновику'),el('p',p.value.text));feed.append(card);}}
 if(!(value.posts||[]).length)feed.append(el('p','Самостоятельных черновиков пока нет. Можно прожить событие и оставить его при себе.','empty'));
 const undertakings=$('undertakings');undertakings.replaceChildren();for(const u of value.undertakings||[]){const card=el('article',undefined,'saved-note');card.append(el('small',u.status==='closed'?'Завершил или отпустил':u.status==='waiting'?'На потом':'Хочу продолжить'),el('p',u.want));undertakings.append(card);}
 if(!(value.undertakings||[]).length)undertakings.append(el('p','Пока без сохранённых затей.','empty'));
 const unresolved=$('unfinished-attempts');unresolved.replaceChildren();
 for(const attempt of value.unfinished_attempts||[]){const card=el('article',undefined,'saved-note');card.append(el('small','Подтверждённого результата нет'),el('p',attempt.written_intent||attempt.action));unresolved.append(card);}
 if(!(value.unfinished_attempts||[]).length)unresolved.append(el('p','Нет прерванных попыток без результата.','empty'));
 const concerns=$('concerns');concerns.replaceChildren();
 for(const c of value.concerns||[]){const card=el('article',undefined,'saved-note');card.append(el('small',c.status==='closed'?'Отпустил или завершил':c.status==='waiting'?'Вернётся позже':'Занимает сейчас'),el('p',c.text));if(c.stake)card.append(el('p',c.stake));if(c.next_hint)card.append(el('small','Возможное продолжение: '+c.next_hint));concerns.append(card);}
 if(!(value.concerns||[]).length)concerns.append(el('p','Пока без личных незаконченных тем.','empty'));
 const signals=$('director-signals');signals.replaceChildren();for(const signal of value.director_signals||[]){signals.append(el('p',signal.signal||'Без сигнала режиссёру.'));}
 $('cycle-flow').textContent=value.active_world?'Обстоятельства → своё намерение → поступок и результат → личный опыт':value.personal_life?'Своё намерение → обстоятельства → поступок и результат → личный опыт':'Обстоятельства → выбор → последствия';
 const hooks=$('world-hooks');hooks.replaceChildren();
 for(const h of value.hooks||[]){const card=el('article',undefined,'saved-note');card.append(el('small',h.exposed_to.length?'Повод уже замечен':h.closes_minute<value.minute?'Возможность прошла':h.opens_minute>value.minute?'План на позже · ещё не событие':'Доступный повод · ещё не замечен'),el('p',h.npc_want),el('p',h.friction),el('small',h.route+' · '+clock(h.opens_minute)+' — '+clock(h.closes_minute)));hooks.append(card);}
 if(!(value.hooks||[]).length)hooks.append(el('p','Пока без отдельных планов жителей.','empty'));
 const notes=$('notes');notes.replaceChildren();if(!value.notes.length)notes.append(el('p','Пока без заметок.','empty'));
 for(const n of value.notes){const card=el('article',undefined,'saved-note');card.append(el('small',`${clock(n.minute)} · ${value.actors.find(a=>a.id===n.actor_id)?.name||n.actor_id}`),el('p',n.text));notes.append(card);}
 const entities=$('entities');entities.replaceChildren();for(const e of value.entities){const card=el('article',undefined,'entity');card.append(el('strong',e.name),el('p',e.condition));entities.append(card);}
 $('banner').textContent=value.failure||'';$('banner').classList.toggle('hidden',!value.failure);
 renderControls();
}
async function refresh(){try{const response=await fetch('/api/state');if(!response.ok)throw Error();render(await response.json());}catch{ $('banner').textContent='Панель не получила состояние. Действия не повторяются автоматически.';$('banner').classList.remove('hidden');}}
async function post(path,value){
 if(pending||!state)return;
 pending=true;renderControls();
 try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-World-CSRF':state.csrf},body:JSON.stringify(value)});const data=await response.json();if(!response.ok)throw Error(data.error||'Действие не выполнено.');$('banner').classList.add('hidden');render(data);return true;}catch(error){$('banner').textContent=error.message||'Нет подтверждения. Действие не повторяется.';$('banner').classList.remove('hidden');return false;}finally{pending=false;renderControls();}
}
for(const key of fields)$(key).addEventListener('input',()=>{dirty=true;labelSliders();renderControls();});
$('settings-form').addEventListener('submit',async event=>{event.preventDefault();const settings=Object.fromEntries(fields.map(key=>[key,Number($(key).value)]));if(await post('/api/settings',{settings,revision:settingsRevision})){dirty=false;renderControls();}});
$('prepare').addEventListener('click',()=>post('/api/prepare',{actor_id:state?.actor_id||'garik',minutes:Number($('minutes').value)}));
$('step').addEventListener('click',()=>{const cycle=active();if(cycle)post('/api/step',{cycle_id:cycle.id,phase:cycle.phase,revision:cycle.revision});});
$('pause').addEventListener('click',()=>post('/api/action',{paused:!state.paused}));
$('abandon').addEventListener('click',()=>{const cycle=active();if(cycle)post('/api/abandon',{cycle_id:cycle.id});});
$('speech').addEventListener('click',()=>post('/api/speech',{cycle_id:state.cycles[0].id,surface:$('speech-surface').value,message:$('speech-message').value}));
for(const key of nightFields)$('night-'+key).addEventListener('input',()=>{nightDirty=true;nightLabels();});
$('night-form').addEventListener('submit',async event=>{event.preventDefault();const options=Object.fromEntries(nightFields.map(key=>[key,Number($('night-'+key).value)]));if(await post('/api/night',{id:crypto.randomUUID(),...options})){nightDirty=false;renderControls();}});
$('night-stop').addEventListener('click',()=>post('/api/night',{action:'stop'}));
refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
