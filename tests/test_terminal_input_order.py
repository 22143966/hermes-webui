"""Terminal keystrokes must reach the shell in the order they were typed.

xterm.js calls ``onData`` once per key (or once per paste). Each call used to
start its own ``/api/terminal/input`` POST without waiting for the previous one.
The server handles requests on separate threads, so those POSTs could reach the
shell out of order and a fast-typed command arrived scrambled. The terminal now
keeps one input request in flight and sends keys typed meanwhile together in the
next request.

These tests drive the real ``static/terminal.js`` in Node with a fake xterm and
an ``api()`` whose requests resolve only when the test says so.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TERMINAL_JS = (REPO / "static" / "terminal.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

HARNESS = r"""
const calls=[];
let onData=null;
class FakeTerminal {
  constructor(){ this.cols=80; this.rows=24; this.options={}; }
  loadAddon(){}
  open(){}
  onData(cb){ onData=cb; }
  dispose(){}
}
global.window={addEventListener(){},Terminal:FakeTerminal};
global.document={
  documentElement:{classList:{contains(){return false;}}},
  getElementById(){return null;},
};
global.getComputedStyle=()=>({getPropertyValue(){return '';}});
global.$=(id)=>id==='terminalSurface'?{textContent:''}:null;
global.requestAnimationFrame=()=>1;
global.cancelAnimationFrame=()=>{};
global.setTimeout=()=>1;
global.clearTimeout=()=>{};
global.S={session:{session_id:'s1',workspace:'/tmp/ws'}};
global.t=(key)=>key;
const toasts=[];
global.showToast=(msg)=>toasts.push(msg);
global.syncTerminalButton=()=>{};
global.api=(path,opts)=>{
  if(path!=='/api/terminal/input')return Promise.resolve({});
  let resolve,reject;
  const p=new Promise((res,rej)=>{resolve=res;reject=rej;});
  const body=JSON.parse(opts.body);
  const {body:_b,method:_m,...rest}=opts;
  calls.push({sid:body.session_id,data:body.data,opts:rest,resolve,reject});
  return p;
};
""" + TERMINAL_JS + r"""
async function settle(){ for(let i=0;i<5;i++) await Promise.resolve(); }
function sent(){ return calls.map(c=>c.data); }
(async()=>{
  const out={};
  _ensureXterm();
  TERMINAL_UI.sessionId='s1';

  // Keys typed while the first request is in flight wait for it.
  'echo hi'.split('').forEach(ch=>onData(ch));
  await settle();
  out.while_first_in_flight=sent();
  out.first_opts=calls[0].opts;
  calls[0].resolve({});
  await settle();
  out.after_first_done=sent();
  calls[1].resolve({});
  await settle();
  out.after_second_done=sent();

  // A failed request drops the keys queued behind it (they would leave a gap in
  // the line), and keys typed afterwards go through.
  onData('\r');
  onData('x');
  onData('y');
  await settle();
  calls[2].reject(new Error('boom'));
  await settle();
  out.after_failure=sent();
  out.toasts=toasts.slice();
  onData('q');
  await settle();
  out.after_failure_next_key=sent();
  calls[3].resolve({});
  await settle();

  // Joined input never exceeds the server's 8192-character limit; a single
  // paste over it is sent alone, unchanged.
  calls.length=0;
  onData('k');
  onData('a'.repeat(5000));
  onData('b'.repeat(5000));
  onData('c');
  onData('d'.repeat(9000));
  await settle();
  for(let i=0;i<4;i++){ calls[i].resolve({}); await settle(); }
  out.capped_lengths=calls.map(c=>c.data.length);
  out.capped_joined=calls.map(c=>c.data).join('')==='k'+'a'.repeat(5000)+'b'.repeat(5000)+'c'+'d'.repeat(9000);

  // Closing drops unsent keys; the reopened shell's keys do not wait for the
  // old shell's request still in flight.
  calls.length=0;
  onData('a');
  onData('b');
  await settle();
  await closeComposerTerminal('s1',{skipApi:true});
  TERMINAL_UI.sessionId='s1';
  onData('z');
  await settle();
  out.after_reopen=sent();
  calls[0].resolve({});
  calls[1].resolve({});
  await settle();
  out.after_reopen_settled=sent();

  // Restarting drops unsent keys too.
  calls.length=0;
  onData('m');
  onData('n');
  await settle();
  TERMINAL_UI.open=true;
  TERMINAL_UI.collapsed=false;
  _startComposerTerminal=async()=>{};
  await restartComposerTerminal();
  calls[0].resolve({});
  await settle();
  onData('o');
  await settle();
  out.after_restart=sent();
  process.stdout.write(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1);});
"""


@pytest.fixture(scope="module")
def result():
    if NODE is None:
        pytest.skip("node not on PATH")
    proc = subprocess.run(
        [NODE, "-e", HARNESS], capture_output=True, text=True, timeout=30, cwd=REPO
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_keys_typed_during_a_request_wait_for_it(result):
    assert result["while_first_in_flight"] == ["e"]
    assert result["after_first_done"] == ["e", "cho hi"]
    assert result["after_second_done"] == ["e", "cho hi"]


def test_input_requests_are_not_retried(result):
    # A retry after a lost response would type the same keys twice.
    assert result["first_opts"]["retries"] == 0
    assert result["first_opts"]["timeoutToast"] is False
    assert 0 < result["first_opts"]["timeoutMs"] <= 10000


def test_a_failed_request_drops_the_keys_queued_behind_it(result):
    assert result["after_failure"] == ["e", "cho hi", "\r"]
    assert result["toasts"] == ["terminal_input_failedboom"]
    assert result["after_failure_next_key"] == ["e", "cho hi", "\r", "q"]


def test_joined_input_stays_within_the_server_limit(result):
    assert result["capped_lengths"] == [1, 5000, 5001, 9000]
    assert result["capped_joined"] is True


def test_closing_drops_unsent_keys_and_does_not_block_the_next_shell(result):
    assert result["after_reopen"] == ["a", "z"]
    assert result["after_reopen_settled"] == ["a", "z"]


def test_restarting_drops_unsent_keys(result):
    assert result["after_restart"] == ["m", "o"]
