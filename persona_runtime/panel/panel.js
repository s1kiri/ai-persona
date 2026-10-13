'use strict';
const $=id=>document.getElementById(id);
const fmt=x=>x==null?'—':new Intl.NumberFormat('ru-RU').format(x);
const labels={message:'Общение',wake:'Пробуждение',search:'Поиск',recall:'Память',legacy_message:'Архив · ход',legacy_wake:'Архив · пробуждение'};
const statuses={running:'В процессе',completed:'Завершён',failed:'Ошибка',cancelled:'Отменён',interrupted:'Прерван'};
let state=null,revision=null,connected=false,sending=false;
const storageKey='garik-pending-message';
let pendingSend=null;
try{pendingSend=JSON.parse(localStorage.getItem(storageKey));}catch{}
function node(tag,text,className){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(className)n.className=className;return n;}
function set(id,text){$(id).textContent=text;}
function clear(id){$(id).replaceChildren();}
function banner(text){set('banner',text);$('banner').classList.toggle('hidden',!text);}
function date(x){return x?new Date(x*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';}
function duration(seconds){seconds=Math.max(0,Math.floor(seconds));const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60);return h?h+' ч '+m+' мин':m?m+' мин '+seconds%60+' с':seconds+' с';}
async function post(path,data){
 const result=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Panel-CSRF':state.csrf},body:JSON.stringify(data)});
 if(!result.ok)throw Error('Запрос не принят ('+result.status+')');
 return result.json();
}
function metric(total,key){return total.coverage[key]?fmt(total[key]):'—';}
function details(target,items){clear(target);for(const [name,value] of items){const row=node('div',null,'detail');row.append(node('span',name),node('strong',value));$(target).append(row);}}
function renderMessages(){
 const box=$('messages'),bottom=box.scrollHeight-box.scrollTop-box.clientHeight<70;
 const oldIds=box.dataset.ids,ids=state.messages.map(x=>x.id+':'+x.status).join(',');
 if(oldIds!==ids){
  box.replaceChildren();box.dataset.ids=ids;
  if(!state.messages.length)box.append(node('div','Можно просто поздороваться. Чат сохраняется между открытиями панели.','empty'));
  for(const m of state.messages){
   const bubble=node('div',m.text,'bubble '+m.role+(m.status==='superseded'?' superseded':''));
   bubble.dataset.messageId=m.id;
   bubble.append(node('small',date(m.created)+(m.status==='superseded'?' · ответ на эту реплику прерван':m.role==='garik'?' · '+(m.status==='read'?'показано':'в локальном чате'):'')));
   box.append(bubble);
  }
  if(bottom||!oldIds)box.scrollTop=box.scrollHeight;
 }
 if(!document.hidden){
  const ids=state.messages.filter(x=>x.role==='garik'&&x.status==='delivered').map(x=>x.id).slice(-100);
  if(ids.length)post('/api/seen',{ids}).catch(()=>{});
 }
}
function renderFeed(){
 clear('feed');
 const names={'runtime.started':'Runtime запущен','runtime.stopped':'Runtime остановлен','runtime.control':'Управление','runtime.notice':'Состояние','call.started':'Вызов Codex','call.finished':'Вызов окончен','usage':'Получен точный usage','web.tool':'Нативный веб-инструмент','chat.delivered':'Сообщение в локальном чате'};
 for(const e of state.feed){
  const row=node('div',null,'event');row.append(node('i'));const body=node('div');
  body.append(node('div',names[e.kind]||e.kind,'event-title'));
  let text='';
  if(e.kind==='call.started')text=(labels[e.data.category]||e.data.category)+(e.data.query?' · '+e.data.query:'');
  if(e.kind==='call.finished')text=statuses[e.data.status]||e.data.status;
  if(e.kind==='runtime.notice')text=e.data.text;
  if(e.kind==='runtime.control')text={pause:'Пауза инициативы',resume:'Продолжить инициативу',cancel:'Отмена текущего хода',stop:'Остановка'}[e.data.action];
  if(e.kind==='web.tool'){const a=e.data.action;text=(e.data.failed?'Ошибка инструмента · ':e.data.stage==='completed'?'Завершён · ':'Начат · ')+(a.query||(a.queries||[]).join(' / ')||a.url||a.type||'');}
  if(e.kind==='usage'){const u=e.data.usage;text='Input '+fmt(u.input_tokens)+' · output '+fmt(u.output_tokens)+' · cached '+fmt(u.cached_input_tokens);}
  if(text)body.append(node('p',text));body.append(node('small',date(e.created)));row.append(body);$('feed').append(row);
 }
}
function renderUsage(){
 const u=state.usage.session;
 set('session-total',u.known_token_calls?(u.unknown_calls?'≥':'')+fmt(u.total_tokens):u.attempts?'—':'0');
 set('input-total',metric(u,'input_tokens'));set('output-total',metric(u,'output_tokens'));set('cache-total',metric(u,'cached_input_tokens'));
 set('usage-status',u.unknown_calls?'Итог известной части. Без usage: '+u.unknown_calls+' вызов(а). Точный счётчик обновится после завершения.':'Точный итог полученных usage. Обновляется ступенями, не во время генерации.');
 $('calls-progress').max=state.limits.max_calls;$('calls-progress').value=state.calls_used;
 set('budget-line',state.calls_used+' / '+state.limits.max_calls+' попыток · '+state.wakes_used+' / '+state.limits.max_wakes+' пробуждений');
 set('token-budget','Известно '+fmt(u.total_tokens)+' / '+fmt(state.limits.max_known_tokens)+' токенов. Порог post-hoc: новый вызов остановится после достижения.');
 clear('usage-summary');
 for(const [key,name] of [['session','Этот запуск'],['day','Сегодня · UTC'],['all','Вся сохранённая история']]){
  const t=state.usage[key],card=node('article',null,'card summary');card.append(node('div',name,'eyebrow'),node('strong',t.known_token_calls?(t.unknown_calls?'≥':'')+fmt(t.total_tokens):t.attempts?'—':'0'),node('small',(t.unknown_attempt_groups?'≥':'')+fmt(t.attempts)+' попыток · '+fmt(t.unknown_calls)+' без полного usage'),node('small','Кэш внутри input: '+metric(t,'cached_input_tokens')));
  $('usage-summary').append(card);
 }
 clear('calls-table');
 for(const c of state.calls){
  const row=node('tr'),type=node('td',date(c.started));type.append(node('small',labels[c.category]||c.category));row.append(type,node('td',statuses[c.status]||c.status),node('td',c.model||'Неизвестна / CLI default'));
  for(const key of ['input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens'])row.append(node('td',fmt(c.usage[key])));
  $('calls-table').append(row);
 }
}
function renderContext(){
 const report=state.calls.find(x=>x.context&&Object.keys(x.context).length)?.context;
 clear('context-parts');clear('memory-sources');
 if(!report){$('context-parts').append(node('p','Первый подготовленный запрос ещё не зафиксирован.'));set('context-note','Оценки будут доступны при запуске вызова.');return;}
 const names={canon:'Личность и лексика',contract:'Контракт среды',input:'Новая реплика',state:'Состояние и намерения',history:'История',memory:'Память',activity:'Занятия',sources:'Квитанции источников'};
 const max=Math.max(...Object.values(report.parts),1);
 for(const [key,value] of Object.entries(report.parts)){const row=node('div',null,'part'),head=node('div',null,'part-head');head.append(node('span',names[key]||key),node('strong','~'+fmt(value)));const bar=node('progress');bar.max=max;bar.value=value;row.append(head,bar);$('context-parts').append(row);}
 set('context-note','Подготовлено ~'+fmt(report.total)+' · '+report.counter+'. Это не токенизатор модели. Внутренние инструкции CLI и результаты native web вне этой оценки.');
 details('context-limits',[['Память',fmt(report.memory_used)+' / '+fmt(report.memory_budget)+' оценочных токенов'],['История',report.history_entries+' реплик; '+report.history_excerpted+' сокращено'],['Состояние',report.state_chars_included+' / '+report.state_chars_available+' символов'],['Суммаризация','Снимок состояния модели; отдельного суммаризатора нет']]);
 for(const m of state.memory){const card=node('div',(m.kind||m.type)+' · '+m.basis);card.append(node('small','Источник '+m.source_event+' · '+date(m.created)+(m.excerpted?' · сокращено':'')+(m.historical_dialogue?' · прежний диалог':'')));$('memory-sources').append(card);}
 clear('sources');
 for(const source of state.sources){const item=node('div');if(source.url){const a=node('a',source.title);a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';item.append(a);}else item.append(node('span',source.title));item.append(node('small','Источник хода '+source.source_event+' · '+date(source.created)));$('sources').append(item);}
 if(!state.sources.length)$('sources').append(node('small','Подтверждённых поисковых источников пока нет.'));
}
function renderChannels(){
 clear('channel-cards');
 for(const c of [...state.channels,...state.capabilities]){
  const on=c.connected??c.enabled,card=node('article',null,'card summary');card.append(node('div',c.title,'eyebrow'),node('strong',on?'Подключено':'Не подключено'),node('small',on?'Реально доступно в этом запуске':'Возможность не симулируется'));$('channel-cards').append(card);
 }
 details('limits-list',[['Попытки на запуск',fmt(state.limits.max_calls)],['Самостоятельные пробуждения',fmt(state.limits.max_wakes)],['Время инициативы',state.limits.max_minutes+' мин'],['Минимальный интервал wake',state.limits.min_wake_seconds/60+' мин'],['Known tokens / запуск',fmt(state.limits.max_known_tokens)],['Known tokens / день UTC',fmt(state.limits.max_daily_known_tokens)],['Попытки / день UTC',fmt(state.limits.max_daily_calls)],['Native web / этап',fmt(state.limits.max_web_calls)]]);
}
function clocks(){
 if(!state)return;
 set('uptime','В работе '+duration(Date.now()/1000-state.started)+' · с '+date(state.started));
 const due=state.effective_wake;
 set('wake',state.mode==='budget'?'Лимит достигнут':state.autonomy!=='включена'?state.autonomy:due==null?'Первое пробуждение':due>Date.now()/1000?'через '+duration(due-Date.now()/1000):state.current?'после текущего хода':'готово к пробуждению');
 set('wake-detail',state.next_wake?new Date(state.next_wake*1000).toLocaleString('ru-RU')+(due>state.next_wake?' · действует лимит частоты':''):'Персонаж выберет свой срок.');
 $('pending').classList.toggle('hidden',!state.pending.length);
 if(state.pending.length){const first=state.pending[0];set('pending',first.due?'Следующая часть через '+duration(first.due-Date.now()/1000):'Готовятся отдельные сообщения · '+state.pending.length);}
}
function render(){
 const titles={idle:'Занимается своим',generating:state.current?.category==='search'?'Пошёл искать':'Сейчас в движении',paused:'Инициатива на паузе',budget:'Лимит этого запуска достигнут',stopping:'Завершает работу'};
 set('runtime-title',titles[state.mode]||'Гарик локально');
 set('runtime-copy',state.test_backend?'Тестовый исполнитель · синтетические данные.':'Вкладку можно закрыть. Гарик продолжит в пределах лимитов.');
 set('model',state.model||'Модель CLI · не указана');
 const active=state.activities.find(x=>['active','waiting'].includes(x.status)),recent=active||state.activities[0];
 set('activity',recent?.title||'Выбирает своё дело');set('next-step',recent?.next_step||'Можно исследовать, записать что-нибудь или спокойно ждать.');
 $('pause').textContent=state.autonomy==='на паузе'?'Возобновить':'Пауза инициативы';
 $('typing').classList.toggle('hidden',!state.current);set('typing',state.current?.category==='search'?'Ищет источник…':state.current?.category==='wake'?'Занимается своим':'Пишет…');
 set('process-info','PID '+state.pid+' · запуск '+date(state.started)+' · localhost');
 set('connection','На связи');$('connection').classList.remove('muted');
 renderMessages();renderFeed();renderUsage();renderContext();renderChannels();clocks();
}
async function sendPending(){
 if(!pendingSend||sending||!connected)return;
 if(pendingSend.database_id&&pendingSend.database_id!==state.database_id){banner('Открыта другая база. Черновик сохранён и автоматически не отправляется.');return;}
 sending=true;$('send').disabled=true;
 try{await post('/api/message',{id:pendingSend.id,text:pendingSend.text});pendingSend=null;localStorage.removeItem(storageKey);banner('');}
 catch{banner('Отправка пока не подтверждена. При восстановлении связи повторим тот же ID, без дубля.');}
 finally{sending=false;$('send').disabled=false;}
}
async function connect(){
 while(true){
  try{
   const response=await fetch('/api/state'+(revision===null?'':'?since='+revision),{cache:'no-store'});
   if(response.status===401){await fetch('/session',{cache:'no-store'});revision=null;continue;}
   if(!response.ok)throw Error();
   const value=await response.json();connected=true;
   if(value.revision!==revision||value.instance!==state?.instance){state=value;revision=value.revision;render();}
   if(pendingSend)await sendPending();
  }catch{connected=false;set('connection','Связь прервана');$('connection').classList.add('muted');banner('Панель переподключается. Закрытие вкладки не останавливает runtime. Если процесс остановлен, запусти garik daemon start.');revision=null;await new Promise(resolve=>setTimeout(resolve,1500));}
 }
}
$('chat-form').addEventListener('submit',event=>{event.preventDefault();const text=$('message-input').value.trim();if(!text||!state)return;if(pendingSend){banner('Сначала подтвердим предыдущую отправку.');return;}pendingSend={id:crypto.randomUUID(),text,database_id:state.database_id};localStorage.setItem(storageKey,JSON.stringify(pendingSend));$('message-input').value='';sendPending();});
$('message-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('chat-form').requestSubmit();}});
for(const [id,action] of [['cancel','cancel'],['stop','stop'],['pause',null]])$(id).addEventListener('click',async()=>{if(!connected)return;const selected=action||(state.autonomy==='на паузе'?'resume':'pause');if(selected==='stop'&&!confirm('Остановить фоновый runtime? Память и локальный чат сохранятся.'))return;try{await post('/api/action',{action:selected});if(selected==='stop')banner('Остановка запрошена. Новый запуск — garik daemon start.');}catch{banner('Команда пока не подтверждена. Проверь связь.');}});
for(const button of document.querySelectorAll('[data-page]'))button.addEventListener('click',()=>{for(const n of document.querySelectorAll('[data-page]'))n.classList.toggle('active',n===button);for(const p of document.querySelectorAll('.page'))p.classList.toggle('hidden',p.id!=='page-'+button.dataset.page);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state)renderMessages();});
setInterval(clocks,1000);connect();
