"""Regression for #4159 (salvage of PR #4224, hardened in #5106): clicking a
sidebar session-search result that matched on message CONTENT opens that
session AND scrolls to the matched message and flashes it.

Hardening properties pinned here (one block each):

1. Backend: content hits carry ``match_message_idx``, the RAW index of the hit
   in the FULL ``sess.messages`` list (hidden internal rows still count toward
   the index, because the /api/session payload keeps them; they just never
   match). Title hits carry no index.
2. Frontend: the full-session index is translated to the LOCAL rendered index.
   A truncated (paged) session is force-loaded through the uncapped
   ``_ensureAllMessagesLoaded()`` (``msg_limit=all``), never a bigger tail
   window, and a stale DOM row with the same local id never stands in for the
   target.
3. Assistant-message hits jump too (not only user rows).
4. Session-switch race: if the user switched (or started switching) sessions
   before the jump fires, nothing happens.
5. Virtualization: the jump is delegated to the transcript's
   virtualization-aware ``jumpToTurnQuestion`` (ui.js), which materializes a
   target that is outside the virtual window instead of assuming a DOM row.

Plus backward compatibility: a missing/invalid index just opens the session.

The frontend tests run the REAL functions extracted from static/*.js in node
against small deterministic stubs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse




# --------------------------------------------------------------------------- #
# 1. Backend: match_message_idx is surfaced on content hits
# --------------------------------------------------------------------------- #
def _run_search(query, *, session_messages, sessions_meta=None):
    import api.routes as routes

    meta = sessions_meta or [
        {"session_id": "s1", "title": "Untitled", "profile": "default"}
    ]
    session = SimpleNamespace(session_id="s1", messages=session_messages)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    # The content scan reads through get_session_for_scan (the LRU-transparent
    # accessor); patching get_session would leave the result set empty.
    with patch("api.routes.all_sessions", return_value=list(meta)), patch(
        "api.routes.get_session_for_scan", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def _hidden_row(text="ASYNC DELEGATION needle wakeup"):
    return {"role": "user", "content": text, "_source": "delegation_wakeup"}


def test_content_match_includes_message_index():
    msgs = [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "second message - no hit"},
        {"role": "user", "content": "NEEDLE in the third message"},
        {"role": "assistant", "content": "fourth message"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["status"] == 200
    results = captured["payload"]["sessions"]
    assert len(results) == 1
    hit = results[0]
    assert hit["match_type"] == "content"
    assert hit["match_message_idx"] == 2


def test_content_match_returns_first_hit_index_not_last():
    msgs = [
        {"role": "user", "content": "alpha NEEDLE first"},
        {"role": "user", "content": "beta NEEDLE second"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 0


def test_assistant_content_match_reports_its_index():
    msgs = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "the NEEDLE is in the reply"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=10", session_messages=msgs
    )
    assert captured["payload"]["sessions"][0]["match_message_idx"] == 1


def test_hidden_rows_count_toward_full_index_but_never_match_or_use_depth():
    """The index is into the FULL sess.messages list. Hidden delegation rows are
    in the /api/session payload (the renderer just skips them), so they must
    shift the index; they still neither match nor consume the depth budget."""
    msgs = [
        _hidden_row(),  # contains the needle but must not match
        _hidden_row("another hidden row"),
        {"role": "user", "content": "plain question"},
        {"role": "assistant", "content": "reply with the NEEDLE"},
    ]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1&depth=2", session_messages=msgs
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_message_idx"] == 3, (
        "raw index into sess.messages (hidden rows included), not an index "
        "into the filtered scan pool"
    )


def test_title_match_does_not_include_message_index():
    meta = [{"session_id": "s1", "title": "needle in the title", "profile": "default"}]
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
        sessions_meta=meta,
    )
    hit = captured["payload"]["sessions"][0]
    assert hit["match_type"] == "title"
    assert "match_message_idx" not in hit


def test_no_match_returns_empty_results():
    captured = _run_search(
        "/api/sessions/search?q=needle&content=1",
        session_messages=[{"role": "user", "content": "no hit here"}],
    )
    assert captured["payload"]["count"] == 0
