"""Regression tests for #5132 — cap sidebar state.db count/title overrides to top-N.

On power users with thousands of sessions, GET /api/sessions blocked 5-18s in the
all_sessions.state_db_overrides stage: _apply_sidebar_state_db_overrides probed
state.db for EVERY row (2400+ ids) on every concurrent poll, piling up sqlite reads
against the gateway-watcher + agent writes and flapping the UI to "Connection lost".

The fix is a TWO-TIER split (the cap on the whole probe was the first cut and was a
regression — see test_source_classification_is_never_capped below):

  * SOURCE classification (source/title) is read for ALL rows. It feeds the CLI/WebUI
    sidebar filter that runs BEFORE the lazy lineage correction, so capping it would
    silently drop rows whose stale JSON source disagrees with state.db.
  * The expensive MESSAGES aggregation (COUNT(*)/MAX(timestamp) GROUP BY) — the actual
    bottleneck — is capped to the top-N (default 60) paint-priority rows.

The cap is env-configurable (HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N) and fails open.

These tests pin: (1) source ids are NEVER capped; (2) count ids ARE capped to top-N;
(3) the env override bounds the COUNT tier; (4) a non-positive / unparseable cap;
(5) lists at/under the cap; (6) a DB error never breaks /api/sessions; (7) a probed
row's webui source is applied; (8) the end-to-end route regression: a stale-CLI
JSON row whose state.db source is webui must stay visible in a WebUI-filtered sidebar
even when it falls beyond the cap; and (9) the cap also bounds the per-row transcript
reconciliation, not just the SQL aggregate.
"""
from __future__ import annotations

import api.models as models


def _capture_probed_ids(monkeypatch):
    """Patch _read_state_db_sidebar_overrides to record both id tiers it is asked for."""
    seen = {}

    def _fake_read(db_path, id_set, count_session_ids=None):
        seen["source_ids"] = set(id_set)
        seen["count_ids"] = None if count_session_ids is None else set(count_session_ids)
        return {}

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return seen


def _sessions(n):
    # Caller passes an already pinned-first/newest-first sorted list; index order
    # therefore IS paint priority. id "s0" is the most-recent/visible-most.
    return [{"session_id": f"s{i}"} for i in range(n)]


def test_source_classification_is_never_capped(monkeypatch):
    """SOURCE tier must cover ALL rows (capping it dropped rows from the sidebar — #5132 regression)."""
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["source_ids"] == {f"s{i}" for i in range(1000)}, (
        "Source/title classification must be read for EVERY row, never capped"
    )


def test_count_aggregation_capped_to_top_n_default_60(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(60)}, (
        "The expensive message-count aggregation must be capped to the top-60"
    )


def test_env_override_changes_count_cap(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "50")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(50)}, (
        "HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N must bound the COUNT tier"
    )
    assert seen["source_ids"] == {f"s{i}" for i in range(1000)}, (
        "...but the source tier stays uncapped"
    )


def test_non_positive_cap_disables_capping(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    models._apply_sidebar_state_db_overrides(_sessions(500))
    assert seen["count_ids"] is None, "cap<=0 must disable the count cap (count every row)"
    assert len(seen["source_ids"]) == 500


def test_unparseable_cap_falls_back_to_default(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "not-a-number")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["count_ids"] == {f"s{i}" for i in range(60)}, (
        "An unparseable cap must fall back to the default 60, not crash"
    )


def test_list_under_cap_counts_everything(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(50))
    assert seen["count_ids"] is None, "A list at/under the cap counts every row (no cap needed)"
    assert seen["source_ids"] == {f"s{i}" for i in range(50)}


def test_override_failure_is_swallowed(monkeypatch):
    """Override lookup must fail open — a DB error never breaks /api/sessions."""
    def _boom(db_path, id_set, count_session_ids=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _boom)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    # Must not raise.
    models._apply_sidebar_state_db_overrides(_sessions(10))


def test_capped_rows_still_receive_source_overrides(monkeypatch):
    """A row beyond the COUNT cap must still get its SOURCE metadata applied."""
    def _fake_read(db_path, id_set, count_session_ids=None):
        # Return a webui source override for s400 (beyond the default cap).
        return {
            "s400": {
                "_state_db_source": "webui",
                "_state_db_source_tag": "webui",
                "_state_db_raw_source": "webui",
                "_state_db_session_source": "webui",
                "_state_db_source_label": "WebUI",
            }
        }

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    sessions: list[dict] = _sessions(500)
    sessions[400]["is_cli_session"] = True
    models._apply_sidebar_state_db_overrides(sessions)
    assert sessions[400]["is_cli_session"] is False, (
        "A row beyond the count cap with a webui state.db source must STILL be corrected "
        "(source classification is uncapped)"
    )


def test_stale_cli_json_beyond_cap_stays_webui_via_real_db(monkeypatch, tmp_path):
    """End-to-end #5132 regression: a stale-CLI JSON row whose state.db source is webui,
    sitting BEYOND the top-N cap, must keep source='webui' so it isn't filtered out of a
    WebUI-only sidebar. This is the exact row that vanished under the first (cap-everything) cut.
    """
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT, message_count INTEGER)")
    # The stale row lives at index 400 (beyond the default cap). state.db says it's webui.
    conn.execute("INSERT INTO sessions (id, source, title, message_count) VALUES (?,?,?,?)",
                 ("s400", "webui", "Stale CLI row", 3))
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)

    sessions = _sessions(500)
    # JSON metadata wrongly marks it CLI.
    sessions[400]["is_cli_session"] = True
    sessions[400]["session_source"] = "cli"
    models._apply_sidebar_state_db_overrides(sessions)

    assert sessions[400]["is_cli_session"] is False, (
        "state.db source=webui must override the stale CLI JSON flag even beyond the cap"
    )
    assert sessions[400].get("session_source") == "webui", (
        "session_source must be reclassified to webui from state.db"
    )


# --- transcript reconciliation is part of the capped tier -------------------------
#
# The COUNT cap used to bound only the SQL aggregate. Every row whose state.db
# count had grown still paid a full sidecar+state.db transcript merge to derive
# ``visible_message_count`` (98 grown rows x 0.19s = 18.8s of a 20s /api/sessions
# build, 307k _cached_message_key calls), so TOP_N=20 and TOP_N=300 measured the
# same wall clock. These tests pin the merge itself to the same top-N window.


def _grown_sessions(n):
    """n rows all reporting fewer messages than state.db (so every one would reconcile)."""
    return [
        {
            "session_id": f"s{i}",
            "message_count": 1,
            "visible_message_count": 1,
            "actual_message_count": 1,
            "last_message_at": 1000.0,
            "updated_at": 1000.0,
        }
        for i in range(n)
    ]


def _grown_metadata(n):
    return {
        f"s{i}": {
            "_state_db_source": "webui",
            "_state_db_message_count": 5,
            "_state_db_last_message_at": 2000.0,
        }
        for i in range(n)
    }


def _capture_reconciled_ids(monkeypatch, n):
    """Patch the expensive per-row merge and record which session ids it was asked for."""
    reconciled: list[str] = []

    def _fake_reconciled(session_id):
        reconciled.append(str(session_id))
        return None

    monkeypatch.setattr(
        models, "_reconciled_visible_count_for_sidebar", _fake_reconciled
    )
    monkeypatch.setattr(
        models,
        "_read_state_db_sidebar_overrides",
        lambda db_path, id_set, count_session_ids=None: _grown_metadata(n),
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return reconciled


def test_transcript_reconciliation_capped_to_top_n(monkeypatch):
    """With cap N, only the top-N grown rows pay the transcript merge."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "3")
    reconciled = _capture_reconciled_ids(monkeypatch, 10)
    sessions = _grown_sessions(10)

    models._apply_sidebar_state_db_overrides(sessions)

    assert sorted(reconciled) == ["s0", "s1", "s2"], (
        "only the top-N rows may run the expensive transcript reconciliation; "
        f"got {sorted(reconciled)}"
    )
    # Rows inside the window still get the real state.db count.
    assert sessions[0]["message_count"] == 5
    # Rows outside it keep their sidecar count until they enter the window.
    assert [s["message_count"] for s in sessions[3:]] == [1] * 7, (
        "capped rows must keep their sidecar count, not a stale state.db overlay"
    )


def test_transcript_reconciliation_uncapped_when_count_ids_is_none(monkeypatch):
    """count_session_ids=None (cap disabled or list under the cap) reconciles every row."""
    reconciled = _capture_reconciled_ids(monkeypatch, 10)

    models._apply_sidebar_state_db_override_metadata(
        _grown_sessions(10), _grown_metadata(10)
    )

    assert sorted(reconciled) == [f"s{i}" for i in range(10)], (
        "without a count_session_ids bound every grown row reconciles as before"
    )


def test_cap_disabled_reconciles_every_row_through_the_entry_point(monkeypatch):
    """TOP_N=0 disables the cap end to end: count_ids is None, so no row is skipped."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    reconciled = _capture_reconciled_ids(monkeypatch, 10)

    models._apply_sidebar_state_db_overrides(_grown_sessions(10))

    assert sorted(reconciled) == [f"s{i}" for i in range(10)], (
        "cap<=0 must restore the uncapped (pre-fix) reconciliation behaviour"
    )
