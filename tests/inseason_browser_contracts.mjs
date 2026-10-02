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
  async change(value) { this.value=value; for(const handler of this.handlers.change??[]) await handler({target:this}); }
  scrollIntoView() { this.scrolled=true; }
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
function environment({hasResults=true, failed=false, searchStatus="completed", savedStatus=null, latestAction=null, resumeRunning=false}={}) {
  const document=new Element('document');
  document.createElement=tag=>new Element(tag);
  document.createTextNode=text=>{const n=new Element('#text');n.textContent=text;return n;};
  for(const id of ['error','dialog','dialogContent','leagueName','freshness','notice','syncButton','navigation','content','busy','phase','progress','jobLabel','jobCount','cancelJob','closeDialog','quitButton','skipLink','pageTitle','pageEyebrow']) {
    const node=new Element('div');node.setAttribute('id',id);document.append(node);
  }
  const data={availability:{enabled:true},selected:'league',preferences:{trade_value_min_ratio:0.7,timezone:'UTC'},parameters:{tolerance:{value:1e-9}},preference_controls:{trade_value_min_ratio:{minimum:0.5,maximum:1,default:0.7}},snapshot:{mine:'mine',teams:[{id:'mine',name:'Mine',players:[]},{id:'other',name:'Other',players:[]}]}};
  const trades=[{opponent:'other',send:['p1'],receive:['p3'],mine_delta:0.1,opponent_delta:0.1,acceptance:0.5,expected_gain:0.5},{opponent:'other',send:['p2'],receive:['p4'],mine_delta:0.9,opponent_delta:0.2,acceptance:0.6,expected_gain:0.2}];
  if(savedStatus) data.last_trade_search={saved_at:'2026-10-02T19:30:00Z',result:{trades:savedStatus==='cancelled'?trades.slice(0,1):trades,counts:{eligible:4,full_effects:savedStatus==='cancelled'?1:4},minimum_value_ratio:0.7,status:savedStatus,completed:savedStatus==='cancelled'?1:4}};
  let action=latestAction, submits=0, cancellations=[], jobReads=0;
  const sandbox=createContext({document,Node:Element,console,Intl,Date,URL,URLSearchParams,setTimeout:fn=>fn(),location:{hash:''},sessionStorage:{getItem:()=>''},history:{replaceState(){}},fetch:async(path,options)=>{
    if(path==='/api/action') { action=JSON.parse(options.body).action;submits++;return {ok:true,json:async()=>({job:'job'})}; }
    if(path==='/api/cancel') { cancellations.push(JSON.parse(options.body));return {ok:true,json:async()=>({cancelling:true})}; }
    if(path==='/api/bootstrap') return {ok:true,json:async()=>data};
    assert.equal(path,'/api/job');
    if(resumeRunning && jobReads++===0) return {ok:true,json:async()=>({id:'job',action:'trade-search',status:'running'})};
    const result=action==='trade-search'?{trades:searchStatus==='cancelled'?trades.slice(0,1):trades,counts:{eligible:4,candidates:5,value_filtered:1,full_effects:searchStatus==='cancelled'?1:4},minimum_value_ratio:0.7,status:searchStatus,completed:searchStatus==='cancelled'?1:4}:action==='partners'?[]:{...trades[0],traces:[],before:[],after:[],opponent_before:[],opponent_after:[]};
    return {ok:true,json:async()=>({id:'job',action,status:failed?'failed':action==='trade-search'?searchStatus:'completed',search:action==='trade-search'?{phase:'evaluating',total:4,completed:searchStatus==='cancelled'?1:4}:null,error:'time budget exceeded; no partial result published',progress:1,result})};
  }});
  for(const name of ['forms.js','views.js','app.js']) {
    const source=readFileSync(root+'/'+name,'utf8').replace(/^import .*;\n/gm,'').replace(/^export \{.*\} from .*;\n/gm,'').replace(/^export /gm,'');
    runInContext(source,sandbox,{filename:name});
  }
  const ctx=runInContext('context',sandbox);ctx.data=data;ctx.tab='trades';ctx.results=hasResults?{trades}:{};ctx.render();
  const button=text=>document.querySelectorAll('button').find(n=>n.textContent===text);
  const sorts=()=>document.querySelector('[aria-label="交易結果排序"]')?.querySelectorAll('button')??[];
  const header=label=>document.querySelectorAll('button').find(n=>n.attributes['aria-label']===`依${label}排序`);
  return {ctx,document,button,header,sorts,submits:()=>submits,cancellations,initialize:()=>runInContext('initialize()',sandbox),updateJob(job){sandbox.jobSnapshot=job;runInContext("updateJob(jobSnapshot, 'trade-search')",sandbox);}};
}
for(const [action,label] of [['partners','找互補對象'],['trade','查看分析']]) {
  test(`${action} completion restores current result sort controls after dialog`,async()=>{
    const e=environment();const previous=e.sorts();
    await e.button(label).click();
    assert(e.document.querySelector('#dialog').open);
    await e.document.querySelector('#closeDialog').click();
    assert.notEqual(e.sorts()[0],previous[0],'completion refreshed the actual view');
    assert(e.sorts().every(n=>!n.disabled));
    await e.header('我方增益').click();
    assert.equal(e.document.querySelectorAll('th').find(n=>n.textContent.includes('我方增益')).attributes['aria-sort'],'descending');
    const text=e.document.querySelector('#tradeSearchResults').querySelector('tbody').textContent;
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


test('progress names the current stage and counts, cancellation targets the observed job',async()=>{
  const e=environment();
  e.updateJob({id:9,status:'running',can_cancel:true,progress:0,search:{phase:'baseline',total:342,completed:0,baseline_total:14,baseline_completed:6}});
  assert.equal(e.document.querySelector('#jobCount').textContent,'已完成 0 / 342 筆');
  assert.match(e.document.querySelector('#phase').textContent,/準備球隊基準 6 \/ 14 隊/);
  assert.equal(e.document.querySelector('#progress').value,0);
  await e.document.querySelector('#cancelJob').click();
  assert.deepEqual(e.cancellations,[{job:9}]);
  assert(e.document.querySelector('#cancelJob').disabled);
  e.updateJob({id:9,status:'cancelling',can_cancel:false,search:{phase:'evaluating',total:342,completed:3}});
  assert.equal(e.document.querySelector('#jobCount').textContent,'已完成 3 / 342 筆');
  assert.match(e.document.querySelector('#phase').textContent,/保留已完成結果/);
});
test('cancelled searches show retained trades as partial and restore the next search',async()=>{
  const e=environment({searchStatus:'cancelled'});
  await e.button('全聯盟 1 換 1').click();
  assert.equal(e.ctx.results.trades.length,1);
  assert.equal(e.ctx.results.tradeCompleted,1);
  assert.equal(e.ctx.results.tradeStatus,'cancelled');
  assert.match(e.document.querySelector('#content').textContent,/部分結果/);
  assert.match(e.document.querySelector('#content').textContent,/已完成 1 \/ 4 筆/);
  assert(!e.document.querySelector('#content').textContent.includes('沒有符合搜尋條件'));
  assert(e.sorts().every(n=>!n.disabled));
  assert(!e.button('全聯盟 1 換 1').disabled);
  assert(!e.ctx.pending);
});

function resultRows(e) {
  return (e.document.querySelector('#tradeSearchResults').querySelector('tbody')?.querySelectorAll('tr')??[]).filter(row=>row.querySelectorAll('td').length===8);
}
const rowSend=row=>row.querySelectorAll('td')[1].textContent;
test('recommendations default to positive gains, expected gain descending and ten visible rows',async()=>{
  const e=environment();
  const base=e.ctx.results.trades[0];
  e.ctx.results.trades=Array.from({length:12},(_,index)=>({...base,send:[`positive${index+1}`],expected_gain:index+1}));
  e.ctx.results.trades.push(...[
    {mine_delta:-1,expected_gain:99},{mine_delta:0,expected_gain:99},
    {expected_gain:0},{expected_gain:-1},{expected_gain:null},
    {mine_delta:1e-10},{expected_gain:1e-10}
  ].map((fields,index)=>({...base,send:[`excluded${index}`],...fields})));
  const original=JSON.stringify(e.ctx.results.trades);e.ctx.render();
  assert.equal(resultRows(e).length,10);
  assert.deepEqual(resultRows(e).map(rowSend),Array.from({length:10},(_,index)=>`positive${12-index}`));
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/12 筆正收益推薦 · 顯示 10 筆/);
  await e.button('顯示更多推薦（還有 2 筆）').click();
  assert.equal(resultRows(e).length,12);
  assert(resultRows(e).every(row=>!row.textContent.includes('excluded')));
  assert.equal(JSON.stringify(e.ctx.results.trades),original,'filtering preserves all completed results');
  assert.equal(e.submits(),0);
});
test('opponent and outgoing-player menus combine filters including bundled trades',async()=>{
  const e=environment();const base=e.ctx.results.trades[0];
  e.ctx.data.snapshot.teams.push({id:'second',name:'Second',players:[]});
  e.ctx.results.trades=[
    {...base,send:['p1','p2'],expected_gain:3},
    {...base,send:['p3'],expected_gain:2},
    {...base,opponent:'second',send:['p1'],expected_gain:1}
  ];e.ctx.render();
  const menu=name=>e.document.querySelector(`select[name="${name}"]`);
  assert(e.document.querySelector('thead').querySelectorAll('select').includes(menu('trade_result_player')),'filters belong to their table columns');
  await menu('trade_result_player').change('p1');
  assert.deepEqual(resultRows(e).map(rowSend),['p1、p2','p1']);
  await menu('trade_result_opponent').change('other');
  assert.deepEqual(resultRows(e).map(rowSend),['p1、p2']);
  await menu('trade_result_player').change('p3');
  await menu('trade_result_opponent').change('second');
  assert.equal(resultRows(e).length,0);
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/沒有符合這組篩選條件/);
  assert(e.document.querySelector('thead').querySelectorAll('select').includes(menu('trade_result_player')),'empty filtered rows keep their table controls');
  await menu('trade_result_player').change('');
  assert.deepEqual(resultRows(e).map(rowSend),['p1']);
  assert.equal(e.submits(),0);
  await e.button('清除篩選').click();
  assert.equal(resultRows(e).length,3);
});
for (const [key,label] of [['mine_delta','我方增益'],['opponent_delta','對方增益'],['acceptance','接受率'],['expected_gain','期望值']]) {
  test(`${key} header toggles numeric order and announces direction`,async()=>{
    const e=environment();const base=e.ctx.results.trades[0];
    e.ctx.results.trades=[{...base,send:['low'],[key]:0.1},{...base,send:['high'],[key]:0.9}];e.ctx.render();
    if(key!=='expected_gain') await e.header(label).click();
    assert.deepEqual(resultRows(e).map(rowSend),['high','low']);
    const th=()=>e.document.querySelectorAll('th').find(n=>n.textContent.includes(label));
    assert.equal(th().attributes['aria-sort'],'descending');
    await e.header(label).click();
    assert.deepEqual(resultRows(e).map(rowSend),['low','high']);
    assert.equal(th().attributes['aria-sort'],'ascending');
    await e.header(label).click();
    assert.deepEqual(resultRows(e).map(rowSend),['high','low']);
    assert.equal(e.submits(),0);
  });
}
test('a cancelled search without positive results distinguishes partial coverage',()=>{
  const e=environment();e.ctx.results.trades=e.ctx.results.trades.map(t=>({...t,mine_delta:-1,expected_gain:-1}));
  e.ctx.results.tradeStatus='cancelled';e.ctx.results.tradeCompleted=2;e.ctx.results.tradeAudit={eligible:20};e.ctx.render();
  assert.equal(resultRows(e).length,0);
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/已完成的部分尚無正收益推薦；搜尋尚未完成/);
});
for(const previousSort of ['expected_gain','mine_delta']) test(`new search resets ${previousSort} ascending to the ten highest expected gains`,async()=>{
  const e=environment();e.ctx.tradeResultOpponent='obsolete';e.ctx.tradeResultPlayer='obsolete';e.ctx.tradeVisible=30;
  e.ctx.tradeSort=previousSort;e.ctx.tradeSortDirection='asc';
  const base=e.ctx.results.trades[0];
  e.ctx.results.trades.push(...Array.from({length:10},(_,index)=>({...base,send:[`new${index+1}`],expected_gain:index+1})));
  await e.button('全聯盟 1 換 1').click();
  assert.equal(e.ctx.tradeResultOpponent,'');assert.equal(e.ctx.tradeResultPlayer,'');assert.equal(e.ctx.tradeVisible,10);
  assert.equal(e.ctx.tradeSort,'expected_gain');assert.equal(e.ctx.tradeSortDirection,'desc');
  assert.deepEqual(resultRows(e).map(rowSend),Array.from({length:10},(_,index)=>`new${10-index}`));
  assert.equal(e.document.querySelectorAll('th').find(n=>n.textContent.includes('期望值')).attributes['aria-sort'],'descending');
});
test('table browsing remains available during a job while recalculation stays disabled',async()=>{
  const e=environment();const pending=e.ctx.run('partners',{});
  assert(e.ctx.pending);assert(e.sorts().every(button=>!button.disabled));
  await e.header('我方增益').click();
  assert.deepEqual(resultRows(e).map(rowSend),['p2','p1']);
  assert(e.button('全聯盟 1 換 1').disabled);assert(e.button('查看分析').disabled);
  assert(e.document.querySelector('select[name="trade_result_player"]'));
  await pending;
  assert(!e.button('查看分析').disabled);assert(!e.button('全聯盟 1 換 1').disabled);
});
test('a failed job restores controls created by filtering and preserves the current form',async()=>{
  const e=environment({failed:true});const pending=e.ctx.run('partners',{});
  await e.document.querySelector('select[name="trade_result_player"]').change('p1');
  const input=e.document.querySelector('input');input.value='draft after filtering';
  assert(e.button('查看分析').disabled);
  await assert.rejects(pending,/time budget exceeded/);
  assert(!e.button('查看分析').disabled);assert(!e.button('全聯盟 1 換 1').disabled);
  assert.equal(e.document.querySelector('input'),input);assert.equal(input.value,'draft after filtering');
  assert.deepEqual(resultRows(e).map(rowSend),['p1']);
});
for(const savedStatus of ['completed','cancelled']) for(const latestAction of ['trade','partners','trade-search']) {
  test(`reload restores ${savedStatus} search after ${latestAction} without recalculating`,async()=>{
    const e=environment({hasResults:false,savedStatus,latestAction});
    await e.initialize();
    assert.equal(e.ctx.results.tradeStatus,savedStatus);
    assert.equal(resultRows(e).length,savedStatus==='cancelled'?1:2);
    assert.match(e.document.querySelector('#tradeSearchResults').textContent,/上次搜尋結果/);
    assert.equal(e.ctx.results.tradeSavedAt,'2026-10-02T19:30:00Z');
    if(savedStatus==='cancelled') assert.match(e.document.querySelector('#tradeSearchResults').textContent,/已完成 1 \/ 4 筆/);
    assert.equal(e.submits(),0,'restoring saved results must not submit calculations');
  });
}
test('reload with no saved search stays empty',async()=>{
  const e=environment({hasResults:false,latestAction:'partners'});await e.initialize();
  assert.equal(resultRows(e).length,0);assert.equal(e.submits(),0);
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/搜尋後，正收益推薦會列在這裡/);
});
test('reload keeps a saved empty result distinct from never searched',async()=>{
  const e=environment({hasResults:false,savedStatus:'cancelled',latestAction:'trade'});
  e.ctx.data.last_trade_search.result.trades=[];await e.initialize();
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/已完成的部分尚無正收益推薦/);
  assert.match(e.document.querySelector('#tradeSearchResults').textContent,/上次搜尋結果/);
  assert.equal(e.submits(),0);
});
test('reload reattaches an ongoing search and replaces the saved result on completion',async()=>{
  const e=environment({hasResults:false,savedStatus:'cancelled',latestAction:'trade-search',resumeRunning:true});
  await e.initialize();
  assert.equal(e.ctx.results.tradeStatus,'completed');assert.equal(resultRows(e).length,2);
  assert.equal(e.ctx.results.tradeSavedAt,null);assert.equal(e.submits(),0);assert(!e.ctx.pending);
});
