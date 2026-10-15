'use strict';
const $=id=>document.getElementById(id);
let state=null,busy=false,candidateSignature='';
function notice(text){$('notice').textContent=text||'';$('notice').hidden=!text;}
function render(value){
 state=value;
 $('bot-status').textContent=value.bot?`Подключён @${value.bot.username||value.bot.first_name}.`:'Токен ещё не сохранён.';
 $('token-form').hidden=value.configured;
 $('save-token').disabled=busy;
 $('bind').disabled=busy||!value.configured;
 $('channel-status').textContent=value.binding?'Канал и обсуждение проверены.':value.configured?'Выбери канал. Проверим привязку обсуждения и права бота.':'Сначала подключи бота.';
 const signature=JSON.stringify(value.candidates);
 if(signature!==candidateSignature){
  candidateSignature=signature;const selected=$('channels').value;$('channels').replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent='Выбери канал';$('channels').append(empty);
  for(const chat of value.candidates){const option=document.createElement('option');option.value=String(chat.id);option.textContent=chat.title||chat.username||String(chat.id);$('channels').append(option);}
  $('channels').value=selected;
  if(value.candidates.length===1&&!$('channel').value){$('channels').value=String(value.candidates[0].id);$('channel').value=String(value.candidates[0].id);}
 }
 $('channel-form').hidden=!!value.binding;
 $('binding').hidden=!value.binding;
 if(value.binding)$('binding').textContent=`Канал: ${value.binding.channel.title}\nОбсуждение: ${value.binding.discussion.title}\nПрава на посты и чтение комментариев есть.`;
 const test=value.outbox.find(item=>item.key==='setup-text-v1');
 $('test-post').disabled=busy||!value.binding||!!test;
 $('test-post').textContent=test?.status==='sent'?'Проверочный пост доставлен':'Отправить проверочный пост в канал';
 $('delivery-status').textContent=test?(test.status==='sent'?'Теперь оставь комментарий под проверочным постом в Telegram.':test.error||'Ждём квитанцию. Повтор не отправляется.'):'Публикации ещё не отправлялись.';
 $('messages').replaceChildren();
 for(const message of value.messages.filter(m=>m.kind==='comment'||m.kind==='own').slice(0,15)){
  const card=document.createElement('article');card.className='message';
  const label=document.createElement('small');label.textContent=`${message.speaker} · ${message.kind==='own'?'ответ бота':'комментарий'} · #${message.id}`;
  const text=document.createElement('p');text.textContent=message.text||'[Сообщение без текста]';card.append(label,text);
  if(message.kind==='comment'){
   const key='setup-reply-'+message.id,delivery=value.outbox.find(item=>item.key===key);
   const button=document.createElement('button');button.type='button';button.disabled=busy||!!delivery;button.textContent=delivery?.status==='sent'?'Проверочный ответ доставлен':delivery?'Отправка требует проверки':'Отправить технический ответ на этот комментарий';
   button.addEventListener('click',()=>post('/api/test-reply',{comment_id:message.id}));card.append(button);
  }
  $('messages').append(card);
 }
 if(value.error)notice(value.error);
}
async function refresh(){
 if(busy)return;
 try{const response=await fetch('/api/state',{cache:'no-store'});const value=await response.json();if(!response.ok)throw Error(value.error);render(value);}
 catch(error){notice('Не удалось обновить подключение. Открой страницу заново.');}
}
async function post(path,value){
 if(busy||!state)return;
 busy=true;render(state);notice('');
 try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-CSRF':state.csrf},body:JSON.stringify(value)});const result=await response.json();if(!response.ok)throw Error(result.error);render(result);}
 catch(error){notice(error.message||'Связь оборвалась. Повтор автоматически не отправляется.');}
 finally{busy=false;render(state);}
}
$('token-form').addEventListener('submit',event=>{event.preventDefault();const token=$('token').value.trim();$('token').value='';return post('/api/token',{token});});
$('channels').addEventListener('change',()=>{$('channel').value=$('channels').value;});
$('channel-form').addEventListener('submit',event=>{event.preventDefault();return post('/api/bind',{channel:$('channel').value.trim()});});
$('test-post').addEventListener('click',()=>post('/api/test-post',{}));
refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
