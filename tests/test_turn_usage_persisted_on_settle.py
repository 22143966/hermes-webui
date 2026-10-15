"""Regression test: the settled assistant message must carry ``_turnUsage``.

The token/cache footer reads ``msg._turnUsage`` in ``static/ui.js``
(``hasTurnUsage = !!msg._turnUsage``). The live tab gets that value from the
SSE ``usage`` payload in the ``done`` handler (``static/messages.js``), but the
value is derived client-side and never returned by ``GET /api/session`` — so a
reload or a second tab loses the badge for every historical turn.

``_turnUsage`` is already listed in ``_SESSION_MESSAGE_DISPLAY_METADATA_KEYS``
(``api/models.py``), i.e. the merge/reload path is ready to carry it. The gap
was purely on the producer side: the settle block stamped ``_turnDuration``,
``_turnTps``, ``_firstTokenMs``, ``_usedModel`` and ``_gatewayRouting`` onto the
final assistant message but never ``_turnUsage``.

This test binds to that producer block so a regression (dropping the write) is
caught without spinning up a full agent turn.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_settle_block_writes_turn_usage_onto_assistant_message():
    src = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    # The write must live next to the other settled-turn display metadata.
    anchor = "_dm['_usedModel'] = _used_model"
    assert anchor in src, "settle-block anchor moved; update this regression test"

    tail = src[src.index(anchor):]
    settle_region = tail[:tail.index("break")]

    assert "_dm['_turnUsage']" in settle_region, (
        "the settle block must stamp _turnUsage alongside _usedModel so the "
        "usage badge survives a session reload"
    )


def test_turn_usage_write_uses_per_turn_deltas_not_session_totals():
    src = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    # The client computes per-turn deltas (messages.js `_doneUsageFallback`).
    # The server-side write must mirror those counter deltas, not the
    # cumulative session totals, or every reloaded turn shows a climbing total.
    for var in (
        "turn_input_tokens",
        "turn_output_tokens",
        "turn_cache_read_tokens",
        "turn_cache_write_tokens",
        "turn_estimated_cost",
    ):
        assert f"{var} = " in src, f"{var} must be computed before the write"

    anchor = "_dm['_turnUsage'] = {"
    region = src[src.index(anchor):src.index(anchor) + 600]
    assert "'input_tokens': turn_input_tokens," in region
    assert "'output_tokens': turn_output_tokens," in region
    assert "'cache_read_tokens': turn_cache_read_tokens," in region
    assert "'cache_write_tokens': turn_cache_write_tokens," in region
    assert "'estimated_cost': turn_estimated_cost," in region
    assert "'cache_hit_percent': turn_cache_hit_percent," in region


def test_turn_usage_is_only_stamped_when_the_turn_consumed_tokens():
    src = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    anchor = "_dm['_turnUsage'] = {"
    region = src[:src.index(anchor)]

    # Guard mirrors the client's `if(curIn>prevIn || ...)` check: a no-op turn
    # must not overwrite a previous turn's snapshot with all-zero counters.
    guard = region.rindex("if (turn_input_tokens or turn_output_tokens")
    assert guard > 0
    assert "turn_cache_read_tokens or turn_cache_write_tokens" in region[guard:]


def test_turn_usage_stays_in_the_persisted_display_metadata_allowlist():
    """The key must keep surviving the sidecar/state.db merge on reload."""
    src = (ROOT / "api" / "models.py").read_text(encoding="utf-8")

    start = src.index("_SESSION_MESSAGE_DISPLAY_METADATA_KEYS = (")
    block = src[start:src.index(")", start)]
    assert '"_turnUsage"' in block
