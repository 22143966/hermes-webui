"""Execute real client functions with synthetic transport and deferred draft saves.

These are Node VM behavior tests, not a rendered/login browser or provider run.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Client VM tests do not require an HTTP server."""


@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """No server sessions are created by this module."""


def _source():
    sessions = (ROOT / "static/sessions.js").read_text()
    ui = (ROOT / "static/ui.js").read_text()
    messages = (ROOT / "static/messages.js").read_text()
    names = (
        "_adoptRegenerationRevision",
        "_currentLoadedRenderableMessageCount",
        "_captureSameSessionForceReloadHint",
        "_clearSameSessionForceReloadHint",
        "_messageReloadLimitForSession",
    )
    functions = [extract_function(sessions, name) for name in names]
    for name in ("loadSession", "_ensureMessagesLoaded", "_ensureAllMessagesLoaded"):
        functions.append(extract_function(sessions, name, "async function"))
    functions.append(extract_function(ui, "regenerateResponse", "async function"))
    functions.append(extract_function(messages, "startRegeneration", "async function"))
    return "\n".join(functions)


HARNESS = r"""
const vm=require('node:vm');
const spec=SPEC;
const rows=Array.from({length:spec.total},(_,i)=>({role:i%2?'assistant':'user',content:'row '+i,timestamp:i+1}));
const cached=rows.slice(spec.offset, spec.initialTotal);
const requests=[], accepted=[], streams=[], statuses=[], renders=[], draftSaves=[];
const noop=()=>{};
let releaseDraft;
const draft=new Promise(resolve=>{releaseDraft=resolve;});
let releaseMessages;
const delayedMessages=new Promise(resolve=>{releaseMessages=resolve;});
const composer={value:'unsaved synthetic draft'};
const msgInner={innerHTML:''};
const stored=new Map();
const c={
  S:{session:{session_id:'A',message_count:spec.initialTotal,regeneration_revision:'old-revision'},messages:cached,toolCalls:[],pendingFiles:[],busy:false,lastUsage:{}},
  INFLIGHT:{}, window:{}, console,
  _loadingSessionId:null, _loadSessionGeneration:0,
  _messagesTruncated:spec.offset>0, _oldestIdx:spec.offset, _loadingOlder:false,
  _sameSessionForceReloadHint:null, _pendingCarryForwardSnapshot:null,
  _INITIAL_MSG_LIMIT:30,_MSG_LIMIT_MAX:500,_msgLimitMax:spec.ceiling||500,
  localStorage:{setItem:(k,v)=>stored.set(k,v),getItem:k=>stored.get(k)||null,removeItem:k=>stored.delete(k)},
  $:id=>id==='msg'?composer:id==='msgInner'?msgInner:null,
  _saveComposerDraftNow:(...args)=>{draftSaves.push(args);return draft;},
  _rearmActiveSessionStream:noop,stopApprovalPolling:noop,startApprovalPolling:noop,hideApprovalCard:noop,
  _updateYoloPill:noop,clearCompressionUi:noop,_sessionVisitHasUnreadState:()=>false,
  _clearDeferredActiveSessionExternalRefresh:noop,
  _resolveSessionModelForDisplaySoon:noop,_acknowledgeSessionVisit:noop,
  _setActiveSessionUrl:noop,_mergePendingSessionMessage:noop,
  _selectLiveRecoveryInflight:()=>null,
  updateQueueBadge:noop,updateSendBtn:noop,setComposerStatus:noop,
  syncTopbar:noop,_deferWorkspaceRefreshForSession:noop,
  _isMessagingSession:()=>false,_hideHandoffHint:noop,
  _syncToolCallsForLoadedMessages:noop,clearLiveToolCards:noop,
  _setSessionViewedCount:noop,_bumpMessagesGeneration:noop,
  renderMessages:()=>renders.push(c.S.messages.map(m=>m.content)),
  setStatus:s=>{if(s)statuses.push(s);}, t:s=>s,
  setTimeout,clearTimeout,
  setBusy:value=>{c.S.busy=value;}, markInflight:noop, removeThinking:noop,
  attachLiveStream:(...args)=>streams.push(args),
};
c.api=async(path,options)=>{
  if(path==='/api/chat/start'){
    const body=JSON.parse(options.body);
    if(body.regenerate!==true)throw new Error('not a regeneration request');
    accepted.push([body.session_id,body.regeneration_revision]);
    if(spec.regenReject)throw new Error('synthetic regeneration rejection');
    return {stream_id:'synthetic-stream',pending_started_at:1};
  }
  requests.push(path);
  const params=new URL(path,'https://synthetic.invalid').searchParams;
  const sid=params.get('session_id');
  if(params.get('messages')==='0')return {session:{session_id:sid,message_count:spec.total,messages:[]}};
  if(spec.delayed)await delayedMessages;
  if(spec.error){const e=new Error('synthetic read rejection');e.status=spec.error;throw e;}
  if(spec.authRedirect)return undefined;
  const raw=params.get('msg_limit');
  const n=(!raw||raw==='all')?rows.length:Math.min(Number(raw),spec.ceiling||500);
  const result=rows.slice(-n);
  const truncated=result.length<rows.length;
  return {session:{session_id:sid,messages:result,message_count:rows.length,
    _messages_truncated:truncated,_messages_offset:rows.length-result.length,_msg_limit_max:spec.ceiling||500,
    ...(!truncated&&!spec.ineligible?{regeneration_revision:'fresh-revision'}:{})}};
};
vm.createContext(c);
vm.runInContext(SOURCE,c);
(async()=>{
 if(spec.mode==='navigation'){
   const pendingB=c.loadSession('B');
   // B waits inside the real draft-save await; A remains on screen.
   if(c._loadingSessionId!=='B'||draftSaves.length!==1)throw new Error('draft seam was not reached');
   await c.loadSession('A');
   releaseDraft();await pendingB;
 }else if(spec.mode==='cold'){
   c.S.session=null;c.S.messages=[];c._messagesTruncated=false;c._oldestIdx=0;
   await c.loadSession('A');
 }else{
   c._loadingSessionId='A';c._loadSessionGeneration=1;
   if(!spec.keepRevision)delete c.S.session.regeneration_revision;
   c._captureSameSessionForceReloadHint('A');
   const pending=c._ensureMessagesLoaded('A',{loadGeneration:1,force:!!spec.force});
   if(spec.delayed){
     // A stale response may not overwrite a new pane or a new same-SID generation.
     c._loadSessionGeneration=2;
     if(spec.switchSid){c._loadingSessionId='B';c.S.session={session_id:'B'};}
     c.S.messages=[{role:'assistant',content:'newer pane'}];
     c._sameSessionForceReloadHint={session_id:spec.switchSid?'B':'A',marker:'newer-owner'};
     releaseMessages();
   }
   try{await pending;}catch(e){statuses.push('read-'+e.status);}
 }
 const beforeRegeneration={count:c.S.messages.length,first:c.S.messages[0]?.content,
   last:c.S.messages.at(-1)?.content,offset:c._oldestIdx,truncated:c._messagesTruncated,
   revision:c.S.session?.regeneration_revision};
 if(spec.regenerate){
   await c.regenerateResponse({closest:()=>({dataset:{msgIdx:String(c.S.messages.length-1)}})});
 }
 process.stdout.write(JSON.stringify({requests,accepted,streams,statuses,renders,draftSaves:draftSaves.length,
   beforeRegeneration,activeSid:c.S.session?.session_id,loadingSid:c._loadingSessionId,
   hint:c._sameSessionForceReloadHint,messageCount:c.S.messages.length,loadingOlder:c._loadingOlder}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_case(**options):
    assert shutil.which("node"), "node is required for client behavior tests"
    spec = {"mode": "navigation", "total": 80, "initialTotal": 80, "offset": 0, **options}
    script = HARNESS.replace("SPEC", json.dumps(spec)).replace("SOURCE", json.dumps(_source()))
    result = subprocess.run(
        ["node", "--max-old-space-size=96", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _message_requests(result):
    return [path for path in result["requests"] if "messages=1" in path]


def test_deferred_draft_a_b_a_refetches_authority_and_can_regenerate():
    result = _run_case(regenerate=True)
    assert result["draftSaves"] == 1
    assert result["activeSid"] == "A"
    assert result["loadingSid"] is None
    assert len(result["requests"]) == 2
    assert all("session_id=A" in path for path in result["requests"])
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["streams"] == [["A", "synthetic-stream", []]]
    assert result["statuses"] == []
    assert result["hint"] is None


@pytest.mark.parametrize(
    "total,initial,offset,expected_count,expected_offset,limit",
    [(80,80,0,80,0,None), (100,100,40,60,40,60),
     (1000,1000,340,1000,0,None), (82,80,0,82,0,None),
     (102,100,40,62,40,62), (20,20,0,20,0,None)],
)
def test_authority_refetch_preserves_loaded_window_and_external_append(
    total, initial, offset, expected_count, expected_offset, limit,
):
    result = _run_case(total=total, initialTotal=initial, offset=offset)
    fetches = _message_requests(result)
    assert len(fetches) == 1
    if limit is None:
        assert "msg_limit=" not in fetches[0]
    else:
        assert f"msg_limit={limit}&" in fetches[0]
    state = result["beforeRegeneration"]
    assert state["count"] == expected_count
    assert state["offset"] == expected_offset
    assert state["first"] == f"row {expected_offset}"
    assert state["last"] == f"row {total - 1}"
    assert result["hint"] is None


def test_partial_history_gets_full_authority_only_when_regenerating():
    result = _run_case(total=100, initialTotal=100, offset=40, regenerate=True)
    assert result["beforeRegeneration"]["count"] == 60
    assert "revision" not in result["beforeRegeneration"]
    assert result["beforeRegeneration"]["truncated"] is True
    assert len(_message_requests(result)) == 2
    assert "msg_limit=all" in _message_requests(result)[1]
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["messageCount"] == 99
    assert result["loadingOlder"] is False


def test_cold_navigation_still_uses_default_tail():
    result = _run_case(mode="cold")
    assert len(_message_requests(result)) == 1
    assert "msg_limit=30&" in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 30
    assert result["beforeRegeneration"]["offset"] == 50


def test_cached_messages_with_authority_do_not_refetch():
    result = _run_case(mode="loader", keepRevision=True)
    assert result["requests"] == []
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None


def test_full_ineligible_response_does_not_loop_or_invent_authority():
    result = _run_case(ineligible=True, regenerate=True)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert result["accepted"] == []
    assert result["statuses"] == ["regen_failed"]


@pytest.mark.parametrize("switch_sid", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_delayed_message_response_cannot_overwrite_newer_owner(switch_sid, force):
    result = _run_case(mode="loader", delayed=True, switchSid=switch_sid, force=force)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["first"] == "newer pane"
    assert result["hint"]["marker"] == "newer-owner"
    assert result["activeSid"] == ("B" if switch_sid else "A")


@pytest.mark.parametrize("error", [409, 413, 503])
@pytest.mark.parametrize("force", [False, True])
def test_rejected_authority_read_keeps_transcript_and_clears_owned_hint(error, force):
    result = _run_case(mode="loader", error=error, force=force)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None
    assert result["statuses"] == [f"read-{error}"]


def test_auth_redirect_keeps_cached_messages_without_authority():
    result = _run_case(mode="loader", authRedirect=True, force=True)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert "revision" not in result["beforeRegeneration"]
    assert result["hint"] is None


def test_partial_window_above_live_ceiling_uses_existing_full_escape_hatch():
    result = _run_case(total=100, initialTotal=100, offset=30, ceiling=60)
    assert "msg_limit=" not in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 100
    assert result["beforeRegeneration"]["offset"] == 0


def test_same_session_force_reload_keeps_existing_full_window_behavior():
    result = _run_case(mode="loader", keepRevision=True, force=True)
    assert len(_message_requests(result)) == 1
    assert "msg_limit=" not in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None


def test_rejected_regeneration_restores_full_transcript_after_authority_fetch():
    result = _run_case(regenerate=True, regenReject=True)
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["streams"] == []
    assert result["messageCount"] == 80
    assert result["statuses"] == ["regen_failedsynthetic regeneration rejection"]
