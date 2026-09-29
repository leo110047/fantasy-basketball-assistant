import subprocess
from pathlib import Path


def test_override_bootstrap_refresh_preserves_concurrent_watch_before_next_save():
    app = Path(__file__).parents[1] / "src/fba/apps/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source = readFileSync(process.argv[1], 'utf8');
const save = source.slice(source.indexOf('async function save('),
  source.indexOf('function failedJobs('));
const watch = source.slice(source.indexOf('async function watch('),
  source.indexOf('function integer('));
const initial = {state:{revision:0,overrides:[],watch:[]},market:{state_sha256:'initial'}};
const overridden = {state:{revision:1,overrides:[{player_id:'a',market:10}],watch:[]},
  market:{state_sha256:'override'}};
const concurrent = {state:{...overridden.state,revision:2,watch:['x']},
  market:{state_sha256:'concurrent'}};
const sent = [], nodes = new Map();
const context = {
  desk:initial,boot:null,players:new Map(),watched:new Set(),jobs:null,
  saving:false,stale:false,changingPrices:false,
  beginTiming(){},invalidateComparison(){},render(){},error(message){assert.equal(message,'')},
  rendered(){},poll(){},catalogue(){return new Map()},receiveResults(){},
  el(id){if(!nodes.has(id))nodes.set(id,{});return nodes.get(id)},
  async api(path,body){
    if(path==='bootstrap')return {desk:concurrent};
    if(path==='results')return {};
    assert.equal(path,'draft'); sent.push(body);
    return sent.length===1 ? overridden : {...concurrent,state:body.draft};
  },
};
context.sha=()=>context.desk.market.state_sha256;
runInNewContext(save+'\n'+watch,context);
assert(await context.save(overridden.state));
assert.deepEqual([...context.watched],['x']);
await context.watch('y');
assert.equal(sent[1].expected_sha256,'concurrent');
assert.deepEqual([...sent[1].draft.watch],['x','y']);
assert.equal(context.desk.state.revision,2);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
