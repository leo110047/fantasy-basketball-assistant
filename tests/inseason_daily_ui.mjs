// Actual UI renderers with a controlled DOM; layout and focus are checked in Chrome.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
class Element {
  constructor(tag, text=''){this.tag=tag;this.text=text;this.children=[];this.attributes={};this.events={};}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(name,fn){this.events[name]=fn;}
  append(child){this.children.push(child);}
  replaceChildren(...children){this.children=children;}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this.text=value;this.children=[];}
  all(tag){return [this,...this.children.flatMap(c=>c.all(tag))].filter(c=>c.tag===tag);}
  querySelector(selector){return this.all(selector.split('[')[0])[0]??null;}
}
function environment(){
  const root=process.argv[2];
  const context={Node:Element,document:{createElement:tag=>new Element(tag),createTextNode:t=>new Element('#text',t)},Intl,Date,URL,FormData,
    formula:trace=>new Element('trace',trace.id)};
  for(const file of ['forms.js','views.js']){
    const source=readFileSync(`${root}/${file}`,'utf8').replace(/^import .*;$/gm,'').replace(/^export \{.*;$/gm,'').replace(/export (?=(?:function|const))/g,'');
    runInNewContext(source,context);
  }
  return context;
}
function fixture(){
  const player={id:'one',name:'Taylor Player',team_id:'BOS',positions:['PG']};
  const forecast={away:'away',home:'mine',scoring:'h2h_one_win',score:.63,raw_score:.68,no_moves_score:.6,elapsed_days:2,remaining_games:{mine:12,away:10},adds_remaining:2,
    categories:[{label:'PTS',home:50,away:40,home_denominator:null,away_denominator:null,raw_probability:.7,probability:.66,strategy:'key',traces:[{id:'category-trace'}]}],
    lineup_search:'joint_exact',simulations:100,standard_error:.01,traces:[{id:'forecast-trace'}],lineups:[]};
  return {data:{projection:{on:'2026-10-02',players:[{player}]},state:{league:{scoring:'h2h_one_win',starter_slots:[{id:'guard',label:'PG'}]}},
    preferences:{timezone:'UTC'},parameters:{week_calibration:{value:.8},calibration:{value:.8}},snapshot:{teams:[{id:'away',name:'Opponents'}]}},
    results:{today:{on:'2026-10-02',score_before:.61,score_after:.63,locks:{one:'2026-10-03T00:00:00Z'},players:[],traces:[{id:'today-trace'}],week_forecast:forecast,
      actions:[{id:'action',player_id:'one',kind:'start',slot:'guard',completed:false,reason:'Before tipoff'}]}},
    render(){},navigate(){},error(error){this.failure=error;},async run(){}};
}
test('daily work precedes analysis; formulas remain available in closed disclosures',()=>{
  const ui=environment(),ctx=fixture(),view=ui.todayView(ctx),text=view.textContent;
  assert(text.indexOf('今天要做的事')<text.indexOf('這週怎麼贏'));
  assert.match(text,/61% → 63%/);assert.match(text,/已在 Yahoo 完成操作/);
  for(const id of ['today-trace','category-trace','forecast-trace']){
    const owner=view.all('details').find(node=>node.textContent.includes(id));
    assert(owner,id);assert.equal(owner.attributes.open,undefined);
  }
  assert.equal(ui.scoreText(5.25,'h2h_each_category'),'5.25 類');
  assert.equal(ui.scoreText(null,'h2h_one_win'),'—');
  assert.match(text,/關鍵類別/);assert(!text.includes('穩贏'));
});
test('completion checkbox waits for persistence and restores rejected changes',async()=>{
  for(const rejected of [false,true]){
    const ui=environment(),ctx=fixture(),action=ctx.results.today.actions[0];let calls=0,release;
    ctx.run=async(name,payload)=>{calls++;assert.equal(name,'complete');assert.equal(payload.id,'action');assert.equal(payload.completed,true);await new Promise(resolve=>release=resolve);if(rejected)throw new Error('save failed');};
    const checkbox=ui.todayView(ctx).all('input').find(n=>n.attributes.type==='checkbox');
    checkbox.checked=true;const pending=checkbox.events.change({target:checkbox});
    assert(checkbox.disabled);assert(!action.completed);assert.equal(calls,1);
    release();await pending;assert.equal(action.completed,!rejected);assert(!checkbox.disabled);
    if(rejected){assert(!checkbox.checked);assert.match(ctx.failure.message,/save failed/);}
    else assert(ui.todayView(ctx).all('input').find(n=>n.attributes.type==='checkbox').checked);
  }
});
test('daily empty state retains an explicit calculation and the selected plan',()=>{
  const ui=environment(),ctx=fixture();ctx.results={selectedPlan:'chosen'};
  const view=ui.todayView(ctx);assert.match(view.textContent,/計算今日安排/);assert.match(view.textContent,/已帶入本週換人計畫/);
  assert.equal(view.all('form').length,1);
});

test('my roster includes injury slots and search filters only its rendered rows',()=>{
  const ui=environment(),ctx=fixture();
  ctx.data.projection.players=[{player:{id:'one',name:'Taylor Player',team_id:'BOS',positions:['PG']},flags:[],adjustments:[],probability:.5,minutes:30},{player:{id:'injured',name:'Alex Return',team_id:'NYK',positions:['C']},flags:[],adjustments:[],probability:0,minutes:30},{player:{id:'free',name:'Free Player',team_id:'CHI',positions:['C']},flags:[],adjustments:[],probability:1,minutes:20}];
  ctx.data.snapshot={mine:'mine',teams:[{id:'mine',name:'My team',players:['one'],injury_players:{injured:'IL'}}]};
  ctx.data.projection_rules={categories:[{id:'PTS',label:'PTS'},{id:'FG%',label:'FG%'},{id:'DD',label:'DD'}]};
  ctx.data.player_categories={one:{PTS:18.2,'FG%':.53,DD:.25},injured:{PTS:18.2,'FG%':.53,DD:.25}};
  const view=ui.teamsView(ctx);
  assert.match(view.textContent,/Taylor Player/);assert.match(view.textContent,/Alex Return/);assert(!view.textContent.includes('Free Player'));
  assert.deepEqual(view.all('th').map(cell=>cell.textContent),['球員','出賽狀態','出賽／輪替機率','出賽時分鐘','PTS','FG%','DD','預測提醒','操作']);
  const rows=view.all('tr'),active=rows.find(row=>row.textContent.includes('Taylor Player')),injured=rows.find(row=>row.textContent.includes('Alex Return'));
  assert.equal(active.all('td')[4].textContent,'18.2');assert.equal(active.all('td')[5].textContent,'53%');assert.equal(active.all('td')[6].textContent,'0.25');
  assert.equal(injured.all('td')[4].textContent,'18.2');assert.equal(injured.all('td')[5].textContent,'53%');
  assert.match(view.textContent,/出賽時的預估場均，不乘出賽率/);
  assert.match(view.textContent,/週總量與排陣另計出賽率/);
  const input=view.all('input').find(n=>n.attributes.name==='player_search');
  input.events.input({target:{value:'Alex'}});
  assert.match(view.textContent,/Alex Return/);assert(!view.textContent.includes('Taylor Player'));
  input.events.input({target:{value:'unmatched'}});assert.match(view.textContent,/沒有符合條件/);
});
test('filtering multi-select players keeps hidden checked selections',()=>{
  const ui=environment(),ctx=fixture(),ids=Array.from({length:10},(_,i)=>`p${i}`);
  ctx.data.projection.players=ids.map(id=>({player:{id,name:`Sample ${id}`,team_id:'BOS',positions:['PG']}}));
  const view=ui.checks(ctx,ids,'send',['p9']);
  view.all('input').find(n=>n.attributes.name==='send-filter').events.input({target:{value:'Sample p0'}});
  const selected=view.all('input').find(n=>n.attributes.value==='p9');
  assert(selected.checked);assert(!selected.disabled);
  const row=view.all('label').find(n=>n.children.includes(selected));assert(row.hidden);
});
