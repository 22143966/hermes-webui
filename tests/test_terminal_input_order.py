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
  calls.push({sid:body.session_id,data:body.data,resolve,reject});
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
  calls[0].resolve({});
  await settle();
  out.after_first_done=sent();
  calls[1].resolve({});
  await settle();
  out.after_second_done=sent();

  // A failed request still lets later keys through, in order.
  onData('\r');
  onData('x');
  await settle();
  calls[2].reject(new Error('boom'));
  await settle();
  out.after_failure=sent();
  out.toasts=toasts.slice();
  calls[3].resolve({});
  await settle();

  // Keys not yet sent when the terminal closes are dropped, not sent to the
  // next shell; the request already in flight is left to finish.
  onData('a');
  onData('b');
  await settle();
  await closeComposerTerminal('s1',{skipApi:true});
  calls[4].resolve({});
  await settle();
  out.after_close=sent();
  TERMINAL_UI.sessionId='s1';
  onData('z');
  await settle();
  out.after_reopen=sent();
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


def test_a_failed_request_does_not_block_or_reorder_later_keys(result):
    assert result["after_failure"] == ["e", "cho hi", "\r", "x"]
    assert result["toasts"] == ["terminal_input_failedboom"]


def test_closing_drops_unsent_keys(result):
    assert result["after_close"] == ["e", "cho hi", "\r", "x", "a"]
    assert result["after_reopen"] == ["e", "cho hi", "\r", "x", "a", "z"]
