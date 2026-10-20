"""Generation-fenced approval relay: the canonical Runs API prompt shape.

A gateway with a session authority publishes an approval as
``{prompt_id|request_id, execution_generation, ...}`` and accepts exactly
``{request_id, execution_generation, choice}`` on
``POST /v1/runs/{id}/approval``. The WebUI used to read only ``approval_id``/
``id``, substitute a random uuid, and answer with ``{choice, approval_id}``,
so every click was rejected with ``400 invalid_params``.

These tests pin the wire shapes on both sides of the relay: the canonical
(generation) shape and the legacy (approval_id) shape must both survive.
"""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import api.route_approvals as approvals
from api import routes
from api.gateway_chat import (
    _gateway_runs_approval_event,
    _relay_gateway_run_approval,
)
from api.runner_client import HttpRunnerClient

SID = "sess-generation-approval"
RUN_ID = "run-generation-approval"
PROMPT_ID = "prompt-generation-approval"


def _canonical_payload(**overrides):
    """The exact shape a generation-fenced gateway emits (#51871 contract)."""
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "prompt_id": PROMPT_ID,
        "execution_generation": 7,
        "tool": "shell",
        "command": "rm -rf /tmp/x",
        "description": "delete temp dir",
        "choices": ["once", "deny"],
    }
    payload.update(overrides)
    return payload


def _relay(payload, *, put_event=None):
    with patch(
        "api.config.gateway_supports_approval_identity_v1", return_value=False
    ):
        return _relay_gateway_run_approval(
            SID,
            RUN_ID,
            payload,
            "http://gw:8642",
            "",
            put_gateway_event=put_event or (lambda _name, _payload: None),
        )


def _answer(body, captured, *, run_projection=None, run_status=None, respond_error=None):
    def fake_request_json(_self, req):
        if req.method == "GET":
            captured.setdefault("gets", []).append(req.full_url)
            if run_status:
                from api.runner_client import RunnerClientError

                raise RunnerClientError(f"Runner returned HTTP {run_status}: {{}}")
            return run_projection or {}
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        if respond_error:
            from api.runner_client import RunnerClientError

            raise RunnerClientError(respond_error)
        return {"ok": True}

    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("api.runner_client.HttpRunnerClient._request_json", new=fake_request_json):
        routes._handle_approval_respond(handler, body)
    return handler


def setup_function():
    with approvals._lock:
        approvals._pending.pop(SID, None)
        approvals._gateway_queues.pop(SID, None)


def teardown_function():
    with approvals._lock:
        approvals._pending.pop(SID, None)
        approvals._gateway_queues.pop(SID, None)


# ---------------------------------------------------------------------------
# Mapper: keep the prompt identity and its generation
# ---------------------------------------------------------------------------

def test_mapper_keeps_prompt_id_and_execution_generation():
    event = _gateway_runs_approval_event(_canonical_payload())

    assert event is not None
    assert event["approval_id"] == PROMPT_ID
    assert event["_gateway_raw_approval_id_present"] is True
    assert event["_gateway_execution_generation"] == 7


def test_mapper_accepts_request_id_alias_used_by_the_session_bridge():
    event = _gateway_runs_approval_event(
        _canonical_payload(prompt_id=None, request_id="req-bridge-1")
    )

    assert event is not None
    assert event["approval_id"] == "req-bridge-1"
    assert event["_gateway_execution_generation"] == 7


def test_mapper_legacy_payload_is_unchanged():
    event = _gateway_runs_approval_event({
        "approval_id": "appr-legacy",
        "command": "echo hi",
        "description": "legacy approval",
    })

    assert event is not None
    assert event["approval_id"] == "appr-legacy"
    assert event["_gateway_raw_approval_id_present"] is True
    assert event["_gateway_execution_generation"] is None


def test_mapper_ignores_non_integer_generation():
    event = _gateway_runs_approval_event(
        _canonical_payload(execution_generation="7")
    )

    assert event is not None
    assert event["_gateway_execution_generation"] is None


# ---------------------------------------------------------------------------
# Client: pick the body shape from what the gateway published
# ---------------------------------------------------------------------------

def test_respond_approval_sends_exactly_the_generation_fenced_body():
    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval(RUN_ID, PROMPT_ID, "once", execution_generation=7)

    assert captured["url"] == f"/v1/runs/{RUN_ID}/approval"
    # Exactly the three keys the gateway validates against; an extra key such
    # as ``approval_id`` is a hard 400 on the canonical endpoint.
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }


def test_respond_approval_without_generation_keeps_the_legacy_body():
    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval(RUN_ID, "appr-legacy", "deny")

    assert captured["body"] == {"choice": "deny", "approval_id": "appr-legacy"}


# ---------------------------------------------------------------------------
# End to end: event payload -> card mirror -> HTTP answer
# ---------------------------------------------------------------------------

def test_canonical_event_is_answered_with_generation_fenced_body():
    """The reported defect: a real gateway payload must not produce a 400."""
    cards = []
    assert _relay(_canonical_payload(), put_event=lambda n, p: cards.append((n, p)))

    mirror = approvals.gateway_pending_mirror(
        SID, approval_id=PROMPT_ID, run_id=RUN_ID
    )
    assert mirror is not None, "the canonical approval must surface as one card"
    assert mirror["approval_id"] == PROMPT_ID

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert captured["url"].endswith(f"/v1/runs/{RUN_ID}/approval")
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }
    assert handler.send_response.call_args[0][0] == 200


def test_legacy_event_is_still_answered_with_the_legacy_body():
    """Backwards compatibility: a gateway that publishes no generation."""
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "approval_id": "appr-legacy-1",
        "command": "echo hi",
        "description": "legacy approval",
        "choices": ["once", "deny"],
    }
    assert _relay(payload)
    mirror = approvals.gateway_pending_mirror(
        SID, approval_id="appr-legacy-1", run_id=RUN_ID
    )
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": "appr-legacy-1",
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert captured["body"] == {"choice": "once", "approval_id": ""}
    assert handler.send_response.call_args[0][0] == 200


def test_yolo_auto_approve_also_carries_the_generation():
    """The sibling path: session YOLO answers without a card, same contract."""
    captured = {}

    def fake_request_json(_self, req):
        captured["body"] = json.loads(req.data)
        return {"ok": True}

    with patch(
        "api.config.gateway_supports_approval_identity_v1", return_value=False
    ), patch(
        "api.gateway_chat._gateway_session_yolo_enabled", return_value=True
    ), patch(
        "api.runner_client.HttpRunnerClient._request_json", new=fake_request_json
    ):
        assert _relay_gateway_run_approval(
            SID,
            RUN_ID,
            _canonical_payload(),
            "http://gw:8642",
            "",
            put_gateway_event=lambda _n, _p: None,
        )

    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }


# ---------------------------------------------------------------------------
# Cards the UI rebuilt itself: the run projection is the authority
# ---------------------------------------------------------------------------

def test_synthetic_gwrun_card_is_answered_from_the_run_projection():
    """A mirror rebuilt from the live queue carries no generation of its own.

    Answering it from the mirror alone sent the legacy body and 400'd on every
    click; the run projection names the live prompt and its generation.
    """
    payload = {
        "event": "approval.request",
        "run_id": RUN_ID,
        "approval_id": "gwrun:run-local-1",
        "command": "echo hi",
        "description": "rebuilt card",
    }
    assert _relay(payload)
    mirror = approvals.gateway_pending_mirror(
        SID, approval_id="gwrun:run-local-1", run_id=RUN_ID
    )
    assert mirror is not None
    assert mirror[approvals._GATEWAY_EXECUTION_GENERATION] is None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": "gwrun:run-local-1",
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={
            "status": "waiting_for_approval",
            "pending_controls": [
                {
                    "kind": "approval",
                    "prompt_id": PROMPT_ID,
                    "execution_generation": 7,
                    "choices": ["once", "deny"],
                }
            ],
        },
    )

    assert captured["gets"] and captured["gets"][0].endswith(f"/v1/runs/{RUN_ID}")
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "once",
    }
    assert handler.send_response.call_args[0][0] == 200


def test_choice_the_gateway_does_not_offer_is_refused_before_it_is_sent():
    """A stale card's "session" button must not become a gateway 400."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "session",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert "body" not in captured, "an unofferable choice must not reach the gateway"
    assert handler.send_response.call_args[0][0] == 400
    assert b"approval_choice_not_offered" in handler.wfile.getvalue()


def test_canonical_card_answers_without_a_projection_refresh():
    """The fast path stays a single POST: no extra GET on a well-formed card."""
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "deny",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
    )

    assert "gets" not in captured
    assert captured["body"] == {
        "request_id": PROMPT_ID,
        "execution_generation": 7,
        "choice": "deny",
    }
    assert handler.send_response.call_args[0][0] == 200


def test_replay_of_an_already_answered_prompt_is_idempotent():
    """A double click must not paint 502 on an approval that already worked.

    The first answer resumes the run and consumes the prompt; the second POST
    is rejected with 409 ``approval_not_pending`` and the run has no control
    left to re-project, so the only correct reading is "already done".
    """
    assert _relay(_canonical_payload())
    mirror = approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID)
    assert mirror is not None

    captured = {}
    handler = _answer(
        {
            "session_id": SID,
            "choice": "once",
            "approval_id": PROMPT_ID,
            "run_id": RUN_ID,
            "mirror_token": mirror[approvals._GATEWAY_MIRROR_TOKEN],
        },
        captured,
        run_projection={"status": "running", "pending_controls": []},
        respond_error=(
            'Runner returned HTTP 409: {"error": {"message": '
            '"approval_not_pending", "code": "approval_not_pending"}}'
        ),
    )

    assert handler.send_response.call_args[0][0] == 200
    assert b'"already_resolved": true' in handler.wfile.getvalue()
    assert approvals.gateway_pending_mirror(SID, approval_id=PROMPT_ID, run_id=RUN_ID) is None
