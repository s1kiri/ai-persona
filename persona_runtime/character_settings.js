'use strict';
// Each character keeps its own draft. Polling and switching never retarget a save.
globalThis.CharacterSettings=class {
 constructor({tabs,narrator,editor,save}){
  Object.assign(this,{tabs,narrator,editor,save});this.selected='narrator';this.entries=new Map();this.pending=false;
 }
 node(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.appendChild(n);return n;}
 update(actors){
  this.actors=actors||[];
  const signature=JSON.stringify(this.actors.map(a=>[a.id,a.name]));
  if(signature!==this.signature){
   this.signature=signature;this.tabs.replaceChildren();this.buttons=new Map();
   this.tabs.setAttribute('role','tablist');this.tabs.setAttribute('aria-label','Настройки повествователя и персонажей');
   for(const actor of [{id:'narrator',name:'Повествователь'},...this.actors]){
    const b=this.node('button',actor.name,this.tabs);b.type='button';b.setAttribute('role','tab');
    b.addEventListener('click',()=>this.select(actor.id));
    b.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
     const ids=[...this.buttons.keys()],i=ids.indexOf(this.selected);
     const index=e.key==='Home'?0:e.key==='End'?ids.length-1:(i+(e.key==='ArrowRight'?1:-1)+ids.length)%ids.length;
     e.preventDefault();this.select(ids[index]);this.buttons.get(ids[index]).focus();});
    this.buttons.set(actor.id,b);
   }
  }
  for(const actor of this.actors){
   let entry=this.entries.get(actor.id);
   if(!entry){entry=this.create(actor);this.entries.set(actor.id,entry);}
   entry.actor=actor;
   entry.description.textContent=actor.description||'Темп жизни, публикации и голос этого персонажа.';
   entry.model.textContent=actor.model?`Сейчас: ${actor.model} · reasoning ${actor.reasoning_effort||'по настройкам запуска'}`:'Модель будет определена при запуске.';
   entry.link.hidden=!actor.panel_url;if(actor.panel_url)entry.link.href=actor.panel_url;
   entry.controlsForm.hidden=!actor.controls;
   if(actor.controls&&!entry.controlDirty){for(const [key,input] of entry.inputs){if(key==='photos')input.checked=actor.controls[key];else input.value=actor.controls[key];}this.labels(entry);}
   entry.profileForm.hidden=!actor.profile;
   if(actor.profile&&!entry.profileDirty){entry.persona.value=actor.profile.persona;entry.modelInput.value=actor.profile.model||actor.model||'';entry.effort.value=actor.profile.reasoning_effort||actor.reasoning_effort||'low';entry.authorDigest=actor.profile.digest;}
   entry.profileHint.textContent=actor.profile?.hint||'';
   entry.reset.disabled=this.pending;
   this.enabled(entry);
  }
  if(this.selected!=='narrator'&&!this.actors.some(a=>a.id===this.selected))this.selected='narrator';
  this.select(this.selected);
 }
 select(id){
  this.selected=id;this.narrator.hidden=id!=='narrator';this.editor.hidden=id==='narrator';
  for(const [key,b] of this.buttons||[]){b.setAttribute('aria-selected',String(id===key));b.tabIndex=id===key?0:-1;}
  for(const [key,e] of this.entries)e.root.hidden=key!==id;
 }
 labels(e){for(const [key,output] of e.outputs){const n=e.inputs.get(key).value;output.textContent=key==='sharing_appetite'?n+' / 100':key==='visit_turns'?n:n+' мин';}}
 enabled(e){
  const active=e.actor.editable&&!this.pending;
  for(const input of e.inputs.values())input.disabled=!active;
  e.controlSave.disabled=!active||!e.controlDirty;
  e.profileSave.disabled=this.pending||!e.actor.profile?.can_save||!e.profileDirty;
  for(const input of [e.persona,e.modelInput,e.effort])input.disabled=this.pending;
 }
 create(actor){
  const e={actor,inputs:new Map(),outputs:new Map(),controlDirty:false,profileDirty:false};
  e.root=this.node('section',undefined,this.editor);e.root.className='character-editor';
  this.node('h3',actor.name,e.root);e.description=this.node('p','',e.root);e.model=this.node('p','',e.root);
  e.link=this.node('a','Канал, память и восстановление →',e.root);
  e.message=this.node('p','',e.root);e.message.setAttribute('role','status');
  e.controlsForm=this.node('form',undefined,e.root);
  const fields=[['wake_minutes','Пауза между эпизодами',5,90,1],['episode_minutes','Ориентир длины занятия',5,90,1],
   ['sharing_appetite','Готовность делиться',0,100,1],['visit_turns','Ответов за один заход',1,6,1],['away_minutes','Пауза между заходами в комментарии',1,30,1]];
  for(const [key,title,min,max,step] of fields){const label=this.node('label',title,e.controlsForm);label.className='slider';
   const output=this.node('output','',label),input=this.node('input',undefined,label);input.type='range';input.setAttribute('aria-label',title);Object.assign(input,{min,max,step});
   input.addEventListener('input',()=>{e.controlDirty=true;this.labels(e);this.enabled(e);});e.inputs.set(key,input);e.outputs.set(key,output);}
  const label=this.node('label',undefined,e.controlsForm),photo=this.node('input',undefined,label);photo.type='checkbox';this.node('span',' Может выбирать фотографии',label);e.inputs.set('photos',photo);
  photo.addEventListener('change',()=>{e.controlDirty=true;this.enabled(e);});
  this.node('p','Темп — ориентир. Решения о постах добровольные; ответы в комментариях рассматриваются отдельно.',e.controlsForm);
  e.controlSave=this.node('button','Сохранить темп жизни',e.controlsForm);e.controlSave.type='submit';
  e.controlsForm.addEventListener('submit',event=>{event.preventDefault();const controls=Object.fromEntries([...e.inputs].map(([key,input])=>[key,key==='photos'?input.checked:Number(input.value)]));return this.submit(e,'controls',controls);});
  e.profileForm=this.node('form',undefined,e.root);this.node('h3','Характер и модель',e.profileForm);
  e.profileHint=this.node('p','',e.profileForm);
  const ml=this.node('label','ID модели',e.profileForm);e.modelInput=this.node('input',undefined,ml);e.modelInput.type='text';e.modelInput.maxLength=100;e.modelInput.required=true;e.modelInput.autocomplete='off';
  this.node('p','Укажите доступный вашему аккаунту ID. Доступ проверится при следующем вызове; скрытой замены модели нет.',e.profileForm);
  const rl=this.node('label','Reasoning',e.profileForm);e.effort=this.node('select',undefined,rl);
  e.effort.setAttribute('aria-label','Reasoning');
  for(const value of ['none','minimal','low','medium','high','xhigh','max']){const option=this.node('option',value,e.effort);option.value=value;}
  const pl=this.node('label','Характер, жизнь и голос',e.profileForm);e.persona=this.node('textarea',undefined,pl);e.persona.rows=18;e.persona.required=true;e.persona.maxLength=100000;e.persona.spellcheck=false;
  this.node('p','Это авторское описание персонажа. Прожитая история, память читателей и результаты действий сохраняются отдельно.',e.profileForm);
  for(const input of [e.modelInput,e.effort,e.persona])input.addEventListener('input',()=>{e.profileDirty=true;this.enabled(e);});
  e.profileSave=this.node('button','Сохранить характер и модель',e.profileForm);e.profileSave.type='submit';
  e.reset=this.node('button','Загрузить сохранённое',e.profileForm);e.reset.type='button';e.reset.addEventListener('click',()=>{e.profileDirty=false;this.update(this.actors);});
  e.profileForm.addEventListener('submit',event=>{event.preventDefault();return this.submit(e,'profile',{digest:e.authorDigest,persona:e.persona.value,model:e.modelInput.value.trim(),reasoning_effort:e.effort.value});});
  return e;
 }
 async submit(e,kind,body){
  if(this.pending||!e.actor.editable||kind==='profile'&&!e.actor.profile?.can_save)return;
  const actorId=e.actor.id;this.pending=true;e.message.textContent='Сохраняем…';this.update(this.actors);
  try{await this.save(actorId,kind,body);if(kind==='controls')e.controlDirty=false;else e.profileDirty=false;
   e.message.textContent=kind==='profile'?'Авторская версия сохранена с резервной копией. Жизнь остаётся на паузе.':'Темп жизни сохранён.';
  }catch(error){e.message.textContent=error.message||'Нет подтверждения сохранения. Повтора не было.';}
  finally{this.pending=false;this.update(this.actors);}
 }
};
