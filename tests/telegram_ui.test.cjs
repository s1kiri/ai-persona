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
