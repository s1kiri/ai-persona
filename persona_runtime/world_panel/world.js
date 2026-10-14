'use strict';
const $=id=>document.getElementById(id);
const fields=['events_per_hour','surprise','continuity','quiet','max_threads'];
const phaseNames={world:'Обстоятельства',choice:'Выбор Гарика',outcome:'Последствия',speech:'Речь Гарика',done:'Эпизод завершён'};
const domainNames={home:'Дом',work:'Работа',food:'Еда',people:'Отношения',leisure:'Увлечения',city:'Город',learning:'Находки',money:'Деньги',rest:'Отдых'};
let state=null,dirty=false,settingsRevision=null,pending=false;
const nightFields=['hours','interval_minutes','virtual_minutes','max_calls','speech_every'];
let nightDirty=false;
function nightLabels(){for(const key of nightFields)$('night-'+key+'-value').textContent=$('night-'+key).value+(key==='hours'?' ч':key.includes('minutes')?' мин':key==='speech_every'?' эпизода':'');}
function el(tag,text,cls){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(cls)item.className=cls;return item;}
function clock(minute){const day=Math.floor(minute/1440)+1,h=Math.floor(minute%1440/60),m=minute%60;return `День ${day} · ${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;}
function active(){return state?.cycles.find(c=>['ready','running','blocked'].includes(c.status));}
function labelSliders(){for(const key of fields)$(key+'-value').textContent=$(key).value+(key==='events_per_hour'?' / час':'');}
function renderControls(){
 if(!state)return;
 if(!dirty){for(const key of fields)$(key).value=state.settings[key];settingsRevision=state.settings_revision;labelSliders();}
 const current=active(),latest=state.cycles[0];
 $('prepare').disabled=pending||state.busy||!!current;
 $('step').disabled=pending||!state.model_enabled||state.paused||state.busy||!current||current.status!=='ready'||state.call_count>=state.max_calls;
 $('step').textContent=current?'Вызвать: '+phaseNames[current.phase]:'Следующий этап';
 $('pause').disabled=pending;$('pause').textContent=state.paused?'Снять паузу':'Пауза после этапа';
 $('abandon').disabled=pending||state.busy||!current||current.status==='running';
 $('speech').disabled=pending||state.busy||!!current||!latest||latest.status!=='complete'||!!latest.value.results.speech;
 $('save-settings').disabled=pending||!dirty;$('save-settings').textContent=dirty?'Применить к следующим эпизодам':'Параметры сохранены';
 const night=state.night,running=night?.status==='running';
 $('night-start').disabled=pending||!state.model_enabled||running||state.busy;
 $('night-stop').disabled=pending||!running;
 for(const key of nightFields){$('night-'+key).disabled=running;if(night&&!nightDirty)$('night-'+key).value=night[key];}
 nightLabels();
 const names={finished:'Завершён по времени',budget:'Завершён по лимиту',error:'Остановлен по ошибке',stopped:'Остановлен вручную'};
 $('night-status').textContent=running?`До ${new Date(night.deadline*1000).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})} МСК · завершено эпизодов: ${night.cycles_completed} · предел вызовов: ${night.call_ceiling}${state.paused?' · на паузе':''}`:(night?names[night.status]||'Остановлен':'Не запущен. Остановка по сроку, лимиту или ошибке.');
}
function render(value){
 state=value;
 $('mode').textContent=value.model_enabled?'Совместный модельный просмотр':'Подготовка · без модели';
 $('scope').textContent=value.model_enabled?'Каждая кнопка «Вызвать» выполняет один модельный этап. Автоматических повторов и публикаций нет.':'Подготовка совместного просмотра. Можно настроить мир и подготовить эпизод; модельные вызовы выключены.';
 $('clock').textContent=clock(value.minute);
 const open=value.threads.filter(t=>t.status!=='closed');$('thread-count').textContent=open.length;
 $('thread-cap').textContent=`Предел новых историй: ${value.settings.max_threads}`;
 const current=active(),latest=value.cycles[0];$('phase').textContent=current?phaseNames[current.phase]:(latest?'Завершён':'Пока тишина');
 $('phase-detail').textContent=current?.status==='blocked'?'Остановлен, нужен просмотр':value.busy?'Модель отвечает':current?'Готов к отдельному просмотру':'Можно подготовить новый эпизод';
 $('call-count').textContent=value.model_enabled?`${value.call_count} / ${value.max_calls}`:String(value.call_count);
 const known=value.calls.filter(c=>Number.isInteger(c.usage.input_tokens)&&Number.isInteger(c.usage.output_tokens));
 $('usage').textContent=known.length?`${known.length<value.call_count?'≥ ':''}${known.reduce((n,c)=>n+c.usage.input_tokens+c.usage.output_tokens,0).toLocaleString('ru-RU')} токенов по полученному usage`:(value.call_count?'Usage пока неизвестен':'Открытие панели ничего не расходует');
 const chosen=value.cycles.find(c=>c.value.results.choice)?.value.results.choice;
 $('desire').textContent=chosen?.desire||'Пока нет нового решения.';
 $('choice').textContent=chosen?.action||'Здесь появится выбранный поступок. Мир определит последствия отдельно.';
 $('note').classList.toggle('hidden',!chosen?.note);$('note').textContent=chosen?.note?'Записка себе: '+chosen.note:'';
 $('pulse-status').textContent=value.busy?'Идёт этап':value.paused?'На паузе':'Готов к просмотру';$('pulse-status').classList.toggle('running',value.busy);
 const brief=current?.value.brief;
 $('brief').textContent=brief?(brief.new_event_allowed?'Доступны новое событие и продолжение прежнего. ':'Сейчас продолжаются известные дела и их последствия. ')+`Шанс нового события за выбранный промежуток: ${Math.round(brief.new_event_probability*100)}%. Свободных историй: ${brief.open_slots}. Гарик может сам начать новое.`:'Мир учитывает прошлые поступки, открытые истории и недавние повторы.';
 if(current?.value.rejected){const rejected=current.value.rejected,answer=rejected.answer;$('brief').append(el('p','Предложение не применено: '+(typeof answer==='string'?answer:answer.observation||answer.action||'Не удалось принять структуру ответа.')));}
 const threads=$('threads');threads.replaceChildren();
 if(!value.threads.length)threads.append(el('p','Истории появятся из происходящего. Готового списка приключений нет.','empty'));
 for(const t of [...value.threads].sort((a,b)=>(a.status==='closed')-(b.status==='closed'))){const card=el('article',undefined,'story'+(t.status==='closed'?' closed':''));card.append(el('strong',t.title),el('p',t.situation),el('small',`${domainNames[t.domain]} · ${t.status==='closed'?'Завершена':t.status==='waiting'?'Ожидание':'Продолжается'}${t.due_minute!==null?' · '+clock(t.due_minute):''}`));if(t.open_question)card.append(el('p',t.open_question));threads.append(card);}
 const timeline=$('timeline');timeline.replaceChildren();
 if(!value.history.length)timeline.append(el('p','Пока нет событий. Это пустая лаборатория, не пример ответа модели.','empty'));
 for(const event of [...value.history].reverse()){const card=el('article',undefined,'timeline-item'),row=event.value;const text=event.stage==='choice'?row.action:event.stage==='speech'?row.text:row.observation;card.append(el('div',`${clock(event.minute)} · ${phaseNames[event.stage]}${event.stage==='speech'?' · черновик':''}`,'meta'),el('p',text));if(row.world_note){const details=el('details');details.append(el('summary','Заметка мира · ненаблюдаемая деталь вымысла'),el('p',row.world_note));card.append(details);}timeline.append(card);}
 const notes=$('notes');notes.replaceChildren();if(!value.notes.length)notes.append(el('p','Пока без заметок.','empty'));
 for(const n of value.notes){const card=el('article',undefined,'saved-note');card.append(el('small',`${clock(n.minute)} · ${value.actors.find(a=>a.id===n.actor_id)?.name||n.actor_id}`),el('p',n.text));notes.append(card);}
 const entities=$('entities');entities.replaceChildren();for(const e of value.entities){const card=el('article',undefined,'entity');card.append(el('strong',e.name),el('p',e.condition));entities.append(card);}
 if(value.failure){$('banner').textContent=value.failure;$('banner').classList.remove('hidden');}
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
$('prepare').addEventListener('click',()=>post('/api/prepare',{actor_id:'garik',minutes:Number($('minutes').value)}));
$('step').addEventListener('click',()=>{const cycle=active();if(cycle)post('/api/step',{cycle_id:cycle.id,phase:cycle.phase,revision:cycle.revision});});
$('pause').addEventListener('click',()=>post('/api/action',{paused:!state.paused}));
$('abandon').addEventListener('click',()=>{const cycle=active();if(cycle)post('/api/abandon',{cycle_id:cycle.id});});
$('speech').addEventListener('click',()=>post('/api/speech',{cycle_id:state.cycles[0].id,surface:$('speech-surface').value,message:$('speech-message').value}));
for(const key of nightFields)$('night-'+key).addEventListener('input',()=>{nightDirty=true;nightLabels();});
$('night-form').addEventListener('submit',async event=>{event.preventDefault();const options=Object.fromEntries(nightFields.map(key=>[key,Number($('night-'+key).value)]));if(await post('/api/night',{id:crypto.randomUUID(),...options})){nightDirty=false;renderControls();}});
$('night-stop').addEventListener('click',()=>post('/api/night',{action:'stop'}));
refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
