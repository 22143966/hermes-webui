"""Regression coverage for orphaned Desktop-origin sidecar pruning.

A conversation created in Hermes Desktop gets a WebUI-owned sidecar
(``webui/sessions/<id>.json`` plus an ``_index.json`` row) the first time it is
opened in the WebUI sidebar. If the user later deletes that conversation from
the Desktop app, the backing ``state.db`` row disappears but the sidecar stays,
so the stale row lingers in the sidebar forever.

#3238 / #4591 removed that shape for CLI and API-server imports by probing
``state.db`` directly through ``agent_session_rows_existing()``. A Desktop row
matched neither predicate — Desktop is not CLI, and it is not API-server — so
the existence probe never inspected it and no prune path could reach it.

These tests pin three things:

1. ``_is_desktop_sidecar_row()`` classifies only Desktop-origin rows.
2. The sidebar merge-loop predicate prunes a Desktop row whose backing
   ``state.db`` row is gone, and keeps one that is still backed.
3. The decision fails closed: when the agent DB is missing or unreadable,
   ``agent_session_rows_existing()`` reports "all present" and nothing is
   pruned, so a live Desktop conversation is never dropped by a transient
   read error.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _make_state_db(path: Path, session_ids, source: str = "desktop"):
    """Create a minimal agent state.db with a ``sessions`` table."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
            "started_at REAL, last_activity REAL)"
        )
        for sid in session_ids:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, last_activity) "
                "VALUES (?, ?, 0, 0)",
                (sid, source),
            )
        conn.commit()
    finally:
        conn.close()


# ── Predicate ────────────────────────────────────────────────────────────────


def test_desktop_sidecar_row_matches_every_source_marker():
    from api.routes import _is_desktop_sidecar_row

    for key in ("source", "source_tag", "raw_source", "session_source", "source_label"):
        assert _is_desktop_sidecar_row({"session_id": "s", key: "desktop"}) is True
        # Sidecar metadata stores the human label capitalised.
        assert _is_desktop_sidecar_row({"session_id": "s", key: "Desktop"}) is True


def test_webui_row_is_not_treated_as_desktop():
    from api.routes import _is_desktop_sidecar_row

    row = {
        "session_id": "webui-native",
        "source_tag": "webui",
        "raw_source": "webui",
        "source_label": "WebUI",
        "session_source": "webui",
        "is_cli_session": False,
    }
    assert _is_desktop_sidecar_row(row) is False


def test_cli_and_api_rows_are_not_treated_as_desktop():
    from api.routes import _is_desktop_sidecar_row

    assert _is_desktop_sidecar_row({"session_id": "c", "source_tag": "cli"}) is False
    assert (
        _is_desktop_sidecar_row({"session_id": "a", "source_tag": "api_server"})
        is False
    )


def test_cron_and_messaging_rows_are_not_treated_as_desktop():
    """The new predicate must not widen the prune to scheduler or chat rows."""
    from api.routes import _is_desktop_sidecar_row

    assert _is_desktop_sidecar_row({"session_id": "j", "source_tag": "cron"}) is False
    assert (
        _is_desktop_sidecar_row({"session_id": "m", "source": "telegram"}) is False
    )


def test_non_dict_row_is_not_desktop():
    from api.routes import _is_desktop_sidecar_row

    assert _is_desktop_sidecar_row(None) is False
    assert _is_desktop_sidecar_row("desktop") is False


# ── Merge-loop decision (mirrors the routes.py guard) ────────────────────────


def _is_orphaned_desktop_sidecar(row, cli_by_id, exists_fn):
    """Replicates the routes.py merge-loop predicate for a Desktop row."""
    from api.agent_sessions import is_cli_session_row
    from api.routes import (
        _is_api_server_sidecar_row,
        _is_desktop_sidecar_row,
        _session_source_is_webui,
    )

    sid = row.get("session_id")
    return bool(
        sid
        and (
            is_cli_session_row(row)
            or _is_api_server_sidecar_row(row)
            or _is_desktop_sidecar_row(row)
        )
        and not _session_source_is_webui(row)
        and sid not in cli_by_id
        and not exists_fn(sid)
    )


def test_desktop_orphan_is_pruned():
    """Desktop sidecar whose backing state.db row was deleted outside WebUI."""
    row = {
        "session_id": "20261022_175909_76f699",
        "source_tag": "desktop",
        "session_source": "other",
        "source_label": "Desktop",
        "is_cli_session": False,
        "title": "moved to zen",
        "message_count": 401,
    }
    assert _is_orphaned_desktop_sidecar(row, {}, exists_fn=lambda sid: False) is True


def test_desktop_row_still_backed_in_state_db_is_kept():
    """A live Desktop conversation must survive even when absent from cli_by_id."""
    row = {
        "session_id": "20261025_155311_a53b52",
        "source_tag": "desktop",
        "session_source": "other",
        "is_cli_session": False,
        "title": "handover",
        "message_count": 282,
    }
    assert _is_orphaned_desktop_sidecar(row, {}, exists_fn=lambda sid: True) is False


def test_desktop_row_listed_by_cli_visibility_is_kept():
    row = {"session_id": "d1", "source_tag": "desktop", "is_cli_session": False}
    assert (
        _is_orphaned_desktop_sidecar(row, {"d1": row}, exists_fn=lambda sid: False)
        is False
    )


def test_native_webui_row_is_never_pruned_by_desktop_path():
    row = {
        "session_id": "webui-native",
        "source_tag": "webui",
        "session_source": "webui",
        "is_cli_session": False,
    }
    assert _is_orphaned_desktop_sidecar(row, {}, exists_fn=lambda sid: False) is False


def test_empty_session_id_is_never_pruned():
    row = {"session_id": "", "source_tag": "desktop"}
    assert _is_orphaned_desktop_sidecar(row, {}, exists_fn=lambda sid: False) is False


# ── Fail-closed behaviour against a real state.db ────────────────────────────


def test_agent_session_rows_existing_reports_deleted_desktop_row(tmp_path, monkeypatch):
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["desktop-alive"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    existing = models.agent_session_rows_existing(["desktop-alive", "desktop-gone"])
    assert existing == frozenset({"desktop-alive"})


def test_desktop_prune_fails_closed_when_db_missing(tmp_path, monkeypatch):
    """No agent DB -> every id reports present -> a live row is never pruned."""
    from api import models

    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "nope" / "state.db"
    )
    wanted = ["desktop-a", "desktop-b"]
    assert models.agent_session_rows_existing(wanted) == frozenset(wanted)


def test_desktop_prune_fails_closed_on_unreadable_db(tmp_path, monkeypatch):
    """A non-SQLite file must degrade to "all present", never to "all gone"."""
    from api import models

    db = tmp_path / "state.db"
    db.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    wanted = ["desktop-c"]
    assert models.agent_session_rows_existing(wanted) == frozenset(wanted)


def test_desktop_prune_decision_uses_existence_probe_not_sidecar_messages(
    tmp_path, monkeypatch
):
    """End-to-end shape: probe says gone -> prune decision is True."""
    from api import models

    home = tmp_path / "home"
    home.mkdir()
    _make_state_db(home / "state.db", ["someone-else"])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")

    def exists_fn(sid):
        return sid in models.agent_session_rows_existing([sid])

    row = {
        "session_id": "desktop-orphan",
        "source_tag": "desktop",
        "is_cli_session": False,
        "message_count": 401,
    }
    assert _is_orphaned_desktop_sidecar(row, {}, exists_fn) is True


# ── Real sidebar builder ────────────────────────────────────────────────────


def _desktop_row(sid: str, *, profile: str = "default") -> dict:
    return {
        "session_id": sid,
        "profile": profile,
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "other",
        "source_label": "Desktop",
        "is_cli_session": False,
        "title": "Desktop conversation",
        "message_count": 1,
        "updated_at": 1,
    }


def _run_actual_builder(
    monkeypatch,
    rows: list[dict],
    *,
    show_cli_sessions: bool,
    all_profiles: bool = False,
    cli_rows: list[dict] | None = None,
):
    """Run the production builder with only unrelated I/O isolated."""
    import api.routes as routes

    pruned: list[str] = []
    monkeypatch.setattr(routes, "all_sessions", lambda **_kwargs: [dict(row) for row in rows])
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda **_kwargs: [dict(row) for row in (cli_rows or [])],
    )
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False
    )
    monkeypatch.setattr(
        routes,
        "_prune_orphaned_webui_zero_message_sessions",
        lambda current, **_kwargs: list(current),
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "prune_session_from_index", pruned.append)

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=all_profiles,
        show_cli_sessions=show_cli_sessions,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return payload, pruned


def test_actual_builder_prunes_desktop_orphan_with_cli_sessions_hidden(monkeypatch):
    """The established-install path must reconcile Desktop rows too."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing_strict",
        lambda ids, profile=None: frozenset(),
        raising=False,
    )
    payload, pruned = _run_actual_builder(
        monkeypatch,
        [_desktop_row("desktop-gone")],
        show_cli_sessions=False,
    )

    assert payload["sessions"] == []
    assert pruned == ["desktop-gone"]


def test_actual_builder_prunes_desktop_orphan_with_cli_sessions_shown(monkeypatch):
    """The new pass must also preserve the existing toggle-on behavior."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing_strict",
        lambda ids, profile=None: frozenset(),
        raising=False,
    )
    payload, pruned = _run_actual_builder(
        monkeypatch,
        [_desktop_row("desktop-gone")],
        show_cli_sessions=True,
    )

    assert payload["sessions"] == []
    assert pruned == ["desktop-gone"]


def test_actual_builder_keeps_live_desktop_row(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing_strict",
        lambda ids, profile=None: frozenset(ids),
        raising=False,
    )
    payload, pruned = _run_actual_builder(
        monkeypatch,
        [_desktop_row("desktop-live")],
        show_cli_sessions=False,
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["desktop-live"]
    assert pruned == []


def test_actual_builder_keeps_desktop_row_listed_by_cli_visibility(monkeypatch):
    """A currently enumerated agent row remains a stronger live signal."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing_strict",
        lambda ids, profile=None: frozenset(),
        raising=False,
    )
    row = _desktop_row("desktop-enumerated")
    payload, pruned = _run_actual_builder(
        monkeypatch,
        [row],
        show_cli_sessions=True,
        cli_rows=[row],
    )

    assert [item["session_id"] for item in payload["sessions"]] == [
        "desktop-enumerated"
    ]
    assert pruned == []


def test_all_profiles_uses_row_owning_db_for_destructive_absence(
    tmp_path, monkeypatch
):
    """A named-profile miss must never be answered by the active profile DB."""
    from api import models

    active_home = tmp_path / "active"
    active_home.mkdir()
    _make_state_db(active_home / "state.db", ["same-id"])
    named_home = tmp_path / "named"
    named_home.mkdir()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: active_home / "state.db")
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: named_home)

    payload, pruned = _run_actual_builder(
        monkeypatch,
        [_desktop_row("same-id", profile="work")],
        show_cli_sessions=False,
        all_profiles=True,
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["same-id"]
    assert pruned == []


def test_all_profiles_prune_matrix_for_named_profile_db(tmp_path, monkeypatch):
    """Live/missing/corrupt rows retain; only a confirmed deletion prunes."""
    from api import models

    active_home = tmp_path / "active"
    active_home.mkdir()
    _make_state_db(
        active_home / "state.db",
        ["missing-db", "live-row", "corrupt-db", "deleted-row"],
    )
    homes: dict[str, Path] = {}
    for profile in ("missing", "live", "corrupt", "deleted"):
        homes[profile] = tmp_path / profile
        homes[profile].mkdir()
    _make_state_db(homes["live"] / "state.db", ["live-row"])
    (homes["corrupt"] / "state.db").write_text("not sqlite", encoding="utf-8")
    _make_state_db(homes["deleted"] / "state.db", [])

    monkeypatch.setattr(models, "_active_state_db_path", lambda: active_home / "state.db")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: homes[profile])

    rows = [
        _desktop_row("missing-db", profile="missing"),
        _desktop_row("live-row", profile="live"),
        _desktop_row("corrupt-db", profile="corrupt"),
        _desktop_row("deleted-row", profile="deleted"),
    ]
    payload, pruned = _run_actual_builder(
        monkeypatch,
        rows,
        show_cli_sessions=False,
        all_profiles=True,
    )

    assert {row["session_id"] for row in payload["sessions"]} == {
        "missing-db",
        "live-row",
        "corrupt-db",
    }
    assert pruned == ["deleted-row"]
