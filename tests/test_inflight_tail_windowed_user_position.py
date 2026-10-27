"""Windowed transcripts must keep the current turn's prompt above its activity rows.

Repro (WebUI, live session opened while `msg_limit` paging is active):

  * the first page is a TAIL window of the transcript, so it starts in the
    middle of the running turn and carries no `user` row at all;
  * `_mergeInflightTailMessages()` pulls the live tail in and, because that
    tail starts at the turn's `user` row, pushes the prompt onto the END of
    the window;
  * `renderMessages()` then paints the orphaned window rows as a standalone
    assistant turn above the user bubble — the reader sees "messages above my
    first message", and the prompt sits after its own progress narration.

The fix: when the window holds no `user` row and `_oldestIdx > 0`, put the
current turn's prompt in front of the window instead of appending it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _helper_src() -> str:
    src = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    start = src.find("function _messageComparableText")
    end = src.find("// Load older messages", start)
    assert start != -1 and end != -1, "sessions.js helper slice markers not found"
    return src[start:end]


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
