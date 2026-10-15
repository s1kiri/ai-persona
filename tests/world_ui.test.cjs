// Synthetic controller state only. No browser, server or model connection.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){
 const elements=new Map(),posts=[];
 function element(){return {textContent:'',className:'',value:'',disabled:false,children:[],handlers:{},classList:{add(){},remove(){},toggle(){}},append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},addEventListener(k,f){this.handlers[k]=f}};}
 const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element};
 const ctx=vm.createContext({document,console,Intl,Date,setInterval(){},fetch(path,options){if(!options?.method)return new Promise(()=>{});return new Promise((resolve,reject)=>posts.push({path,options,reject,resolve:(value,ok=true)=>resolve({ok,json:async()=>value})}));}});
 vm.runInContext(fs.readFileSync('persona_runtime/world_panel/world.js','utf8'),ctx);
 const sample={csrf:'synthetic',model_enabled:false,paused:true,busy:false,failure:null,minute:0,settings_revision:0,settings:{events_per_hour:2,surprise:45,continuity:70,quiet:30,max_threads:4,post_cooldown_minutes:90,director_empathy:50},actors:[{id:'garik',name:'Гарик'}],threads:[],entities:[],history:[],notes:[],cycles:[],calls:[],call_count:0,max_calls:24};
 ctx.sample=sample;vm.runInContext('render(sample)',ctx);
 return {ctx,sample,posts,el:id=>document.getElementById(id),run:code=>vm.runInContext(code,ctx)};
}
test('preparation panel clearly disables model stage and updates slider labels',()=>{
 const t=setup();assert.equal(t.el('step').disabled,true);assert.match(t.el('mode').textContent,/без модели/);
 t.el('surprise').value='15';t.el('surprise').handlers.input();
 assert.equal(t.el('surprise-value').textContent,'15');assert.equal(t.el('save-settings').disabled,false);
});
test('polling does not overwrite unsaved slider and saves with its original revision',async()=>{
 const t=setup();t.el('surprise').value='15';t.el('surprise').handlers.input();
 t.run('render({...sample,settings_revision:1,settings:{...sample.settings,surprise:90}})');
 assert.equal(t.el('surprise').value,'15');
 const promise=t.el('settings-form').handlers.submit({preventDefault(){}});
 const body=JSON.parse(t.posts[0].options.body);assert.equal(body.revision,0);assert.equal(body.settings.surprise,15);
 t.posts[0].resolve({error:'Conflict'},false);await promise;
 assert.equal(t.el('surprise').value,'15');assert.equal(t.run('dirty'),true);
});
test('successful slider save adopts current persisted controls',async()=>{
 const t=setup();t.el('surprise').value='15';t.el('surprise').handlers.input();
 const promise=t.el('settings-form').handlers.submit({preventDefault(){}});
 t.posts[0].resolve({...t.sample,settings_revision:1,settings:{...t.sample.settings,surprise:15}});await promise;
 assert.equal(t.run('dirty'),false);assert.equal(t.el('save-settings').disabled,true);assert.equal(t.run('settingsRevision'),1);
});
test('double click and lost reply never trigger automatic repeat',async()=>{
 const t=setup();t.el('minutes').value='30';
 const promise=t.el('prepare').handlers.click();await t.el('prepare').handlers.click();assert.equal(t.posts.length,1);
 t.posts[0].reject(Error('Synthetic disconnect'));await promise;
 assert.equal(t.posts.length,1);assert.match(t.el('banner').textContent,/Synthetic disconnect/);
});
test('model-written markup remains literal text in the monitor',()=>{
 const t=setup();const text='<img src=x onerror=alert(1)>';
 t.ctx.markup=text;t.run('render({...sample,entities:[{name:markup,condition:markup}]})');
 const card=t.el('entities').children[0];assert.equal(card.children[0].textContent,text);assert.equal(card.children[1].textContent,text);
});
test('autonomous drafts and personal reactions are rendered as literal text',()=>{
 const t=setup();const text='<script>synthetic</script>';
 t.ctx.markup=text;
 t.run('render({...sample,autonomous_posts:true,posts:[{minute:90,value:{text:markup}}],undertakings:[{status:"active",want:markup}],cycles:[{status:"complete",value:{results:{choice:{desire:"Want",action:"Act",reaction:markup,note:""}}}}]})');
 assert.equal(t.el('post-feed').children[0].children[1].textContent,text);
 assert.equal(t.el('undertakings').children[0].children[1].textContent,text);
 assert.equal(t.el('reaction').textContent,text);
 assert.match(t.el('posts-mode').textContent,/сам выбирает/);
});

test('personal matters and director signal stay literal and distinct in the console',()=>{
 const t=setup();const text='<script>private</script>';t.ctx.markup=text;
 t.run('render({...sample,personal_life:true,concerns:[{text:markup,stake:markup,next_hint:markup,status:"active"}],director_signals:[{signal:"Brief interest"}]})');
 assert.equal(t.el('concerns').children[0].children[1].textContent,text);
 assert.equal(t.el('director-signals').children[0].textContent,'Brief interest');
 assert.match(t.el('cycle-flow').textContent,/Своё намерение/);
});

test('interrupted choices show missing outcome and preserve full original intention',()=>{
 const t=setup();const text='Ask for meat. If sold, bring it home and try it with apples.';t.ctx.intent=text;
 t.run('render({...sample,history:[{minute:1333,stage:"choice",execution:"interrupted",value:{action:"Ask for meat."}}],unfinished_attempts:[{action:"Ask for meat.",written_intent:intent}]})');
 assert.match(t.el('timeline').children[0].children[0].textContent,/Исхода нет.*прерван/);
 assert.equal(t.el('unfinished-attempts').children[0].children[1].textContent,text);
});

test('fictional reader comments and Garik replies stay attached and literal',()=>{
 const t=setup();t.run('render({...sample,posts:[{id:"reply",minute:60,value:{text:"<b>reply</b>",reply_to:"root",audience_id:"aud"}},{id:"root",minute:30,value:{text:"First fragment"}}],audience:[{id:"aud",minute:45,value:{post_id:"root",comments:[{speaker:"Reader",text:"<script>claim</script>"}]}}]})');
 assert.equal(t.el('post-feed').children.length,1);
 const discussion=t.el('post-feed').children[0].children[2];
 assert.match(discussion.children[0].textContent,/Вымышленные комментарии/);
 assert.equal(discussion.children[1].textContent,'Reader: <script>claim</script>');
 assert.equal(discussion.children[2].textContent,'Гарик: <b>reply</b>');
});

test('direct channel decisions show the exact text instead of a missing summary',()=>{
 const t=setup();t.run('render({...sample,autonomous_posts:true,direct_channel:true,history:[{minute:30,stage:"share",value:{decision:"post",text:"блять",source_ids:[]}}]})');
 assert.equal(t.el('timeline').children[0].children[1].textContent,'Хочу рассказать: блять');
 assert.match(t.el('posts-mode').textContent,/без переписывания/);
});
test('live Telegram life shows real publication mode and disables manual cycle mutations',()=>{
 const t=setup();t.run('render({...sample,model_enabled:true,max_calls:null,channel_life:{enabled:true},paused:false,call_count:230})');
 assert.equal(t.el('call-count').textContent,'230');assert.equal(t.el('prepare').disabled,true);
 assert.equal(t.el('night-start').disabled,true);assert.match(t.el('scope').textContent,/публикации/);
});
