"""Recovered (cut-off) fan-outs wake the parent with a re-dispatch prompt."""
from api import gateway_delegation_wakeup as w

RECOVERED = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_x]\nLast persisted unit status: running (before owner "
             "exit; not current liveness). Unrecorded outcomes remain unknown; inspect evidence.")


def test_finished_unit_keeps_plain_wake_prompt():
    assert w.wake_prompt([{"content": "[ASYNC DELEGATION BATCH COMPLETE]\nstatus=completed"}]) == w.WAKE_PROMPT


def test_recovered_unit_gets_redispatch_prompt():
    assert w.wake_prompt([{"content": "done"}, {"content": RECOVERED}]) == w.REDISPATCH_PROMPT


def test_redispatch_prompt_scopes_to_unfinished_tasks():
    assert "ONLY the unfinished tasks" in w.REDISPATCH_PROMPT and "Keep the results" in w.REDISPATCH_PROMPT


def test_poller_passes_row_based_prompt(monkeypatch):
    seen = {}

    def fake_start(sid, prompt, **kw):
        seen["prompt"] = prompt
        return {"stream_id": "s"}

    import api.routes as routes
    monkeypatch.setattr(routes, "start_session_turn", fake_start)
    assert w._start_wakeup("sid", None, w.wake_prompt([{"content": RECOVERED}]))
    assert seen["prompt"] == w.REDISPATCH_PROMPT
