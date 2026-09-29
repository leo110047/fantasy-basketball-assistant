// Component regression tests for the actual shipped handlers. Network and DOM
// boundaries are controlled here; real layout, focus and dialogs are tested in Chrome.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
const root=process.argv[2], app=readFileSync(root+'/app.js','utf8'), editing=readFileSync(root+'/editing.js','utf8');
function section(source,begin,end){const a=source.indexOf(begin),b=source.indexOf(end,a);assert(a>=0&&b>a,begin);return source.slice(a,b)}
function element(id=''){
  const handlers=new Map();
  return {id,value:'',textContent:'',hidden:false,disabled:false,open:false,isConnected:true,
    elements:[],children:[],handlers,focused:false,
    addEventListener(name,fn){if(!handlers.has(name))handlers.set(name,[]);handlers.get(name).push(fn)},
    async fire(name,values={}){const event={target:this,prevented:false,preventDefault(){this.prevented=true},...values};for(const fn of handlers.get(name)??[])await fn(event);return event},
    querySelector(){return null},replaceChildren(...values){this.children=values},
    showModal(){this.open=true},close(){this.open=false;return this.fire('close')},focus(){this.focused=true},
    getBoundingClientRect(){return {left:10,right:100,top:10,bottom:100}},
  };
}
function environment(){
 const nodes=new Map(),document=element('document');
 const el=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id)};
 const context={el,document,desk:{state:{revision:0,teams:[],sales:[],watch:[],overrides:[]},market:{state_sha256:'first',market:{room:[{id:'ours',slots:2,maximum_bid:9}]}}},
  boot:{league:{minimum_bid:1,bid_increment:1}},jobs:{state_sha256:'first',equal:{status:'ready',result:{id:'equal'}},fit:{status:'ready',result:{id:'fit'}}},
  selected:'one',saving:false,stale:false,changingPrices:false,comparing:false,composing:false,polling:false,pollTimer:null,
  compareRevision:0,players:new Map(),watched:new Set(),notes:[],renders:0,
  clearTimeout(){},setTimeout(){},beginTiming(){},rendered(){},measure(){},render(){context.renders++},
  money:String,error(message){context.notes.push(message)},saveError:message=>message,controls(){},
  invalidateComparison(){context.compareRevision++},poll(){},integer:v=>Number(v),
  crypto:{randomUUID:()=> 'unique-sale'},clearSaleFields(){el('amount').value='';el('buyer').value=''},
  performance,requestAnimationFrame:fn=>fn(),node:(_tag,text)=>({text}),renderComparison(value){context.compared=value},
 };
 context.sha=()=>context.desk.market.state_sha256;
 el('buyer').value='ours';el('amount').value='4';el('mode').value='fit';el('comparePrice').value='4';
 runInNewContext(section(app,'function integer(','function saveError('),context);
 return context;
}
function load(c,begin,end){runInNewContext(section(app,begin,end),c);return c}
const event=()=>({preventDefault(){}});

test('IME and repeated Enter cannot commit a lookup or submit a sale',async()=>{
 const c=environment();let picked=0;
 c.el('matches').querySelector=()=>({click(){picked++}});
 c.sell=()=>{};c.renderBuyers=()=>{};c.save=()=>{};
 runInNewContext(section(app,'el("lookup").addEventListener("keydown"','for (const id of ["filter"'),c);
 for(const flags of [{isComposing:true},{repeat:true}]){
  await c.el('lookup').fire('keydown',{key:'Enter',...flags});assert.equal(picked,0);
  const e=await c.el('saleForm').fire('keydown',{key:'Enter',target:c.el('amount'),...flags});assert(e.prevented);
 }
 await c.el('lookup').fire('keydown',{key:'Enter'});assert.equal(picked,1);
 assert((await c.el('saleForm').fire('keydown',{key:'Enter',target:c.el('buyer')})).prevented);
 assert(!(await c.el('saleForm').fire('keydown',{key:'Enter',target:c.el('amount')})).prevented);
 await c.el('saleForm').fire('compositionstart');assert(c.composing);
 await c.el('saleForm').fire('compositionend');assert(!c.composing);
});

test('sale handlers preserve rejected input and prevent duplicate in-flight submission',async()=>{
 const c=load(environment(),'async function sell(','async function poll(');let count=0,release;
 c.save=()=>{count++;c.saving=true;return new Promise(r=>release=r)};
 c.composing=true;await c.sell(event());assert.equal(count,0);c.composing=false;
 const pending=c.sell(event());await c.sell(event());assert.equal(count,1);
 release(false);await pending;assert.equal(c.el('amount').value,'4');assert.equal(c.el('buyer').value,'ours');
 c.saving=false;c.el('amount').value='10';await c.sell(event());assert.equal(count,1);assert.match(c.notes.at(-1),/最高可付/);
 for(const invalid of ['1e2','1,000','4.5','0','-1','9007199254740992']){c.el('amount').value=invalid;await c.sell(event());assert.equal(count,1);assert.match(c.notes.at(-1),/正整數/)}
 assert.equal(c.integer('$４６'),46);
 c.el('amount').value='4';c.save=async candidate=>{assert.equal(candidate.sales[0].amount,4);return true};
 await c.sell(event());assert.equal(c.el('amount').value,'');assert.equal(c.el('buyer').value,'');
});

test('HTTP rejection preserves the prior ledger and prices; uncertain saves require reload',async()=>{
 for(const status of [400,403,500,undefined]){
  const c=load(environment(),'async function save(','function failedJobs('),old=c.desk,jobs=c.jobs;
  c.api=async()=>{throw Object.assign(new Error('injected'),{status})};
  assert.equal(await c.save({...old.state,sales:[{id:'new'}]}),false);
  assert.equal(c.desk,old);assert.equal(c.el('amount').value,'4');assert(!c.saving);
  if(status===400||status===403){assert.equal(c.jobs,jobs);assert(!c.stale);assert.match(c.notes.at(-1),/未保存/)}
  else{assert(c.stale);assert.equal(c.jobs,null);assert.match(c.notes.at(-1),/保存未確認/)}
 }
});

test('poll replies cannot overwrite a concurrent sale or leak prices from an older state',async()=>{
 const c=load(environment(),'async function poll(','async function comparison(');let release,received=0;
 c.api=()=>new Promise(r=>release=r);c.receiveResults=()=>received++;
 let pending=c.poll();c.saving=true;release({});await pending;assert.equal(received,0);
 c.saving=false;pending=c.poll();c.desk.market.state_sha256='next';release({});await pending;assert.equal(received,0);
 pending=c.poll();release({});await pending;assert.equal(received,1);
 load(c,'function result(','function unavailable(');c.streamingReply=null;
 assert.equal(c.result(),null);c.jobs.state_sha256='next';assert.equal(c.result().id,'fit');
 c.changingPrices=true;assert.equal(c.result(),null);c.changingPrices=false;c.stale=true;assert.equal(c.result(),null);
});

test('compare replies are discarded after player, price, mode or draft changes',async()=>{
 for(const mutate of [c=>c.selected='two',c=>c.compareRevision++,c=>c.el('mode').value='equal',c=>c.desk.market.state_sha256='next',c=>c.saving=true]){
  const c=load(environment(),'async function comparison(','function openSettings(');let release;
  c.api=()=>new Promise(r=>release=r);const pending=c.comparison(event());mutate(c);release({comparison:'old'});await pending;
  assert.equal(c.compared,undefined);assert(!c.comparing);
 }
 const c=load(environment(),'async function comparison(','function openSettings(');c.api=async()=>({comparison:'current'});
 await c.comparison(event());assert.equal(c.compared.comparison,'current');
});

test('confirmed save remains saved when result loading fails and keeps pending sale fields',async()=>{
 const c=load(environment(),'async function save(','function receiveResults('),initial=c.desk;
 const saved={...initial,state:{...initial.state,revision:1,watch:['one']},market:{...initial.market,state_sha256:'next'}};
 c.api=async path=>{if(path==='draft')return saved;throw new Error('results unavailable')};
 assert(await c.save({...initial.state,watch:['one']},true));
 assert.equal(c.desk,saved);assert.equal(c.jobs.fit.status,'failed');assert.equal(c.jobs.state_sha256,'next');
 assert.match(c.el('saved').textContent,/已保存.*1/);assert.match(c.notes.at(-1),/已保存.*載入失敗/);
 assert.equal(c.el('amount').value,'4');assert(!c.stale);
});

test('server draft changes invalidate prices and unchanged polling does not rerender',()=>{
 const c=load(environment(),'function receiveResults(','async function sell(');c.render=()=>{c.renders++};
 c.receiveResults(c.jobs);assert.equal(c.renders,0);
 c.receiveResults({...c.jobs,equal:{status:'failed',result:null,error:'injected'}});assert.equal(c.renders,1);
 c.receiveResults({state_sha256:'another'});assert(c.stale);assert.equal(c.jobs,null);assert.match(c.notes.at(-1),/其他視窗/);
});

test('dialog dirty edits, reverted fields, drag boundaries, saving lock and focus restoration',async()=>{
 const c=environment(),dialog=element(),form=element(),close=element(),opener=element();
 const field={name:'amount',value:'4'};form.elements=[field];form.querySelector=()=>({hidden:false});
 c.document.activeElement=opener;let allow=false,asked=0;
 runInNewContext(section(editing,'function formValue(','async function submit('),c);
 const open=c.protect(dialog,form,close,{isSaving:()=>c.saving,confirmChange:async()=>{asked++;return allow}});
 open();field.value='5';await close.fire('click');assert(dialog.open);assert.equal(asked,1);
 allow=true;await dialog.fire('cancel');assert(!dialog.open);assert(opener.focused);
 open();field.value='6';field.value='5';await close.fire('click');assert(!dialog.open);assert.equal(asked,2);
 open();c.saving=true;await close.fire('click');assert(dialog.open);c.saving=false;
 await dialog.fire('pointerdown',{clientX:20,clientY:20});await dialog.fire('pointerup',{clientX:0,clientY:0});assert(dialog.open);
 await dialog.fire('pointerdown',{clientX:0,clientY:0});await dialog.fire('pointerup',{clientX:20,clientY:20});assert(dialog.open);
 await dialog.fire('pointerdown',{clientX:0,clientY:0});await dialog.fire('pointerup',{clientX:0,clientY:0});assert(!dialog.open);
});

test('browsing another player never changes the nominated sale; replacing nominee protects pending input',async()=>{
 const c=environment();c.players=new Map([['one',{id:'one',name:'One'}],['two',{id:'two',name:'Two'}]]);
 c.renderNominee=()=>{};c.renderProjectionDetail=()=>{};c.renderSensitivity=()=>{};c.editors={override(){}};
 c.action=()=>({});c.money=String;let allow=false;c.confirmChange=async()=>allow;
 load(c,'async function nominate(','function renderDetails(');
 c.renderDetails=id=>{c.inspected=id};load(c,'function browse(','function renderStreaming(');
 c.browse('two');assert.equal(c.inspected,'two');assert.equal(c.selected,'one');assert.equal(c.el('amount').value,'4');
 await c.nominate('two');assert.equal(c.selected,'one');assert.equal(c.el('amount').value,'4');
 allow=true;await c.nominate('two');assert.equal(c.selected,'two');assert.equal(c.el('amount').value,'');
});


test('settings reject blank names locally and freeze staged fields throughout saving',async()=>{
 const c=environment(),field={dataset:{team:'ours'},value:'   ',disabled:false};
 c.desk.state.mine='ours';c.el('mine').value='ours';c.el('settingsDialog').open=true;
 c.el('teamNames').querySelectorAll=()=>[field];c.el('settingsForm').elements=[field,c.el('mine')];
 let saved=0,release;c.save=(candidate,metadata)=>{saved++;assert(metadata);assert.equal(candidate.teams[0].name,'New');c.saving=true;return new Promise(r=>release=r)};
 load(c,'async function applySettings(', 'el("settingsForm").addEventListener("submit", applySettings)');
 await c.applySettings(event());assert.equal(saved,0);assert.match(c.el('settingsError').textContent,/不能空白/);assert(!field.disabled);
 field.value='  New  ';const pending=c.applySettings(event());assert(field.disabled);assert(c.el('mine').disabled);
 await c.applySettings(event());assert.equal(saved,1);release(false);await pending;
 assert(c.el('settingsDialog').open);assert.equal(field.value,'  New  ');assert(!field.disabled);
 c.saving=false;c.save=async()=>true;await c.applySettings(event());assert(!c.el('settingsDialog').open);assert(c.el('settings').focused);
});
