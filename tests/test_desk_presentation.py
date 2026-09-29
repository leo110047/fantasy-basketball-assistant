import subprocess
from pathlib import Path


def test_price_table_search_filters_csv_and_comparison_contract():
    module = Path(__file__).parents[1] / "src/fba/apps/static/presentation.js"
    script = r"""
import assert from 'node:assert/strict';
const {matches,valueGap,priceRows,priceCSV,rowTags,comparisonLabel,floorBackup,forecastRange} =
  await import(process.argv[1]);
const make = (id,name,fair,positions=['PG']) =>
  ({id,name,fair,positions,positions_confirmed:true,detail:null});
assert(matches(make('1','Nikola Jokić',30),'jokic'));
assert(matches(make('1','Nikola Jokić',30),'NJ'));
assert(matches(make('1','LeBron James',30),'LBJ'));
assert(matches(make('1','P.J. Washington',30),'PJW'));
const laker={...make('1','LeBron James',30),team:{abbreviation:'LAL',name:'Los Angeles Lakers'}};
assert(matches(laker,'lal'));
assert(matches(laker,'los angeles'));
assert(matches(laker,'Lakers James'));
assert(!matches(laker,'LAC'));
assert(!matches(make('1','Nikola Jokić',30),'other'));
assert.deepEqual(valueGap(null,2),{difference:null,discount:null,focused:false});
assert.equal(valueGap(25,20).focused,true);
assert.equal(valueGap(100,95).focused,false);
assert.equal(valueGap(20,16).focused,false);
assert(floorBackup({planning_cost:2},{amount:2},false,2));
assert(!floorBackup({planning_cost:2},{amount:1},false,2));
assert(!floorBackup({planning_cost:2},undefined,false,2));
assert(!floorBackup({planning_cost:2},{amount:2},true,2));
assert(!floorBackup({planning_cost:2},{amount:2,reason:'unavailable'},false,2));
const players = new Map([
  ['a',make('a','Alpha',25)],['b',make('b','Beta',30,['C'])],
  ['c',make('c','Gamma',null)],['d',make('d','=HYPERLINK("x")',50)],
]);
const desk={state:{teams:[{id:'one',name:'Team One'}],
  sales:[{player_id:'d',buyer:'one',amount:9}]},market:{state_sha256:'sha',market:{prices:[
  {player_id:'a',expected:20,anchor:20},{player_id:'b',expected:29,anchor:29},{player_id:'c',expected:null,anchor:null},{player_id:'d',expected:10,anchor:10},
]}}};
const result={caps:[{player_id:'a',amount:21},{player_id:'b',amount:35}],plan:{purchases:['b']}};
const filters={query:'',scope:'available',position:'',sort:'fair'}, watched=new Set(['a']);
const ids=rows=>rows.map(r=>r.player.id);
assert.deepEqual(ids(priceRows(players,desk,result,watched,filters)),['b','a','c']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,scope:'focus'})),['a']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,scope:'watch'})),['a']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,sort:'gap'})),['a','b','c']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,sort:'cap'})),['b','a','c']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,sort:'edge'})),['b','a','c']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,sort:'name'})),['a','b','c']);
assert.deepEqual(ids(priceRows(players,desk,result,watched,{...filters,position:'C'})),['b']);
// Market focus is independent of our cap and plan, including missing quotes.
const altered={caps:[{player_id:'a',amount:0}],plan:{purchases:[]}};
assert.deepEqual(ids(priceRows(players,desk,altered,watched,{...filters,scope:'focus'})),['a']);
const rows=priceRows(players,desk,result,watched,{...filters,scope:'all',sort:'name'});
const managed={...result,streaming:{flex:[{player_id:'b',removal_loss:0.1}]}};
const streamingRows=priceRows(players,desk,managed,watched,filters);
assert(rowTags(streamingRows.find(r=>r.player.id==='b')).includes('可操作候選'));
assert(!rowTags(rows.find(r=>r.player.id==='b')).includes('可操作候選'));
const csv=priceCSV(rows,desk,'fit');
assert(csv.startsWith('\ufeff'));
assert(csv.includes('"\'=HYPERLINK(""x"")"'));
assert(csv.includes('"Team One","9","fit","sha"'));
assert(!csv.includes('undefined')&&!csv.includes('null'));
assert(csv.indexOf('"Alpha"')<csv.indexOf('"Beta"'));
const pending=priceRows(players,desk,null,watched,filters);
assert(pending.every(r=>r.cap==null&&r.edge==null));
assert(!rowTags(pending[0]).includes('組隊方案'));
assert(rowTags(rows.find(r=>r.player.id==='c')).includes('缺市場報價'));
const annotated={...pending[0],player:{...pending[0].player,detail:{adjustments:[],
  history_games:2,history_minimum:3,original_expected_games:7,healthy_games_threshold:8}}};
assert(rowTags(annotated).includes('歷史樣本少'));
assert(rowTags(annotated).includes('出賽風險'));
annotated.player.detail.history_minimum=2;
annotated.player.detail.healthy_games_threshold=7;
assert(!rowTags(annotated).includes('歷史樣本少'));
assert(!rowTags(annotated).includes('出賽風險'));
assert.equal(comparisonLabel({buy:{players:[]},skip:{players:[]},delta:1}),'買入方案的模型效用較高');
assert.equal(comparisonLabel({buy:{reason:'x'},skip:{players:[]},delta:null}),'此價格買入後無法完成組隊');
assert.equal(comparisonLabel({buy:{players:[]},skip:{reason:'x'},delta:null}),'目前組隊需要買入此人');
assert.deepEqual(forecastRange({fair:20,scenarios:[]}),{status:'absent'});
assert.deepEqual(forecastRange({fair:20,scenarios:[{fair:10},{fair:30}]}),{status:'ready',low:10,high:30});
assert.deepEqual(forecastRange({fair:40,scenarios:[{fair:10},{fair:30}]}),{status:'ready',low:10,high:40});
assert.deepEqual(forecastRange({fair:20,scenarios:[{fair:null},{fair:30}]}),{status:'incomplete'});
assert.deepEqual(forecastRange({fair:null,scenarios:[{fair:30}]}),{status:'incomplete'});
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, module.as_uri()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_streaming_reply_is_discarded_for_changed_state_and_hidden_for_another_mode():
    app = Path(__file__).parents[1] / "src/fba/apps/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(process.argv[1],'utf8');
const operation=source.slice(source.indexOf('async function inspectStreaming('),
  source.indexOf('function renderSensitivity('));
const getter=source.slice(source.indexOf('function result('),
  source.indexOf('function unavailable('));
let identity='first',mode='fit',complete;
const context={streamingPending:false,streamingReply:null,
 saving:false,stale:false,changingPrices:false,
 jobs:{state_sha256:'first',fit:{result:{plan:{players:['a']}}},equal:{result:{plan:{players:['b']}}}},
 sha:()=>identity,el:()=>({value:mode}),render(){},
 api:()=>new Promise(resolve=>{complete=resolve})};
runInNewContext(operation+'\n'+getter,context);
let pending=context.inspectStreaming();
identity='second';complete({state_sha256:'first',mode:'fit',streaming:{recommended_slots:1}});
await pending;assert.equal(context.streamingReply,null);
identity='first';pending=context.inspectStreaming();mode='equal';
complete({state_sha256:'first',mode:'fit',streaming:{recommended_slots:1}});
await pending;assert.equal(context.result().streaming,null);
mode='fit';assert.equal(context.result().streaming.recommended_slots,1);
identity='second';assert.equal(context.result(),null);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
