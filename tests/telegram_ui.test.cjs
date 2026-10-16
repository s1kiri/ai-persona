// Local setup controller only; no browser, credentials, Telegram or model.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){
 const elements=new Map(),requests=[];
 function element(){return {textContent:'',value:'',disabled:false,hidden:false,children:[],handlers:{},append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},addEventListener(k,f){this.handlers[k]=f}};}
 const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element};
 const ctx=vm.createContext({document,setInterval(){},fetch(path,options){if(!options?.method)return new Promise(()=>{});return new Promise((resolve,reject)=>requests.push({path,options,reject,resolve:(body,ok=true)=>resolve({ok,json:async()=>body})}));}});
 vm.runInContext(fs.readFileSync('persona_runtime/telegram_panel/telegram.js','utf8'),ctx);
 const sample={csrf:'synthetic',configured:false,bot:null,binding:null,candidates:[],messages:[],outbox:[],error:null};
 ctx.sample=sample;vm.runInContext('render(sample)',ctx);
 return {ctx,sample,requests,el:id=>document.getElementById(id),run:code=>vm.runInContext(code,ctx)};
}
test('Arkady Telegram panel uses own name, memory and world port',()=>{
 const t=setup();t.run('render({...sample,world_url:"http://127.0.0.1:8866",life:{name:"Аркадий",paused:true,controls:{wake_minutes:15,photos:true},memories:[{text:"<b>origin</b>"}],concerns:[{text:"Продолжить дело.",next_hint:"Проверить запись."}]}})');
 assert.equal(t.el('character-name').textContent,'Аркадий');
 assert.match(t.el('intro').textContent,/Аркадий/);
 assert.equal(t.el('world-link').href,'http://127.0.0.1:8866/#posts');
 assert.equal(t.el('origin-memory').children[0].textContent,'<b>origin</b>');
 assert.equal(t.el('origin-section').hidden,false);
});
test('token is cleared before sending and is not reinserted after a failed request',async()=>{
 const t=setup();t.el('token').value='synthetic-sensitive-value';
 const promise=t.el('token-form').handlers.submit({preventDefault(){}});
 assert.equal(t.el('token').value,'');assert.equal(t.el('save-token').disabled,true);
 assert.equal(t.requests[0].path,'/api/token');assert.equal(JSON.parse(t.requests[0].options.body).token,'synthetic-sensitive-value');
 t.requests[0].resolve({error:'Synthetic failure'},false);await promise;
 assert.equal(t.el('token').value,'');assert.equal(t.requests.length,1);assert.equal(t.el('save-token').disabled,false);
 assert.match(t.el('notice').textContent,/Synthetic failure/);
});
test('polling preserves manual channel selection and malicious titles remain literal',()=>{
 const t=setup();t.el('channel').value='@manual_channel';
 t.run('render({...sample,configured:true,candidates:[{id:-10001,title:"<img onerror=alert(1)>"}]})');
 assert.equal(t.el('channel').value,'@manual_channel');
 assert.equal(t.el('channels').children[1].textContent,'<img onerror=alert(1)>');
});
test('no automatic posts; double click and failed send do not retry',async()=>{
 const t=setup();t.run('render({...sample,configured:true,binding:{channel:{title:"Test"},discussion:{title:"Test comments"}}})');
 assert.equal(t.requests.length,0);
 const promise=t.el('test-post').handlers.click();await t.el('test-post').handlers.click();
 assert.equal(t.requests.length,1);t.requests[0].reject(Error('Synthetic disconnection'));await promise;
 assert.equal(t.requests.length,1);
});
test('delivered test remains disabled and comments render as literal text',()=>{
 const t=setup();t.run('render({...sample,configured:true,binding:{channel:{title:"Test"},discussion:{title:"Comments"}},outbox:[{key:"setup-text-v1",status:"sent"}],messages:[{id:51,kind:"comment",speaker:"<script>Reader</script>",text:"<b>Comment</b>"}]})');
 assert.equal(t.el('test-post').disabled,true);
 assert.equal(t.el('messages').children[0].children[1].textContent,'<b>Comment</b>');
 assert.match(t.el('messages').children[0].children[0].textContent,/<script>Reader<\/script>/);
});
test('live panel preserves unsaved pace and does not offer technical replies',()=>{
 const t=setup();t.ctx.live={status:'running',paused:false,busy:false,controls:{wake_minutes:15,photos:true},world_calls:1,phase:'waiting',awake:null,error:null};
 t.run('render({...sample,configured:true,life:live,messages:[{id:51,kind:"comment",speaker:"Reader",text:"Hello"}]})');
 assert.equal(t.el('life-section').hidden,false);assert.equal(t.el('test-post').hidden,true);
 assert.equal(t.el('messages').children[0].children.length,2);
 t.el('wake').value='30';t.el('wake').handlers.input();
 t.run('render({...sample,configured:true,life:{...live,controls:{wake_minutes:45,photos:true}}})');
 assert.equal(t.el('wake').value,'30');assert.equal(t.el('life-save').disabled,false);
 assert.equal(t.requests.length,0);
});
test('reader impressions and intentions remain literal and are separate from automatic replies',()=>{
 const t=setup();t.ctx.live={status:'running',paused:false,controls:{wake_minutes:15,photos:true,visit_turns:3,away_minutes:5},world_calls:1,phase:'away',awake:{active:true,until:null},error:null,
  reader_memory:[{name:'<img onerror=alert(1)>',impression:'<script>impression</script>',facts:[{text:'<b>reported</b>'}],anonymous:false}],own_intentions:[{status:'pending',kind:'life',action:'Eat soup'}]};
 t.run('render({...sample,configured:true,life:live})');
 assert.equal(t.el('reader-memory-section').hidden,false);
 assert.equal(t.el('reader-memory').children[0].children[0].textContent,'<img onerror=alert(1)>');
 assert.equal(t.el('reader-memory').children[0].children[1].textContent,'<script>impression</script>');
 assert.equal(t.el('personal-intentions').children[0].children[1].textContent,'Eat soup');
 assert.match(t.el('awake-status').textContent,/пока работает процесс/);
 assert.equal(t.requests.length,0);
});
test('attention slider saves independently of post timing and polling preserves edits',async()=>{
 const t=setup();t.ctx.live={paused:false,controls:{wake_minutes:15,photos:true,visit_turns:3,away_minutes:5},world_calls:1,phase:'waiting',awake:null,error:null,reader_memory:[],own_intentions:[]};
 t.run('render({...sample,configured:true,life:live})');
 t.el('visit-turns').value='2';t.el('visit-turns').handlers.input();
 t.run('render({...sample,configured:true,life:{...live,controls:{...live.controls,visit_turns:6}}})');
 assert.equal(t.el('visit-turns').value,'2');
 const promise=t.el('life-form').handlers.submit({preventDefault(){}});
 const body=JSON.parse(t.requests[0].options.body);assert.equal(body.visit_turns,2);assert.equal(body.away_minutes,5);assert.equal(body.post_every,undefined);
 t.requests[0].resolve({...t.sample,life:t.ctx.live});await promise;
});
test('episode length is saved as a soft life control and survives polling while edited',async()=>{
 const t=setup();t.ctx.live={paused:false,controls:{wake_minutes:5,episode_minutes:15,photos:true,visit_turns:3,away_minutes:5},world_calls:1,phase:'living',awake:null,error:null,reader_memory:[],own_intentions:[]};
 t.run('render({...sample,configured:true,life:live})');
 t.el('episode-minutes').value='10';t.el('episode-minutes').handlers.input();
 t.run('render({...sample,configured:true,life:live})');assert.equal(t.el('episode-minutes').value,'10');
 const promise=t.el('life-form').handlers.submit({preventDefault(){}});
 const body=JSON.parse(t.requests[0].options.body);assert.equal(body.episode_minutes,10);assert.equal(body.wake_minutes,5);assert.equal(body.post_every,undefined);
 t.requests[0].resolve({...t.sample,life:t.ctx.live});await promise;
});
test('sharing appetite is independent from episode length and never defines a post quota',async()=>{
 const t=setup();t.ctx.live={paused:false,controls:{wake_minutes:5,episode_minutes:15,sharing_appetite:50,photos:true,visit_turns:3,away_minutes:5},world_calls:1,phase:'waiting',awake:null,error:null,reader_memory:[],own_intentions:[]};
 t.run('render({...sample,configured:true,life:live})');
 t.el('sharing-appetite').value='80';t.el('sharing-appetite').handlers.input();t.run('render({...sample,configured:true,life:live})');
 assert.equal(t.el('sharing-appetite').value,'80');
 const promise=t.el('life-form').handlers.submit({preventDefault(){}});const body=JSON.parse(t.requests[0].options.body);
 assert.equal(body.sharing_appetite,80);assert.equal(body.episode_minutes,15);assert.equal(body.post_every,undefined);
 t.requests[0].resolve({...t.sample,life:t.ctx.live});await promise;
});
