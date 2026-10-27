"""Windowed transcripts must place the current turn's prompt by turn ownership.

Repro (WebUI, live session opened while `msg_limit` paging is active):

  * the first page is a TAIL window of the transcript, so it can start in the
    middle of the running turn and carry no `user` row at all;
  * `_mergeInflightTailMessages()` pulls the live tail in and, because that
    tail starts at the turn's `user` row, pushed the prompt onto the END of
    the window;
  * `renderMessages()` then painted the orphaned window rows as a standalone
    assistant turn above the user bubble — the reader sees "messages above my
    first message", and the prompt sits after its own progress narration.

A user-free window alone does NOT prove the rows belong to the running turn:
with deferred session saving the last window can be entirely the PREVIOUS
completed turn while the new prompt is still unpersisted. Placement therefore
follows turn/start ownership (`pending_started_at` vs persisted `_ts`, plus
`_live` rows) instead of the shape of the window.

A prompt recovered from a window is display-only: its canonical index is
outside the loaded window (or not persisted yet), so it must never shift the
absolute index of the real window rows that fork/edit/regenerate and the
older-page cursor are computed from.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
UI_JS_PATH = ROOT / "static" / "ui.js"
COMMANDS_JS_PATH = ROOT / "static" / "commands.js"
NODE = shutil.which("node")


def _helper_src() -> str:
    src = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    start = src.find("function _messageComparableText")
    end = src.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper slice markers not found"
    return src[start:end]


def _function(src: str, name: str) -> str:
    marker = "function " + name + "("
    plain = src.find(marker)
    async_at = src.find("async " + marker)
    candidates = [p for p in (plain, async_at) if p != -1]
    assert candidates, f"{name} not found"
    start = min(candidates)
    brace = src.index("{", start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated function {name}")


def _function_optional(src: str, name: str) -> str:
    try:
        return _function(src, name)
    except AssertionError:
        return ""


def _index_helpers() -> str:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    helpers = (
        "_displayOnlyIndexState",
        "_markDisplayOnlyRow",
        "_displayOnlyRawIndices",
        "_displayOnlyCountBefore",
        "_displayOnlyRowAt",
        "_messageSessionIndexBase",
        "_messageSessionIndexForRawIdx",
        "_messageRawIdxForSessionIndex",
        "_canonicalRawIdxForSessionIndex",
        "_rowCurrentRawIdx",
    )
    parts = [chunk for chunk in (_function_optional(src, n) for n in helpers) if chunk]
    if "_canonicalRawIdxForSessionIndex" not in "\n".join(parts):
        # Base blob has no canonical helper; keep the RED control semantic
        # (fork payload 33 instead of 32) instead of failing at extraction.
        parts.append(
            "function _canonicalRawIdxForSessionIndex(i){return Number(i)-_messageSessionIndexBase();}"
        )
    return "\n".join(parts)


def _fork_caller() -> str:
    return _function(COMMANDS_JS_PATH.read_text(encoding="utf-8"), "forkFromMessage")


def _run(script: str) -> str:
    assert NODE, "node is required for this test"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def test_windowed_tail_keeps_prompt_above_activity_rows():
    """A tail window that starts mid-turn must not swallow the prompt to the end."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 16;
{helper_src}

// First page = transcript[16:], which is inside the running turn: activity
// rows only, no user row. The live tail still starts at that turn's prompt.
const base = [
  {{role:'assistant', content:'icons generated, starting the first build'}},
  {{role:'tool', content:'{{"status":"success"}}'}},
  {{role:'assistant', content:'build failed in 11 places'}},
];
const inflight = [
  {{role:'user', content:'build me a todo app with a sticky notification'}},
  {{role:'assistant', _live:true, content:'now running the JVM tests'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(
  merged.map(m => m.role),
  ['user', 'assistant', 'tool', 'assistant', 'assistant'],
  'prompt must stay in front of the loaded window rows'
);
assert.strictEqual(merged[0].content, 'build me a todo app with a sticky notification');
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
assert.strictEqual(merged[merged.length - 1]._live, true);
"""
    _run(script)


def test_full_transcript_still_dedupes_prompt_in_place():
    """Full transcripts keep the prompt where it already is (no duplicate, no move).

    Real callers run `_dropCurrentTurnAssistantMessages(S.messages)` first, so a
    full transcript reaches the merge with its current-turn assistant rows
    already removed — the tail user row is then the last row.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 0;
{helper_src}

const base = [
  {{role:'user', content:'go'}},
];
const inflight = [
  {{role:'user', content:'go'}},
  {{role:'assistant', _live:true, content:'first progress'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.deepStrictEqual(merged.map(m => m.role), ['user', 'assistant']);
assert.strictEqual(merged[0].content, 'go');
assert.strictEqual(merged[merged.length - 1]._live, true);
"""
    _run(script)


def test_windowed_tail_places_pending_prompt_before_the_window():
    """The pending-user recovery path needs the same windowed placement rule.

    `_mergeInflightTailMessages()` alone is not enough: the live restore also
    runs `_mergePendingSessionMessage()`, which splices the pending prompt in
    front of the live tail. With a mid-turn window that leaves the prompt after
    its own activity rows, so it must go in front of the window instead.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 120;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const live = {{role:'assistant', _live:true, content:'running'}};
const messages = [
  {{role:'assistant', content:'icons generated'}},
  {{role:'tool', content:'{{"status":"success"}}'}},
  live,
];
const changed = _mergePendingSessionMessage({{pending_user_message:'build me a todo app', pending_started_at:1}}, messages);
assert.strictEqual(changed, true);
assert.deepStrictEqual(
  messages.map(m => m.role),
  ['user', 'assistant', 'tool', 'assistant'],
  'pending prompt must lead the windowed transcript'
);
assert.strictEqual(messages[0].content, 'build me a todo app');
assert.strictEqual(messages[messages.length - 1]._live, true);
"""
    _run(script)


def test_window_with_an_earlier_turn_keeps_pending_prompt_before_live():
    """When the window already holds a turn, the pending prompt still goes before the live tail."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 40;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const live = {{role:'assistant', _live:true, content:'running'}};
const messages = [
  {{role:'user', content:'earlier turn'}},
  {{role:'assistant', content:'earlier answer'}},
  live,
];
const changed = _mergePendingSessionMessage({{pending_user_message:'current turn', pending_started_at:3}}, messages);
assert.strictEqual(changed, true);
assert.deepStrictEqual(
  messages.map(m => m.role),
  ['user', 'assistant', 'user', 'assistant'],
  'pending prompt belongs after the earlier turn, directly before the live tail'
);
assert.strictEqual(messages[0].content, 'earlier turn');
assert.strictEqual(messages[2].content, 'current turn');
"""
    _run(script)


def test_previous_turn_window_does_not_claim_the_new_prompt():
    """A window made only of the previous turn must not put the new prompt on top.

    Deferred session saving keeps the new prompt out of the persisted
    transcript while the tail window still ends at the previous completed turn:
    every row has `_ts` before `pending_started_at`, so none of them belongs to
    the running turn. The prompt belongs AFTER those rows.
    """
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

// Persisted window: 30 rows of the PREVIOUS turn (timestamps 32..61), while
// the new turn starts at 100 and its prompt is still unsaved.
const base = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'previous turn activity ' + i,
  _ts: 32 + i,
}}));
const inflight = [
  {{role:'user', content:'brand new prompt', _ts:100}},
  {{role:'assistant', _live:true, content:'working on it'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged[0].role, 'assistant', 'previous-turn activity stays above the new prompt');
assert.strictEqual(merged[merged.length - 1]._live, true);
assert.strictEqual(merged[merged.length - 2].content, 'brand new prompt');
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def test_previous_turn_window_keeps_pending_prompt_after_older_rows():
    """Pending recovery must use the same ownership rule for the previous-turn window."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

function getPendingSessionMessage(session, messages) {{
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {{role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true}};
}}

const messages = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'previous turn activity ' + i,
  _ts: 32 + i,
}}));
messages.push({{role:'assistant', _live:true, content:'working on it'}});
const changed = _mergePendingSessionMessage({{pending_user_message:'brand new prompt', pending_started_at:100}}, messages);
assert.strictEqual(changed, true);
assert.strictEqual(messages[0].role, 'assistant');
assert.strictEqual(messages[messages.length - 1]._live, true);
assert.strictEqual(messages[messages.length - 2].content, 'brand new prompt');
assert.strictEqual(messages.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def test_current_turn_window_still_places_prompt_first():
    """Timestamps at/after the turn start prove the window is the running turn's suffix."""
    helper_src = _helper_src()
    script = f"""
const assert = require('assert');
let _oldestIdx = 31;
{helper_src}

const base = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'current turn activity ' + i,
  _ts: 100 + i,
}}));
const inflight = [
  {{role:'user', content:'current prompt', _ts:100}},
  {{role:'assistant', _live:true, content:'working on it'}},
];
const merged = _mergeInflightTailMessages(base, inflight);
assert.strictEqual(merged[0].content, 'current prompt');
assert.strictEqual(merged[0]._displayOnlyRow, true);
assert.strictEqual(merged.filter(m => m.role === 'user').length, 1);
"""
    _run(script)


def _fork_harness(index_helpers: str, helper_src: str, fork_src: str, oldest_idx: int = 31) -> str:
    """Shared node prelude: real index helpers + real merge helpers + real fork caller."""
    return f"""
const assert = require('assert');
var S = {{session:{{session_id:'s1', pending_started_at:100}}, messages:[], busy:false}};
let _oldestIdx = {oldest_idx};
{index_helpers}
{helper_src}
const forkPayloads = [];
function api(url, opts) {{
  forkPayloads.push(JSON.parse(opts.body));
  return Promise.resolve({{session_id:'forked'}});
}}
async function loadSession() {{}}
async function renderSessionList() {{}}
async function _ensureAllMessagesLoaded() {{}}
function showToast() {{}}
function t(k) {{ return k; }}
function _isReadOnlySession() {{ return false; }}
function _isBranchableReadOnlySession() {{ return false; }}
{fork_src}
function rowFor(rawIdx) {{
  return {{dataset:{{msgIdx:String(rawIdx), sessionMsgIdx:String(_messageSessionIndexForRawIdx(rawIdx))}}}};
}}
async function forkAt(rawIdx) {{
  await forkFromMessage(rawIdx + 1, {{closest:() => rowFor(rawIdx)}});
  return forkPayloads[forkPayloads.length - 1].keep_count;
}}
const windowRows = Array.from({{length:30}}, (_, i) => ({{
  role: i % 4 === 1 ? 'tool' : 'assistant',
  content: 'current turn activity ' + i,
  _ts: 100 + i,
}}));
const currentPrompt = {{role:'user', content:'current prompt', _ts:100, _pending:true}};
"""


def test_recovered_prompt_does_not_shift_canonical_window_indices():
    """Both recovery sources must keep `keep_count` on the canonical window row.

    The window begins at absolute index 31. Inserting a recovered prompt at the
    head must not make canonical row 31 look like absolute row 32 — the real
    fork caller would then send 33 instead of 32.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    script = (
        _fork_harness(index_helpers, helper_src, fork_src)
        + """
function getPendingSessionMessage(session, messages) {
  const text = String((session && session.pending_user_message) || '').trim();
  if (!text) return null;
  return {role:'user', content:text, _ts:(session.pending_started_at || 10), _pending:true};
}

(async () => {
  // Source A: INFLIGHT live-tail recovery.
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {role:'assistant', _live:true, content:'working on it'},
  ]);
  assert.strictEqual(S.messages[0].content, 'current prompt');
  assert.strictEqual(S.messages[1].content, 'current turn activity 0');
  assert.strictEqual(_messageSessionIndexForRawIdx(0), null, 'display-only rows have no canonical index');
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const inflightKeep = await forkAt(1);

  // Source B: pending-user recovery on a prompt-free window.
  S.messages = windowRows.slice();
  const changed = _mergePendingSessionMessage({pending_user_message:'current prompt', pending_started_at:100}, S.messages);
  assert.strictEqual(changed, true);
  assert.strictEqual(S.messages[0].content, 'current prompt');
  assert.strictEqual(S.messages[0]._displayOnlyRow, true);
  assert.strictEqual(_messageSessionIndexForRawIdx(1), 31);
  const pendingKeep = await forkAt(1);

  console.log(JSON.stringify({inflightKeep, pendingKeep}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    assert '"inflightKeep":32' in out, out
    assert '"pendingKeep":32' in out, out


def test_fork_payload_survives_older_page_load():
    """Paging into the canonical prompt must drop the display copy and keep indices exact.

    Runs the real `_loadOlderMessages()` against a stubbed transport. The older
    page contains the canonical user row (absolute 10), so the display-only
    prompt must be reconciled away rather than duplicated; fork on absolute 31
    still sends 32 afterwards.
    """
    index_helpers = _index_helpers()
    helper_src = _helper_src()
    fork_src = _fork_caller()
    bump_generation = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_bumpMessagesGeneration")
    load_older = _function(SESSIONS_JS_PATH.read_text(encoding="utf-8"), "_loadOlderMessages")
    script = (
        _fork_harness(index_helpers, helper_src, fork_src)
        + f"""
let _messagesTruncated = true;
let _loadingOlder = false;
let _loadingSessionId = 's1';
let _msgLimitMax = 500;
let _messageRenderWindowSize = 50;
let _messagesGeneration = 0;
const _INITIAL_MSG_LIMIT = 30;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
var window = {{}};
{bump_generation}

// Full canonical transcript: row 10 is the still-running turn's user prompt.
const transcript = Array.from({{length:61}}, (_, i) => ({{
  role: i === 10 ? 'user' : (i % 4 === 1 ? 'tool' : 'assistant'),
  content: i === 10 ? 'current prompt' : 'row ' + i,
  _ts: i,
}}));
function page(rows, offset, truncated) {{
  return {{session:{{messages:rows, _messages_offset:offset, _messages_truncated:truncated, tool_calls:[]}}}};
}}
function api(url, opts) {{
  if (opts && opts.method === 'POST') {{
    forkPayloads.push(JSON.parse(opts.body));
    return Promise.resolve({{session_id:'forked'}});
  }}
  if (url.indexOf('msg_before=31') !== -1) return Promise.resolve(page(transcript.slice(1, 31), 1, true));
  return Promise.resolve(page(transcript.slice(0, 61), 0, false));
}}
function $() {{ return null; }}
function renderMessages() {{}}
function _syncToolCallsForLoadedMessages() {{}}
function _advanceVisibleBaselineForServerKnownRows() {{}}
function _currentMessageRenderWindowSize() {{ return 50; }}
function msgContent(m) {{ return String((m && m.content) || ''); }}
{load_older}

(async () => {{
  // The running turn starts at absolute row 10 (ts 10), so the window rows
  // (ts 100+) and the recovered prompt share that turn start.
  currentPrompt._ts = 10;
  S.session.pending_started_at = 10;
  S.messages = _mergeInflightTailMessages(windowRows, [
    currentPrompt,
    {{role:'assistant', _live:true, content:'working on it'}},
  ]);
  const before = await forkAt(1);
  await _loadOlderMessages();
  assert.strictEqual(S.messages.filter(m => m.role === 'user' && m.content === 'current prompt').length, 1,
    'the canonical row replaces the display-only copy');
  const canonicalIdx = S.messages.findIndex(m => m.role === 'user' && m.content === 'current prompt');
  assert.strictEqual(canonicalIdx, 9);
  const windowStart = S.messages.findIndex(m => m.content === 'current turn activity 0');
  assert.strictEqual(_oldestIdx + windowStart, 31, 'absolute index of the window head is preserved');
  const after = await forkAt(windowStart);
  console.log(JSON.stringify({{before, after, messages: S.messages.length}}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    )
    out = _run(script)
    assert '"before":32' in out, out
    assert '"after":32' in out, out
