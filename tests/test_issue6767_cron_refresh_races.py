"""Observable regressions for cron refresh races raised in PR #6767."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
PANELS = Path(__file__).resolve().parents[1] / "static" / "panels.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

HARNESS = r"""
const assert=require('assert');
const src=require('fs').readFileSync(PANELS_PATH,'utf8');
function extract(name){
 const start=src.search(new RegExp('(async\\s+)?function\\s+'+name+'\\s*\\('));
 assert(start>=0,name);let i=src.indexOf('{',start)+1,depth=1;
 while(depth&&i<src.length){if(src[i]==='{')depth++;else if(src[i]==='}')depth--;i++;}
 return src.slice(start,i);
}
function node(){return {innerHTML:'',style:{},children:[],classList:{add(){},toggle(){}},appendChild(x){this.children.push(x);},addEventListener(){}};}
const box=node(),button=node(),output=node();output.innerHTML='Loaded output';output.open=true;
const fields={cronList:box,cronRefreshBtn:button,cronDetailRuns:output,cronFormName:{value:'Saved'},cronFormSchedule:{value:'0 8 * * *'},cronFormPrompt:{value:'Do work'},cronFormError:node()};
function $(id){return fields[id]||null;}
const document={createElement:node};
let _cronList=[{id:'existing',name:'Existing'}],_cronOtherProfileCount=0,_showAllCronProfiles=false,_cronLoadGeneration=0;
const _cronNewJobIds=new Set(['newer']);
let _currentCronDetail='existing',_currentCronDetailKey='existing',_cronMode='detail',_editingCronId=null,_cronPreFormDetail=null,_cronIsDuplicate=false,_cronSelectedSkills=[];
let renders=0,clears=0,opened=null,writes=[],renderedJob=null;const pending=[];
let _cronDetailRefreshKey = null;
const localStorage = {getItem(){return null;},setItem(){}};
function api(url,opts){if(opts){writes.push(url);return Promise.resolve({id:'saved'});}return new Promise((resolve,reject)=>pending.push({resolve,reject}));}
async function loadCronProfiles(){} function loadCronGatewayNotice(){}
function esc(s){return String(s);} function t(s){return s;} function showToast(){}
function _cronStatusMeta(){return {state:'active',label:'on',listClass:'active'};}
function _cronItemId(j){return 'cron-'+j.id;} function _cronJobKey(j){return j.id;}
function _cronProfileLabel(p){return p;} function _cronOwnerProfileName(){return 'default';}
function _appendCronProfileToggle(){} function _clearCronDetail(){clears++;output.innerHTML='';output.open=false;}
function _renderCronDetail(job){renders++;renderedJob=job;output.innerHTML='Rebuilt';output.open=false;}
function openCronDetail(job){if(typeof job==='string')job=_cronList.find(j=>j.id===job);if(!job)return;opened=job;_cronMode='detail';}
eval(extract('loadCrons'));eval(extract('saveCronForm'));
eval(extract('cronPause'));eval(extract('cronResume'));
async function waitRequests(n){for(let i=0;i<50&&pending.length<n;i++)await Promise.resolve();assert.equal(pending.length,n);}
"""


def run_js(body):
    script = HARNESS.replace("PANELS_PATH", json.dumps(str(PANELS)))
    script += "\n(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1);});"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rejects", [False, True])
def test_superseded_load_preserves_new_list_and_dom(rejects):
    settle = "pending[0].reject(new Error('old timeout'));" if rejects else "pending[0].resolve({jobs:[{id:'stale',name:'Stale'}]});"
    run_js("""
const old=loadCrons(true),fresh=loadCrons();await waitRequests(2);
pending[1].resolve({jobs:[{id:'newer',name:'Newer'}]});const freshData=await fresh;
const html=box.innerHTML,children=box.children.slice();
""" + settle + """
const oldData=await old;assert.deepEqual(_cronList.map(j=>j.id),['newer']);
assert.equal(box.innerHTML,html);assert.deepEqual(box.children,children);
assert.equal(button.disabled,false);assert.equal(button.style.opacity,'');
assert.equal(freshData.jobs[0].id,'newer');
""" + ("assert.equal(oldData,undefined);" if rejects else "assert.equal(oldData.jobs[0].id,'stale');"))


def test_current_load_failure_displays_error():
    run_js("const p=loadCrons(true);await waitRequests(1);pending[0].reject(new Error('current timeout'));await p;assert(box.innerHTML.includes('current timeout'));assert.equal(button.disabled,false);")


@pytest.mark.parametrize("empty", [False, True])
def test_background_refresh_keeps_output_open_and_marks_list(empty):
    jobs = [] if empty else [{"id": "newer", "name": "Newer"}]
    run_js("const p=loadCrons(false,{background:true});await waitRequests(1);pending[0].resolve({jobs:" + json.dumps(jobs) + "});const data=await p;assert.equal(renders,0);assert.equal(clears,0);assert.equal(output.innerHTML,'Loaded output');assert.equal(output.open,true);assert.deepEqual(_cronList,data.jobs);if(data.jobs.length)assert(box.children[0].innerHTML.includes('cron-new-dot'));")


@pytest.mark.parametrize("action,state", [("cronPause", "paused"), ("cronResume", "active")])
@pytest.mark.parametrize("older_first", [False, True])
def test_action_refresh_survives_background_supersession(action, state, older_first):
    new_job = json.dumps({"id": "existing", "name": "Existing", "state": state})
    old_job = json.dumps({"id": "existing", "name": "Older response", "state": "outdated"})
    settle_old = "pending[0].resolve({jobs:[" + old_job + "]});await action;"
    settle_new = "pending[1].resolve({jobs:[" + new_job + "]});await poll;"
    run_js("const action=" + action + "('existing');await waitRequests(1);"
           "const poll=loadCrons(false,{background:true});await waitRequests(2);" +
           (settle_old + settle_new if older_first else settle_new + settle_old) + """
assert.equal(renders,1);assert.equal(renderedJob.state,""" + json.dumps(state) + """);
assert.equal(_cronDetailRefreshKey,null);
// Subsequent unrelated background ticks must not rebuild it again.
const next=loadCrons(false,{background:true});await waitRequests(3);
pending[2].resolve({jobs:[""" + new_job + """]});await next;assert.equal(renders,1);
""")


@pytest.mark.parametrize("mode,key", [("edit", "existing"), ("create", "existing"), ("detail", "other")])
def test_pending_refresh_does_not_override_new_selection_or_form(mode, key):
    run_js("""
const old=loadCrons();await waitRequests(1);
_cronMode=""" + json.dumps(mode) + ";_currentCronDetailKey=" + json.dumps(key) + ";" + """
const poll=loadCrons(false,{background:true});await waitRequests(2);
pending[1].resolve({jobs:[{id:'existing',name:'Existing'},{id:'other',name:'Other'}]});await poll;
pending[0].resolve({jobs:[{id:'existing',name:'Old'}]});await old;
assert.equal(renders,0);assert.equal(_cronDetailRefreshKey,null);
""")


def test_failed_latest_refresh_retains_intent_until_successful_retry():
    run_js("""
const old=cronPause('existing');await waitRequests(1);
const poll=loadCrons(false,{background:true});await waitRequests(2);
pending[1].reject(new Error('temporary'));await poll;
pending[0].resolve({jobs:[{id:'existing',name:'Old'}]});await old;
assert.equal(renders,0);assert.equal(_cronDetailRefreshKey,'existing');
const retry=loadCrons(false,{background:true});await waitRequests(3);
pending[2].resolve({jobs:[{id:'existing',name:'Current',state:'paused'}]});await retry;
assert.equal(renders,1);assert.equal(renderedJob.state,'paused');assert.equal(_cronDetailRefreshKey,null);
""")


def test_user_requested_refresh_still_updates_detail():
    run_js("const p=loadCrons();await waitRequests(1);pending[0].resolve({jobs:[{id:'existing',name:'Updated'}]});await p;assert.equal(renders,1);")


@pytest.mark.parametrize("editing", [False, True])
def test_save_selects_saved_job_even_when_poll_supersedes_refresh(editing):
    prefix = "_cronMode=" + json.dumps("edit" if editing else "create") + ";" + ("_editingCronId='saved';" if editing else "")
    run_js(prefix + "const save=saveCronForm();await waitRequests(1);const poll=loadCrons(false,{background:true});await waitRequests(2);pending[1].resolve({jobs:[{id:'existing',name:'Existing'}]});await poll;pending[0].resolve({jobs:[{id:'existing',name:'Existing'},{id:'saved',name:'Saved'}]});await save;assert.equal(opened&&opened.id,'saved');assert.equal(typeof opened,'object');assert.equal(_cronMode,'detail');assert.equal(writes.length,1);")
