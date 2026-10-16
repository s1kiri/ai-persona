const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){
 const elements=new Map(),posts=[];
 function element(tag){return {tag,textContent:'',disabled:false,children:[],handlers:{},appendChild(child){this.children.push(child)},replaceChildren(){this.children=[]},addEventListener(k,f){this.handlers[k]=f}};}
 const document={getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id)},createElement:element};
 const ctx=vm.createContext({document,window:{confirm:()=>true},setInterval(){},fetch(path,options){if(!options?.method)return new Promise(()=>{});return new Promise((resolve,reject)=>posts.push({path,options,reject,resolve:(v,ok=true)=>resolve({ok,json:async()=>v})}));}});
 vm.runInContext(fs.readFileSync('persona_runtime/shared_panel/shared.js','utf8'),ctx);
 ctx.sample={csrf:'synthetic',live:false,paused:true,minute:700,actors:[{id:'arkady',name:'Аркадий Кедров',model:'gpt-6-luna',reasoning_effort:'low',configured:false,panel_url:'http://127.0.0.1:8887',memories:8,life:null,peer_context:{nearby_actor_ids:[],ongoing_action:null}},{id:'garik',name:'Гарик',configured:false,panel_url:'http://127.0.0.1:8888',memories:2,life:null,peer_context:{nearby_actor_ids:[],ongoing_action:null}}],cycles:[],peer_jobs:[]};
 vm.runInContext('render(sample)',ctx);
 return {ctx,posts,el:id=>document.getElementById(id),run:c=>vm.runInContext(c,ctx)};
}
function buttons(card){return card.children.flatMap(c=>c.children||[]).filter(c=>c.tag==='button');}
test('preparation lists both independent panels without any publication',()=>{
 const t=setup();assert.equal(t.posts.length,0);assert.equal(t.el('actors').children.length,2);
 assert.equal(buttons(t.el('actors').children[0])[0].disabled,true);
 const links=t.el('actors').children.map(c=>c.children.find(e=>e.tag==='a').href);
 assert.deepEqual(links,['http://127.0.0.1:8887','http://127.0.0.1:8888']);
});
test('names, pending messages and task text render literally',()=>{
 const t=setup();t.ctx.text='<img src=x onerror=alert(1)>';
 t.run('render({...sample,actors:sample.actors.map(a=>({...a,name:text})),peer_jobs:[{id:"job",sender:"arkady",recipient:"garik",status:"pending",value:{text}}]})');
 assert.equal(t.el('actors').children[0].children[0].textContent,t.ctx.text);
 assert.equal(t.el('jobs').children[0].children[0].textContent,t.ctx.text);
});
test('one pause targets only its actor and cannot double-submit',()=>{
 const t=setup();t.run('render({...sample,live:true,actors:sample.actors.map(a=>({...a,life:{paused:false,error:null}}))})');
 const button=buttons(t.el('actors').children[1])[0];button.handlers.click();button.handlers.click();
 assert.equal(t.posts.length,1);assert.deepEqual(JSON.parse(t.posts[0].options.body),{actor_id:'garik',running:false});
});
test('unknown peer delivery requires an explicit owner absence confirmation',()=>{
 const t=setup();t.run('render({...sample,live:true,peer_jobs:[{id:"peer-id",sender:"garik",recipient:"arkady",status:"unknown",value:{text:"Own chosen reply"}}]})');
 assert.equal(t.posts.length,0);t.el('jobs').children[0].children[1].handlers.click();
 assert.equal(t.posts.length,1);assert.equal(t.posts[0].path,'/api/peer-confirm-absent');
 assert.deepEqual(JSON.parse(t.posts[0].options.body),{actor_id:'garik',job_id:'peer-id',confirmation:'owner_checked_absent'});
});
test('world event submits once with a stable key and text survives polling',()=>{
 const t=setup();t.el('world-event-text').value='На улице продают прошлогодний снег.';
 t.run('queueWorldEvent();queueWorldEvent();render(sample)');
 assert.equal(t.posts.length,1);assert.equal(t.posts[0].path,'/api/world-events');
 const body=JSON.parse(t.posts[0].options.body);assert.equal(body.text,t.el('world-event-text').value);assert.ok(body.request_id);
});
test('world event rendering uses literal text and offers cancellation only before dispatch',()=>{
 const t=setup();t.run('render({...sample,world_events:[{id:"one",text:"<img src=x>",status:"queued"},{id:"two",text:"started",status:"started"}]})');
 assert.equal(t.el('world-events').children[0].children[0].textContent,'<img src=x>');
 assert.equal(t.el('world-events').children[1].children.some(c=>c.tag==='button'),false);
 t.el('world-events').children[0].children[2].handlers.click();assert.equal(t.posts[0].path,'/api/world-events/cancel');
});
