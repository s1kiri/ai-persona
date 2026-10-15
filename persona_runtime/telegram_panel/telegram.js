'use strict';
const $=id=>document.getElementById(id);
let state=null,busy=false,candidateSignature='',lifeDirty=false;
function notice(text){$('notice').textContent=text||'';$('notice').hidden=!text;}
function render(value){
 state=value;
 $('life-section').hidden=!value.life;
 if(value.life){
  const life=value.life;
  $('intro').textContent='Гарик живёт, сохраняет память и сам выбирает, чем поделиться. Графика постов нет.';
  $('life-status').textContent=life.error?'Остановлен · нужна проверка':life.paused?'На паузе':'Живёт самостоятельно';
  const phases={share:'Решает, хочется ли написать',horizon:'Что затевают жители',world:'Замечает обстоятельства',impulse:'Чего хочется сейчас',choice:'Выбирает поступок',outcome:'Что получилось',experience:'Что осталось важным',research:'Ищет и читает',photo_choice:'Выбирает фотографию',delivery:'Отправляет выбранное',living:'Занят своим делом',waiting:'Между эпизодами'};
  $('life-detail').textContent=life.error||`${phases[life.phase]||'Готовится'} · вызовов жизни: ${life.world_calls}`;
  if(!lifeDirty){$('wake').value=life.controls.wake_minutes;$('photos').checked=life.controls.photos;}
  $('wake-value').textContent=$('wake').value+' мин';
  $('life-save').disabled=busy||!lifeDirty;
  $('life-action').disabled=busy||!!life.error;$('life-action').textContent=life.paused?'Продолжить жизнь':'Пауза после текущего этапа';
  $('awake-status').textContent=life.awake?.active?(life.awake.until===null?'Mac защищён от сна, пока работает процесс Гарика. Блокировка экрана допустима.':`Mac защищён от сна до ${new Date(life.awake.until*1000).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})} МСК. Блокировка экрана допустима.`):'Защита от сна сейчас не действует.';
  $('messages-title').textContent='Реальные комментарии';$('messages-intro').textContent='Комментарии под новыми постами попадают в память вместе с исходной записью. Отвечать на каждый не обязательно.';
  $('test-post').hidden=true;$('delivery-status').hidden=true;$('footer').textContent='Пульс мира показывает мысли, поступки, истории и дословные публикации. Ошибки и неизвестные доставки останавливаются без автоматического повтора.';
 }
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
  if(message.kind==='comment'&&!value.life){
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
 try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-CSRF':state.csrf},body:JSON.stringify(value)});const result=await response.json();if(!response.ok)throw Error(result.error);render(result);return true;}
 catch(error){notice(error.message||'Связь оборвалась. Повтор автоматически не отправляется.');}
 finally{busy=false;render(state);}
}
$('token-form').addEventListener('submit',event=>{event.preventDefault();const token=$('token').value.trim();$('token').value='';return post('/api/token',{token});});
$('channels').addEventListener('change',()=>{$('channel').value=$('channels').value;});
$('channel-form').addEventListener('submit',event=>{event.preventDefault();return post('/api/bind',{channel:$('channel').value.trim()});});
$('test-post').addEventListener('click',()=>post('/api/test-post',{}));
$('wake').addEventListener('input',()=>{lifeDirty=true;$('wake-value').textContent=$('wake').value+' мин';$('life-save').disabled=false;});
$('photos').addEventListener('change',()=>{lifeDirty=true;$('life-save').disabled=false;});
$('life-form').addEventListener('submit',async event=>{event.preventDefault();if(await post('/api/life/settings',{wake_minutes:Number($('wake').value),photos:$('photos').checked})){lifeDirty=false;render(state);}});
$('life-action').addEventListener('click',()=>post('/api/life/action',{running:state.life.paused}));
refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
