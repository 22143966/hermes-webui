"""#8310 maintainer fold: Session.save() keeps the in-memory metadata counts aligned with the sidecar.

compact() prefers the load-time ``_metadata_message_count`` over the live array. Before this fold, save() wrote the
new counts to disk but left the load-time values on the object, so the clear route (truncate to zero, save, return
``s.compact()``) answered with the pre-clear total. #8310's /status card shows a "Total messages" row whenever that
total differs from the visible count, which turned the stale scalar into a phantom "Messages 0, Total 6" until the
next session load.
"""
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

import pytest


def _seed_session_dir(monkeypatch, tmp_path):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _rows():
    """Six rows: four chat rows, one tool row, one hidden delegation wakeup (raw 6, visible 5)."""
    return [
        {"id": "u1", "role": "user", "content": "first", "timestamp": 1.0},
        {"id": "a1", "role": "assistant", "content": "reply first", "timestamp": 2.0},
        {"id": "t1", "role": "tool", "content": "{}", "tool_call_id": "c1", "timestamp": 2.5},
        {"id": "w1", "role": "user", "content": "[wakeup]", "timestamp": 2.7, "_source": "delegation_wakeup"},
        {"id": "u2", "role": "user", "content": "second", "timestamp": 3.0},
        {"id": "a2", "role": "assistant", "content": "reply second", "timestamp": 4.0},
    ]


def _call_clear(monkeypatch, session_id):
    import api.routes as routes

    body_bytes = json.dumps({"session_id": session_id}).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(routes, "j", fake_j)
    handler = SimpleNamespace(headers={"Content-Length": str(len(body_bytes))}, rfile=BytesIO(body_bytes))
    routes.handle_post(handler, SimpleNamespace(path="/api/session/clear"))
    return captured


def test_clear_response_reports_zero_totals_for_a_loaded_session(monkeypatch, tmp_path):
    models = _seed_session_dir(monkeypatch, tmp_path)
    models.Session(session_id="f8310clear", messages=_rows()).save()
    models.SESSIONS.clear()  # the route loads from disk, caching the load-time counts (6 raw / 5 visible)

    loaded = models.Session.load("f8310clear")
    assert loaded.compact()["message_count"] == 6
    assert loaded.compact()["visible_message_count"] == 5
    models.SESSIONS.clear()

    captured = _call_clear(monkeypatch, "f8310clear")
    assert captured["payload"].get("ok") is True
    session = captured["payload"]["session"]
    assert session["message_count"] == 0
    assert session["visible_message_count"] == 0


def test_save_refreshes_counts_after_an_in_memory_append(monkeypatch, tmp_path):
    models = _seed_session_dir(monkeypatch, tmp_path)
    models.Session(session_id="f8310grow", messages=_rows()[:2]).save()
    loaded = models.Session.load("f8310grow")
    assert loaded.compact()["message_count"] == 2

    loaded.messages.append({"id": "u9", "role": "user", "content": "more", "timestamp": 9.0})
    loaded.save()
    assert loaded.compact()["message_count"] == 3
    assert loaded._metadata_message_count == 3
    assert loaded._metadata_visible_message_count == 3


def test_a_failed_write_keeps_the_previous_counts(monkeypatch, tmp_path):
    models = _seed_session_dir(monkeypatch, tmp_path)
    models.Session(session_id="f8310fail", messages=_rows()).save()
    loaded = models.Session.load("f8310fail")
    assert loaded._metadata_message_count == 6

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(models, "_replace_session_sidecar", boom)
    loaded.messages = loaded.messages[:2]
    with pytest.raises(OSError):
        loaded.save()
    # Nothing was persisted, so the object must not claim the new count.
    assert loaded._metadata_message_count == 6
    assert loaded._metadata_visible_message_count == 5
