"""Round-5 re-gate regression tests for #7882 (greptile 2026-10-10 inline P2).

Finding (static/sessions.js, `upsertActiveSessionForLocalTurn`): after an
idle transcript reload, `_ensureMessagesLoaded` (and the sibling reload
paths `_ensureAllMessagesLoaded` and the gateway CLI refresh) set
`_visibleBaselineLocal` WITHOUT `_visibleBaselineServer`. The next local
send skips the pair initialization because the local baseline already
exists, so the bump computes
`visible_message_count = undefined + optimisticVisible` → NaN, the NaN
comparison loses `Math.max`, and the sidebar falls back to the RAW count —
counting hidden delegation wakeups again.

Fix: every reload path re-baselines BOTH halves together, and the bump
self-heals a missing server half from the local baseline.
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    if NODE_BIN is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        import subprocess

        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


_VM_COMMON = r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}
const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};
"""


def _updater_body() -> str:
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    return SESSIONS_SRC[start:end]


# ── Finding: NaN-poisoned bump after an idle transcript load ───────────────


@_node_tests
def test_send_after_reload_only_local_baseline_does_not_go_nan():
    """The exact greptile reproduction: a reload path set ONLY the local
    baseline (simulated by the test pre-seeding the pair the way
    `_ensureMessagesLoaded` used to). The next send must still produce a
    finite visible count that excludes hidden wakeups — never NaN, which
    the sidebar falls back from to the raw count."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + r"""
const S = {
  session: {
    session_id: 'sid-nan-7882',
    title: 'Chat with wakeup',
    message_count: 4,             // server raw total: 3 visible + 1 hidden
    visible_message_count: 3,     // server visible total
    // Reload-path shape: local half set, server half left undefined —
    // exactly what _ensureMessagesLoaded produced before the fix.
    _visibleBaselineLocal: 3,
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};

"""
        + _updater_body()
        + r"""

upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["visible"] == result["visible"], (
        "Visible count went NaN — an undefined _visibleBaselineServer "
        f"poisoned the bump and the sidebar would fall back to the raw "
        f"count, got {result}"
    )
    assert result["raw"] == 5
    # 3 visible + 1 send = 4: the hidden wakeup stays excluded.
    assert result["visible"] == 4, (
        f"Visible count must stay 4 (3 visible + 1 send), got {result}"
    )


@_node_tests
def test_send_after_full_reload_uses_fresh_server_baseline():
    """After a real idle reload (both baselines set together), a send bumps
    the SERVER visible total by exactly its own rows — the fresh server
    half from the reload response is authoritative, so the wakeup that the
    reload absorbed stays excluded."""
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + _VM_COMMON
        + r"""
const S = {
  session: {
    session_id: 'sid-reload-7882',
    title: 'Reloaded chat',
    message_count: 4,
    visible_message_count: 3,     // fresh value from the reload response
    // Both halves, as _ensureMessagesLoaded now sets them: the server
    // half adopts the reload's authoritative visible total.
    _visibleBaselineLocal: 3,
    _visibleBaselineServer: 3,
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};

"""
        + _updater_body()
        + r"""

upsertActiveSessionForLocalTurn({messageCount: 5, localVisibleBeforePush: 3});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    # 3 visible + 1 send = 4 — no NaN, no raw-count leak.
    assert result["visible"] == 4, (
        f"Send after reload must bump exactly its own rows (3+1=4), got {result}"
    )


# ── Source-level pin: reload paths must set both halves together ──────────


def test_ensure_messages_loaded_rebaselines_both_halves():
    """_ensureMessagesLoaded must reset _visibleBaselineServer alongside
    _visibleBaselineLocal when idle — the local-only baseline was the bug."""
    fn = _extract_fn("_ensureMessagesLoaded")
    assert "_visibleBaselineLocal=" in fn
    assert "_visibleBaselineServer=" in fn, (
        "_ensureMessagesLoaded re-baselines only the local half — the next "
        "send's bump goes NaN and the sidebar counts hidden wakeups again"
    )


def test_ensure_all_messages_loaded_rebaselines_both_halves():
    """_ensureAllMessagesLoaded must reset both halves together too."""
    fn = _extract_fn("_ensureAllMessagesLoaded")
    assert "_visibleBaselineLocal=" in fn
    assert "_visibleBaselineServer=" in fn, (
        "_ensureAllMessagesLoaded re-baselines only the local half"
    )


def test_gateway_refresh_rebaselines_both_halves():
    """The gateway CLI-refresh path replaces the transcript while idle; it
    must also move both baselines together."""
    assert "_visibleBaselineLocal=" in SESSIONS_SRC
    assert "_visibleBaselineServer=" in SESSIONS_SRC
    # The self-heal in the updater makes a forgotten path non-NaN even if a
    # future reload path misses its half.
    updater = _updater_body()
    assert "typeof S.session._visibleBaselineServer!=='number'" in updater


def _extract_fn(name: str) -> str:
    marker = f"function {name}("
    start = SESSIONS_SRC.index(marker)
    i = SESSIONS_SRC.index("{", start) + 1
    depth = 1
    while depth and i < len(SESSIONS_SRC):
        if SESSIONS_SRC[i] == "{":
            depth += 1
        elif SESSIONS_SRC[i] == "}":
            depth -= 1
        i += 1
    return SESSIONS_SRC[start:i]
