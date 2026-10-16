'use strict';
let state=null, sending=false;
const $=id=>document.getElementById(id);
const settingFields=['events_per_hour','surprise','continuity','quiet','max_threads','post_cooldown_minutes','director_empathy'];
let settingsDirty=false,settingsRevision=null;
const characterSettings=globalThis.CharacterSettings?new globalThis.CharacterSettings({
 tabs:$('settings-tabs'),narrator:$('narrator-settings'),editor:$('character-settings'),
 save:(actor_id,kind,value)=>saveSettings(kind==='profile'?'/api/character-profile':'/api/character-settings',{actor_id,[kind]:value})}):null;
function settingsLabels(){for(const key of settingFields)$(key+'-value').textContent=$(key).value;}
function line(parent,text,cls){const p=document.createElement('p');p.textContent=text;if(cls)p.className=cls;parent.appendChild(p);return p;}
function render(value){
 state=value;
 $('queue-world-event').disabled=sending;
 $('world-event-text').disabled=sending;
 $('world-events').replaceChildren();
 const eventStatuses={queued:'В очереди',started:'Мир разыгрывает событие',applied:'Произошло',declined:'Не применено',failed:'Сбой · повтор выключен',cancelled:'Отменено'};
 for(const event of value.world_events||[]){
  const li=document.createElement('li');line(li,event.text);line(li,eventStatuses[event.status]||event.status,'muted');
  if(event.result?.summary)line(li,event.result.summary);
  if(event.result?.reason)line(li,event.result.reason,'muted');
  for(const observation of event.result?.observations||[]){const name=value.actors.find(a=>a.id===observation.actor_id)?.name||observation.actor_id;line(li,`${name}: ${observation.via==='unaware'?'пока не узнал':observation.via==='news'?'узнал из сообщения':'наблюдает'}${observation.observation?' · '+observation.observation:''}`,'muted');}
  if(event.error)line(li,event.error);
  if(event.status==='queued'){const button=document.createElement('button');button.textContent='Отменить';button.disabled=sending;button.addEventListener('click',()=>cancelWorldEvent(event.id));li.appendChild(button);}
  $('world-events').appendChild(li);
 }
 if(value.settings&&!settingsDirty){for(const key of settingFields)$(key).value=value.settings[key];settingsRevision=value.settings_revision;settingsLabels();}
 $('save-settings').disabled=sending||!settingsDirty;
 characterSettings?.update(value.actors.map(a=>({...a,editable:true,description:value.live?'Настройки собственного канала и темпа жизни.':'Подготовка отдельного профиля. Рабочая жизнь и её история сюда ещё не перенесены.'})));
 if(value.error)$('error').textContent=value.error;
 $('mode').textContent=value.live?'Совместная жизнь · каждый выбирает свои слова и дела':'Подготовка · модельные вызовы и автономные публикации выключены. Настройте обоих ботов, затем запустите общий хост с --live.';
 const m=value.minute;$('clock').textContent=`День ${Math.floor(m/1440)+1} · ${String(Math.floor(m%1440/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
 $('actors').replaceChildren();
 for(const actor of value.actors){
  const card=document.createElement('article');card.className='actor';const title=document.createElement('h2');title.textContent=actor.name;card.appendChild(title);
  const life=actor.life;line(card,life?(life.paused?'На паузе':'Живёт'):(actor.configured?'Telegram настроен':'Telegram нужно настроить'));
  if(actor.model)line(card,`${actor.model} · reasoning ${actor.reasoning_effort||'по умолчанию'}`,'muted');
  line(card,`Исходных воспоминаний: ${actor.memories}`,'muted');
  if(life?.error)line(card,life.error);
  if(actor.peer_context.ongoing_action)line(card,`Сейчас: ${actor.peer_context.ongoing_action}`);
  if(actor.peer_context.nearby_actor_ids.length)line(card,'Сейчас находятся вместе');
  const link=document.createElement('a');link.href=actor.panel_url;link.textContent=life?'Канал, память и настройки':'Настроить Telegram';card.appendChild(link);
  const p=document.createElement('p'),button=document.createElement('button');button.textContent=life?.paused?'Продолжить':'Пауза';button.disabled=!value.live||sending||Boolean(life?.error);button.addEventListener('click',()=>action(actor.id,Boolean(life?.paused)));p.appendChild(button);card.appendChild(p);$('actors').appendChild(card);
 }
 $('cycles').replaceChildren();for(const c of value.cycles){const li=document.createElement('li');const name=value.actors.find(a=>a.id===c.actor_id)?.name||c.actor_id;li.textContent=`${name}: ${c.status==='waiting'?'занят, остальные продолжают жить':c.status==='held'?'дело сохранено до продолжения':c.phase} · ${c.status}`;$('cycles').appendChild(li);}
 if(!value.cycles.length){const li=document.createElement('li');li.textContent='Незавершённых этапов нет';$('cycles').appendChild(li);}
 $('jobs').replaceChildren();for(const job of value.peer_jobs){const li=document.createElement('li');li.textContent=`${job.sender} → ${job.recipient}: ${job.status}${job.error?' · '+job.error:''}`;if(job.value?.text){const quote=document.createElement('blockquote');quote.textContent=job.value.text;li.appendChild(quote);}if(job.status==='unknown'){const button=document.createElement('button');button.textContent='Проверил: комментария нет';button.disabled=sending;button.addEventListener('click',()=>repair(job));li.appendChild(button);}$('jobs').appendChild(li);}
}
async function refresh(){try{const r=await fetch('/api/state');if(!r.ok)throw Error('Откройте пульт заново');render(await r.json());return true;}catch(e){$('error').textContent=e.message;return false;}}
async function action(actor_id,running){if(sending||!state)return;sending=true;render(state);try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Shared-CSRF':state.csrf},body:JSON.stringify({actor_id,running})});const v=await r.json();if(!r.ok)throw Error(v.error);$('error').textContent='';}catch(e){$('error').textContent=e.message;}finally{sending=false;await refresh();}}
async function saveSettings(path,value){
 if(sending||!state)throw Error('Дождитесь текущего сохранения.');sending=true;
 try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Shared-CSRF':state.csrf},body:JSON.stringify(value)});const result=await r.json();if(!r.ok)throw Error(result.error||'Не удалось сохранить.');if(!await refresh())throw Error('Изменения приняты, но новое состояние не получено. Черновик сохранён; обновите страницу для проверки.');}
 finally{sending=false;if(state)render(state);}
}
for(const key of settingFields)$(key).addEventListener('input',()=>{settingsDirty=true;settingsLabels();$('save-settings').disabled=false;});
$('settings-form').addEventListener('submit',async e=>{e.preventDefault();if(sending)return;try{await saveSettings('/api/settings',{settings:Object.fromEntries(settingFields.map(k=>[k,Number($(k).value)])),revision:settingsRevision});settingsDirty=false;render(state);}catch(error){$('error').textContent=error.message;}});
refresh();setInterval(refresh,2000);

let worldEventDraft=null;
async function queueWorldEvent(){
 if(sending||!state)return;
 const text=($('world-event-text').value||'').trim();
 if(!text){$('world-event-status').textContent='Сначала опиши событие.';return;}
 if(!worldEventDraft||worldEventDraft.text!==text)worldEventDraft={text,request_id:globalThis.crypto?.randomUUID?.()||'event-'+Date.now()+'-'+Math.random().toString(36).slice(2)};
 $('world-event-status').textContent='Добавляем в очередь…';
 try{await saveSettings('/api/world-events',worldEventDraft);$('world-event-text').value='';worldEventDraft=null;$('world-event-status').textContent='Событие в очереди. Реакции и посты герои выбирают сами.';}
 catch(error){$('world-event-status').textContent=error.message+' Текст сохранён. Повтор с тем же текстом не создаст дубль.';}
}
$('world-event-form').addEventListener('submit',e=>{e.preventDefault();queueWorldEvent();});
async function cancelWorldEvent(id){if(sending||!state)return;try{await saveSettings('/api/world-events/cancel',{id});}catch(error){$('world-event-status').textContent=error.message;}}

async function repair(job){if(sending||!state)return;if(!window.confirm('Проверьте обсуждение получателя. Подтвердить, что этого комментария там нет, и разрешить новую попытку после продолжения автора?'))return;sending=true;render(state);try{const r=await fetch('/api/peer-confirm-absent',{method:'POST',headers:{'Content-Type':'application/json','X-Shared-CSRF':state.csrf},body:JSON.stringify({actor_id:job.sender,job_id:job.id,confirmation:'owner_checked_absent'})});const v=await r.json();if(!r.ok)throw Error(v.error);$('error').textContent='Отсутствие отмечено. Автор остаётся на паузе до вашего продолжения.';}catch(e){$('error').textContent=e.message;}finally{sending=false;await refresh();}}
