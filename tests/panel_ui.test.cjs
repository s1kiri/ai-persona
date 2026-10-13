// Offline browser-controller tests. No browser, network, credentials or model.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(saved=[]){
 const elements=new Map(),storage=new Map(saved),posts=[];
 function element(){return {textContent:'',value:'',dataset:{},disabled:false,children:[],handlers:{},classList:{toggle(){},add(){},remove(){}},append(...x){this.children.push(...x)},replaceChildren(){this.children=[]},addEventListener(k,f){this.handlers[k]=f},scrollHeight:0,scrollTop:0,clientHeight:0};}
 const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element,querySelectorAll(){return []},addEventListener(){}};
 const browserStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
 const ctx=vm.createContext({document,localStorage:browserStorage,sessionStorage:browserStorage,Intl,Date,console,crypto:{randomUUID:()=> 'synthetic-id'},confirm:()=>true,setInterval(){},setTimeout(){},fetch(path,options){if(!options?.method)return new Promise(()=>{});return new Promise((resolve,reject)=>posts.push({path,options,resolve:v=>resolve({ok:true,json:async()=>v}),reject}));}});
 vm.runInContext(fs.readFileSync('persona_runtime/panel/panel.js','utf8'),ctx);
 const totals={attempts:0,known_token_calls:0,unknown_calls:0,total_tokens:0,coverage:{}};
 const sample={instance:'first',database_id:'synthetic',csrf:'synthetic',revision:1,mode:'idle',started:1,pid:1,model:'synthetic',activities:[],pending:[],messages:[],feed:[],calls:[],sources:[],memory:[],channels:[],capabilities:[],autonomy:'выключена',limits:{max_calls:8,max_wakes:0,max_known_tokens:10000},usage:{session:totals,day:totals,all:totals}};
 ctx.sample=sample;vm.runInContext('connected=true;adopt(sample)',ctx);
 return {ctx,posts,storage,sample,el:id=>document.getElementById(id),run:code=>vm.runInContext(code,ctx)};
}
test('one stop request, final receipt freezes controls and preserves transcript/draft',async()=>{
 const t=setup();t.el('message-input').value='unsent synthetic draft';
 const first=t.el('stop').handlers.click();await t.el('stop').handlers.click();
 assert.equal(t.posts.length,1);assert.equal(t.el('send').disabled,true);
 const final={...t.sample,mode:'stopped',revision:2,ended:10,messages:[{id:'answer',role:'garik',status:'read',text:'saved answer',created:2}]};
 t.posts[0].resolve({ok:true,state:final});await first;
 t.run('adopt({...sample,mode:"stopping"});connected=false;renderStop();clocks()');
 assert.equal(t.el('runtime-title').textContent,'Демон остановлен');
 assert.equal(t.el('stop').disabled,true);assert.equal(t.el('send').disabled,true);
 assert.equal(t.el('message-input').value,'unsent synthetic draft');
 assert.equal(t.storage.get('garik-draft:synthetic'),'unsent synthetic draft');
 assert.match(t.el('messages').children[0].textContent,/saved answer/);
 assert.match(t.el('uptime').textContent,/остановлен/);
});
test('lost response never claims stopped and allows an explicit retry while connected',async()=>{
 const t=setup(),promise=t.el('stop').handlers.click();t.posts[0].reject(Error('offline'));await promise;
 assert.equal(t.el('runtime-title').textContent,'Остановка не подтверждена');
 assert.equal(t.el('stop').disabled,false);assert.equal(t.el('send').disabled,true);
 t.run('connected=false;renderStop()');assert.equal(t.el('stop').disabled,true);
 assert.match(t.el('banner').textContent,/garik daemon status/);
});
test('legacy receipt is accepted, not falsely upgraded to a final shutdown',async()=>{
 const t=setup(),promise=t.el('stop').handlers.click();t.posts[0].resolve({ok:true});await promise;
 t.run('connected=false;renderStop()');
 assert.notEqual(t.el('runtime-title').textContent,'Демон остановлен');
 assert.match(t.el('banner').textContent,/не прислал итоговую квитанцию/);
});
test('new instance unlocks controls and an old delayed stop reply cannot stop its UI',async()=>{
 const t=setup(),promise=t.el('stop').handlers.click();
 t.run("adopt({...sample,instance:'second'})");
 t.posts[0].resolve({ok:true,state:{...t.sample,mode:'stopped',ended:10}});await promise;
 assert.equal(t.el('stop').textContent,'Остановить демон');assert.equal(t.el('send').disabled,false);
 assert.equal(t.storage.has('garik-stop'),false);
});
test('unknown cache, reported zero and partial aggregate have different labels',()=>{
 const t=setup();
 assert.equal(t.run("metric({attempts:2,coverage:{cached:0},cached:0},'cached')"),'—');
 assert.equal(t.run("metric({attempts:2,coverage:{cached:2},cached:0},'cached')"),'0');
 assert.equal(t.run("metric({attempts:2,coverage:{cached:1},cached:0},'cached')"),'≥0');
});

test('reload restores a draft and lost in-flight stop permits explicit retry',()=>{
 const t=setup([['garik-draft:synthetic','retained draft'],['garik-stop',JSON.stringify({instance:'first',phase:'requesting'})]]);
 assert.equal(t.el('message-input').value,'retained draft');
 assert.equal(t.el('runtime-title').textContent,'Остановка не подтверждена');
 assert.equal(t.el('stop').disabled,false);
});
test('stopping blocks pending delivery without deleting its durable ID',async()=>{
 const pending=JSON.stringify({id:'stable-id',text:'unsent',database_id:'synthetic'});
 const t=setup([['garik-pending-message',pending],['garik-stop',JSON.stringify({instance:'first',phase:'accepted'})]]);
 await t.run('sendPending()');
 assert.equal(t.posts.length,0);assert.equal(t.storage.get('garik-pending-message'),pending);
});
