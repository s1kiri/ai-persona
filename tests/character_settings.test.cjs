const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(){
 function element(tag){return {tag,textContent:'',value:'',hidden:false,disabled:false,children:[],handlers:{},attributes:{},
  appendChild(child){this.children.push(child)},replaceChildren(){this.children=[]},setAttribute(k,v){this.attributes[k]=v},
  addEventListener(k,f){this.handlers[k]=f},focus(){this.focused=true}};}
 const document={createElement:element};const ctx=vm.createContext({document});
 vm.runInContext(fs.readFileSync('persona_runtime/character_settings.js','utf8'),ctx);
 const requests=[],tabs=element('nav'),narrator=element('div'),editor=element('div');
 const widget=new ctx.CharacterSettings({tabs,narrator,editor,save:(actor,kind,value)=>new Promise((resolve,reject)=>requests.push({actor,kind,value,resolve,reject}))});
 const actors=['arkady','garik'].map((id,i)=>({id,name:id,editable:true,description:'Own life',model:'synthetic-model',reasoning_effort:'low',
  controls:{wake_minutes:5+i*10,episode_minutes:15,sharing_appetite:50,visit_turns:3,away_minutes:5,photos:true},
  profile:{digest:id+'-v1',persona:id+' original persona',model:'synthetic-model',reasoning_effort:'low',can_save:true}}));
 widget.update(actors);return {widget,actors,requests,tabs,narrator,editor};
}
const submit=()=>({preventDefault(){}});
test('tabs separate narrator and two independent drafts; polling preserves dirty text and settings',()=>{
 const {widget:w,actors,narrator}=setup();assert.equal(narrator.hidden,false);
 w.select('arkady');const a=w.entries.get('arkady');a.inputs.get('wake_minutes').value='20';a.inputs.get('wake_minutes').handlers.input();
 a.persona.value='Private authored draft';a.persona.handlers.input();w.select('garik');
 const g=w.entries.get('garik');g.inputs.get('wake_minutes').value='30';g.inputs.get('wake_minutes').handlers.input();
 w.update(actors.map(x=>({...x,profile:{...x.profile,digest:x.id+'-v2'}})));w.select('arkady');
 assert.equal(a.inputs.get('wake_minutes').value,'20');assert.equal(a.persona.value,'Private authored draft');
 assert.equal(g.inputs.get('wake_minutes').value,'30');assert.equal(a.authorDigest,'arkady-v1');
 assert.equal(narrator.hidden,true);assert.equal(w.buttons.get('arkady').attributes['aria-selected'],'true');
});
test('an in-flight save retains its actor when the user switches to another character',async()=>{
 const {widget:w,requests}=setup();w.select('arkady');const a=w.entries.get('arkady');
 a.inputs.get('sharing_appetite').value='90';a.inputs.get('sharing_appetite').handlers.input();
 const promise=a.controlsForm.handlers.submit(submit());w.select('garik');
 await a.controlsForm.handlers.submit(submit());assert.equal(requests.length,1);
 assert.equal(requests[0].actor,'arkady');assert.equal(requests[0].kind,'controls');assert.equal(requests[0].value.sharing_appetite,90);
 requests[0].resolve();await promise;assert.equal(w.selected,'garik');assert.equal(a.controlDirty,false);
});
test('lost author-save response does not replay or erase the draft; reload is explicit',async()=>{
 const {widget:w,requests,actors}=setup();const a=w.entries.get('arkady');
 a.persona.value='New authored voice';a.persona.handlers.input();const promise=a.profileForm.handlers.submit(submit());
 assert.equal(requests[0].value.digest,'arkady-v1');requests[0].reject(Error('Synthetic disconnect'));await promise;
 assert.equal(requests.length,1);assert.equal(a.profileDirty,true);assert.equal(a.persona.value,'New authored voice');
 w.update(actors.map(x=>({...x,profile:{...x.profile,digest:x.id+'-v2',persona:'New saved version'}})));
 assert.equal(a.authorDigest,'arkady-v1');a.reset.handlers.click();assert.equal(a.authorDigest,'arkady-v2');assert.equal(a.persona.value,'New saved version');
});
test('unavailable peer and running-life author edit never submit; tab keyboard navigation works',async()=>{
 const {widget:w,actors,requests}=setup();w.update(actors.map(x=>({...x,editable:x.id!=='garik',profile:{...x.profile,can_save:false}})));
 const a=w.entries.get('arkady');a.profileDirty=true;w.enabled(a);assert.equal(a.profileSave.disabled,true);
 await a.profileForm.handlers.submit(submit());const g=w.entries.get('garik');g.controlDirty=true;await g.controlsForm.handlers.submit(submit());
 assert.equal(requests.length,0);w.buttons.get('narrator').handlers.keydown({key:'ArrowRight',preventDefault(){}});
 assert.equal(w.selected,'arkady');assert.equal(w.buttons.get('arkady').focused,true);
});
