"""One-shot runs (`hermes -z`) must not fill the interactive sidebar window (#8265).

The reported shape: agents that delegate through `hermes -z` create many more
one-shot rows than Desktop conversations, so the bounded 20-row window listed
mostly one-shot rows and dropped most Desktop sessions.
"""

import time

import api.models as models
from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _insert_session,
    _make_state_db,
)


def _build_crowded_db(tmp_path):
    db = tmp_path / "state.db"
    # Six older Desktop conversations, then many newer one-shot runs, the way
    # a delegating agent produces them (241 one-shot vs 82 desktop in the report).
    _make_state_db(db, sessions=6, messages_per_session=1, source="desktop", session_source="desktop")
    now = time.time()
    for i in range(models.CLI_VISIBLE_SESSION_LIMIT * 2):
        _insert_session(db, f"oneshot-{i:03d}", source="oneshot", started_at=now + i)
    return db


def test_oneshot_runs_do_not_push_desktop_sessions_out_of_the_window(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    rows = models.get_cli_sessions()
    ids = {row["session_id"] for row in rows}

    assert {f"cli_perf_{i:04d}" for i in range(6)} <= ids
    assert not any(sid.startswith("oneshot-") for sid in ids)


def test_explicit_oneshot_filter_still_lists_oneshot_runs(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    rows = models.get_cli_sessions(source_filter="oneshot")
    ids = {row["session_id"] for row in rows}

    assert ids
    assert all(sid.startswith("oneshot-") for sid in ids)


def test_oneshot_is_a_background_source():
    assert "oneshot" in models.BACKGROUND_CLI_SOURCES


def _configure_named_profiles(monkeypatch, tmp_path, homes):
    _configure_real_single_profile(monkeypatch, tmp_path, homes[0] / "state.db")
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [(home, home / "state.db", name) for home, name in zip(homes, ("default", "work"), strict=True)],
            tuple(
                (str(home), name, models._sqlite_file_stat_cache_key(home / "state.db"))
                for home, name in zip(homes, ("default", "work"), strict=True)
            ),
        ),
    )
    models.clear_cli_sessions_cache()


def test_opened_oneshot_session_can_still_be_found_pinned_and_renamed(tmp_path, monkeypatch):
    # #8349 review: keeping oneshot rows out of the sidebar window must not make
    # a oneshot session the user opened unreachable for pin/rename/workspace.
    from api import routes

    import api.profiles as profiles

    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(models, "_get_profile_home", lambda profile=None: tmp_path)
    sid = f"oneshot-{models.CLI_VISIBLE_SESSION_LIMIT * 2 - 1:03d}"

    meta = routes._lookup_cli_session_metadata(sid)
    assert meta.get("session_id") == sid

    session = routes._get_or_materialize_session(sid)
    session.pinned = True
    session.title = "Renamed oneshot"
    session.save()

    stored = models.get_session(sid)
    assert stored.pinned is True
    assert stored.title == "Renamed oneshot"


def test_oneshot_session_resolves_under_a_named_profile(tmp_path, monkeypatch):
    from api import routes

    homes = [tmp_path / "default-home", tmp_path / "work-home"]
    for home in homes:
        home.mkdir()
    _make_state_db(homes[0] / "state.db", sessions=1, messages_per_session=1)
    _make_state_db(homes[1] / "state.db", sessions=1, messages_per_session=1)
    _insert_session(homes[1] / "state.db", "oneshot-work", source="oneshot")
    _configure_named_profiles(monkeypatch, tmp_path, homes)

    meta = routes._resolve_cli_import_metadata(
        "oneshot-work", requested_profile="work", allow_all_profiles=True,
    )
    assert meta.get("session_id") == "oneshot-work"
    assert meta.get("profile") == "work"
