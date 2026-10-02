// Exercise shipped views and job handlers with controlled DOM / HTTP boundaries.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext, runInContext} from 'node:vm';
import test from 'node:test';
const root = process.argv[2];
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.attributes={}; this.dataset={}; this.handlers={}; this.disabled=false; this.hidden=false; this.classList={toggle(){}}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  set textContent(text) { this.children=[]; this.text=String(text); }
  get textContent() { return (this.text??'')+this.children.map(n=>n.textContent).join(''); }
  setAttribute(key,value) { this.attributes[key]=String(value); if(key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name,handler) { (this.handlers[name]??=[]).push(handler); }
  async click() { assert(!this.disabled,'cannot click a disabled control'); for(const handler of this.handlers.click??[]) await handler({target:this}); }
  showModal() { this.open=true; }
  close() { this.open=false; }
  querySelector(selector) { return this.querySelectorAll(selector)[0]??null; }
  querySelectorAll(selector) {
    const descendants=this.children.flatMap(n=>[n,...n.querySelectorAll('*')]);
    return descendants.filter(node=>selector.split(',').some(part=>{
      const s=part.trim();
      if(s==='*') return true;
      if(s.startsWith('#')) { const [id,tag]=s.slice(1).split(' '); return tag ? this.querySelector('#'+id)?.querySelectorAll(tag).includes(node) : node.attributes.id===id; }
      const match=s.match(/^([^\[]*)\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return match ? (!match[1]||node.tag===match[1])&&match[2] in node.attributes&&(match[3]===undefined||node.attributes[match[2]]===match[3]) : node.tag===s;
    }));
  }
}
function environment({hasResults=true, failed=false}={}) {
  const document=new Element('document');
  document.createElement=tag=>new Element(tag);
  document.createTextNode=text=>{const n=new Element('#text');n.textContent=text;return n;};
  for(const id of ['error','dialog','dialogContent','leagueName','freshness','notice','syncButton','navigation','content','busy','phase','progress','closeDialog','quitButton','skipLink','pageTitle','pageEyebrow']) {
    const node=new Element('div');node.setAttribute('id',id);document.append(node);
  }
  const data={availability:{enabled:true},selected:'league',preferences:{trade_value_min_ratio:0.7,timezone:'UTC'},parameters:{tolerance:{value:1e-9}},preference_controls:{trade_value_min_ratio:{minimum:0.5,maximum:1,default:0.7}},snapshot:{mine:'mine',teams:[{id:'mine',name:'Mine',players:[]},{id:'other',name:'Other',players:[]}]}};
  const trades=[{opponent:'other',send:['p1'],receive:['p3'],mine_delta:0.1,opponent_delta:0.1,acceptance:0.5,expected_gain:0.5},{opponent:'other',send:['p2'],receive:['p4'],mine_delta:0.9,opponent_delta:0.2,acceptance:0.6,expected_gain:0.2}];
  let action, submits=0;
  const sandbox=createContext({document,Node:Element,console,Intl,Date,URL,URLSearchParams,setTimeout:fn=>fn(),location:{hash:''},sessionStorage:{getItem:()=>''},history:{replaceState(){}},fetch:async(path,options)=>{
    if(path==='/api/action') { action=JSON.parse(options.body).action;submits++;return {ok:true,json:async()=>({job:'job'})}; }
    if(path==='/api/bootstrap') return {ok:true,json:async()=>data};
    assert.equal(path,'/api/job');
    const result=action==='partners'?[]:{...trades[0],traces:[],before:[],after:[],opponent_before:[],opponent_after:[]};
    return {ok:true,json:async()=>({id:'job',status:failed?'failed':'completed',error:'time budget exceeded; no partial result published',progress:1,result})};
  }});
  for(const name of ['forms.js','views.js','app.js']) {
    const source=readFileSync(root+'/'+name,'utf8').replace(/^import .*;\n/gm,'').replace(/^export \{.*\} from .*;\n/gm,'').replace(/^export /gm,'');
    runInContext(source,sandbox,{filename:name});
  }
  const ctx=runInContext('context',sandbox);ctx.data=data;ctx.tab='trades';ctx.results=hasResults?{trades}:{};ctx.render();
  const button=text=>document.querySelectorAll('button').find(n=>n.textContent===text);
  const sorts=()=>document.querySelector('[aria-label="交易結果排序"]').querySelectorAll('button');
  return {ctx,document,button,sorts,submits:()=>submits};
}
for(const [action,label] of [['partners','找互補對象'],['trade','0.5']]) {
  test(`${action} completion restores current result sort controls after dialog`,async()=>{
    const e=environment();const previous=e.sorts();
    await e.button(label).click();
    assert(e.document.querySelector('#dialog').open);
    await e.document.querySelector('#closeDialog').click();
    assert.notEqual(e.sorts()[0],previous[0],'completion refreshed the actual view');
    assert(e.sorts().every(n=>!n.disabled));
    await e.button('增益最大').click();
    assert.equal(e.button('增益最大').attributes['aria-pressed'],'true');
    const text=e.document.querySelector('#content').textContent;
    assert(text.indexOf('p2')<text.indexOf('p1'),'new sort changes row order');
    assert.equal(e.submits(),1);
  });
}
test('failed jobs preserve input and prior results while restoring controls',async()=>{
  const e=environment({failed:true});const input=e.document.querySelector('input');input.value='unsaved';
  await e.button('找互補對象').click();
  assert.equal(input.value,'unsaved');assert(e.document.querySelectorAll('input').includes(input));
  assert(e.sorts().every(n=>!n.disabled));assert(!e.ctx.pending);
  assert.match(e.document.querySelector('[data-job-actions]').textContent,/逾時/);
  assert.equal(e.ctx.results.trades.length,2);
});
test('empty result sorting remains disabled after a successful job',async()=>{
  const e=environment({hasResults:false});await e.button('找互補對象').click();
  assert(e.sorts().every(n=>n.disabled));
});
test('running jobs disable actions and reject duplicate submissions',async()=>{
  const e=environment();const pending=e.ctx.run('partners',{});
  assert(e.ctx.pending);assert(e.button('全聯盟 1 換 1').disabled);
  assert.match(e.document.querySelector('[data-job-actions]').textContent,/請稍候/);
  await assert.rejects(e.ctx.run('partners',{}),/已有計算進行中/);
  await pending;assert.equal(e.submits(),1);
  assert(!e.button('全聯盟 1 換 1').disabled);
});
