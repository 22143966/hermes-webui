"""#7237 round 5 — the two regressions the Oct 11 re-gate reproduced.

The 10/11 re-gate replayed the production-composed probes on head
``369431458``: current head **11 failed / 3 passed**, prior-head control
**1 failed / 13 passed**. Both new regression families share one root
cause, and the reviewer supplied a scratch-verified repair direction
(47 tests, including the prior court checks, on that scratch).

**Regression 1 — adjacent assistants lose the next exchange.**
``_sanitize_messages_for_agent`` ran ``_merge_agent_replay_bare_assistant_rows``
BEFORE ``_sanitize_messages_for_api``. The cancelled-history block list
(``_replay_merge_blocked_by_cancelled_history``) then exempted rows whose
provenance the sanitizer would have stripped — so the outbound snapshot
kept adjacent bare assistants as separate rows while the installed Agent
still merges them. The reviewer's controlled replay: the answer to
``ANSWER follow-up`` was gone from the reloaded context.

**Regression 2 — verification supersession is defeated.**
The same pre-sanitizer assistant merge CONCATENATES a provisional
``verification_required`` row with its final replacement. The sanitizer's
own pass-0 merge (which owns the supersession rule: a
``verification_required`` row is REPLACED, not unioned) then never sees the
pair, so the provisional text and its finish reason survive.

The reviewer's direction: keep the user-row normalization where it is,
move the bare-assistant normalization AFTER ``_sanitize_messages_for_api``.
That keeps the block list reading raw provenance (the user merge does not
need it), lets the sanitizer's pass-0 own the assistant-adjacency
contract including supersession, and re-applies the assistant merge only
to the rows the sanitizer deliberately left as a bare pair.

These tests drive the REAL ``_sanitize_messages_for_agent`` and the real
merge/block helpers out of ``api/streaming.py``.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text, **extra):
    row = {"role": "assistant", "content": text}
    row.update(extra)
    return row


def _sanitize(messages, **kwargs):
    from api.streaming import _sanitize_messages_for_agent

    return _sanitize_messages_for_agent(copy.deepcopy(messages), **kwargs)


# ---------------------------------------------------------------------------
# Regression 2: verification supersession.
# ---------------------------------------------------------------------------


def test_a_verification_candidate_is_replaced_not_concatenated():
    """The reviewer's regression 2: provisional + final must not concatenate.

    A ``verification_required`` row followed by its final replacement is a
    supersession: the final row REPLACES the provisional one. The
    pre-sanitizer merge concatenated them, so the surviving row carried the
    provisional text AND its finish reason, and the sanitizer's own
    supersession rule never saw the pair.
    """
    messages = [
        _user("q"),
        _assistant("provisional answer", finish_reason="verification_required"),
        _assistant("final answer", finish_reason="stop"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"the superseded provisional row must be REPLACED, leaving one "
        f"assistant row; got {assistants}"
    )
    survivor = assistants[0]
    assert survivor.get("content") == "final answer", (
        f"the survivor must be the FINAL candidate, not a concatenation: "
        f"{survivor.get('content')!r}"
    )
    assert survivor.get("finish_reason") != "verification_required", (
        f"the provisional finish reason must not survive the supersession: "
        f"{survivor.get('finish_reason')!r}"
    )


def test_ordinary_adjacent_assistants_still_merge():
    """Control: the adjacency contract the pre-sanitizer merge existed for.

    Two plain adjacent assistant rows must still collapse into one turn, or
    the Agent replay diverges from the installed Agent's own repair.
    """
    messages = [
        _user("q"),
        _assistant("first part"),
        _assistant("second part"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        f"two plain adjacent assistants must still merge; got {assistants}"
    )
    content = assistants[0].get("content") or ""
    assert "first part" in content and "second part" in content, (
        f"the merged row must carry both texts; got {content!r}"
    )


def test_the_flag_off_keeps_adjacent_assistants_separate():
    """Control: the #8034 parity contract — the flag still gates the merge."""
    messages = [
        _user("q"),
        _assistant("first part"),
        _assistant("second part"),
        _user("next"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=False)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 2, (
        f"without the flag the rows must stay separate turns (#8034); got "
        f"{assistants}"
    )


# ---------------------------------------------------------------------------
# Regression 1: adjacent assistants keep the next exchange addressable.
# ---------------------------------------------------------------------------


def test_a_cancelled_history_row_keeps_the_next_exchange_addressable():
    """The reviewer's regression 1: the follow-up answer must survive.

    A cancelled-history splice leaves rows with durable provenance
    (``_state_db_row_id``) and a float clock. Those rows must stay
    individually addressable, and — critically — the merge must not leave
    the OUTBOUND snapshot holding separate rows for a pair the installed
    Agent will merge, which is what dropped the next exchange.
    """
    messages = [
        _user("first question"),
        _assistant("first answer"),
        _assistant(
            "late answer continuation",
            _state_db_row_id=41,
            timestamp=1.5,
        ),
        _user("follow-up"),
    ]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    # The durable rows stay separate (the cancelled-history contract)...
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 2, (
        f"the cancelled-history rows must stay individually addressable; "
        f"got {assistants}"
    )
    # ...and the follow-up turn is still there to be answered.
    users = [m for m in out if m.get("role") == "user"]
    assert any(m.get("content") == "follow-up" for m in users), (
        f"the follow-up prompt must survive the projection; got {users}"
    )
    # The provenance that makes the rows addressable is not leaked to the
    # wire: the sanitizer still strips it.
    for row in out:
        assert "_state_db_row_id" not in row, (
            f"internal provenance must not reach the provider payload: {row}"
        )


def test_a_float_clock_row_is_not_treated_as_a_merge_blocker_only():
    """The block list may keep a pair separate, but the exchange survives.

    This pins the reviewer's "separate durable-row preservation from
    outbound producer parity": the block list is about ADDRESSABILITY of
    the durable rows, never a licence to drop the following turn.
    """
    from api.streaming import _replay_merge_blocked_by_cancelled_history

    prev = _assistant("answer", timestamp=1.0)
    msg = _assistant("continuation", timestamp=2.0)
    nxt = _user("follow-up")
    assert _replay_merge_blocked_by_cancelled_history(prev, msg, nxt) is True
    # The blocked pair does not consume the following turn.
    messages = [_user("q"), prev, msg, nxt]
    out = _sanitize(messages, normalize_bare_assistant_adjacency=True)
    users = [m for m in out if m.get("role") == "user"]
    assert [m.get("content") for m in users] == ["q", "follow-up"], (
        f"the user turns must survive verbatim; got {users}"
    )


# ---------------------------------------------------------------------------
# Source shape: the ordering the reviewer's direction requires.
# ---------------------------------------------------------------------------


def test_the_assistant_merge_runs_after_the_api_sanitizer():
    """The ordering pin: the sanitizer's pass-0 owns supersession.

    The reviewer's scratch moved the bare-assistant normalization after
    ``_sanitize_messages_for_api`` so the pass-0 merge (which replaces a
    ``verification_required`` row instead of unioning it) sees the original
    pair. If the merge moves back before the sanitizer, regression 2
    reappears.
    """
    from api.streaming import _sanitize_messages_for_agent

    import inspect

    src = inspect.getsource(_sanitize_messages_for_agent)
    sanitizer_at = src.index("_sanitize_messages_for_api(")
    assistant_merge_at = src.index("_merge_agent_replay_bare_assistant_rows(")
    user_merge_at = src.index("_merge_agent_replay_bare_user_rows(")
    assert assistant_merge_at > sanitizer_at, (
        "the bare-assistant normalization must run AFTER the API sanitizer "
        "so the pass-0 merge owns the verification supersession"
    )
    assert user_merge_at < sanitizer_at, (
        "the user-row normalization stays BEFORE the sanitizer (the "
        "reviewer's direction)"
    )
