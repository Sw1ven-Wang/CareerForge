import json

import pytest
import typer
from typer.testing import CliRunner

from careerforge.cli.control_ext import register_cli


def configured(result=None):
    app = typer.Typer()
    calls, displays, saved = [], [], []
    state = {
        "token": "test-token",
        "principal_id": "owner",
        "namespace_id": "old",
        "funding_id": "old-budget",
        "conversation_id": "old-chat",
        "last_turn_id": "old-turn",
        "last_event_id": 8,
        "last_turn_status": "completed",
    }

    def request(*args):
        calls.append(args)
        return result or {"ok": True}

    register_cli(app, request, displays.append, lambda: dict(state), saved.append)
    return app, calls, displays, saved


@pytest.mark.parametrize(
    ("command", "route"),
    [
        (["taxonomy", "propose"], "/api/v1/taxonomy/proposals"),
        (["taxonomy", "decide", "proposal"], "/api/v1/taxonomy/proposals/proposal/decisions"),
        (["maintenance", "plan"], "/api/v1/maintenance/plans"),
        (["maintenance", "execute", "plan"], "/api/v1/maintenance/plans/plan/execute"),
        (["maintenance", "schedule"], "/api/v1/maintenance/schedules"),
        (["shared", "create"], "/api/v1/shared-builds"),
        (["shared", "subscribe", "run"], "/api/v1/shared-builds/run/subscriptions"),
        (["shared", "unsubscribe", "run"], "/api/v1/shared-builds/run/unsubscribe"),
        (["index", "create"], "/api/v1/retrieval/index-jobs"),
    ],
)
def test_control_commands_forward_exact_user_contract(tmp_path, command, route):
    body = tmp_path / "request.json"
    explicit = {"idempotency_key": "user-key", "confirm": "USER_SUPPLIED", "call_cap": 4}
    body.write_text(json.dumps(explicit))
    app, calls, _, _ = configured()
    outcome = CliRunner().invoke(app, [*command, str(body)])
    assert outcome.exit_code == 0, outcome.output
    assert calls == [("POST", route, explicit)]


def test_privacy_export_is_private_and_never_overwrites(tmp_path):
    body = {"rows": [{"private": "body"}]}
    app, calls, shown, _ = configured(body)
    output = tmp_path / "private.json"
    runner = CliRunner()
    assert runner.invoke(app, ["privacy", "export", str(output)]).exit_code == 0
    assert json.loads(output.read_text()) == body and output.stat().st_mode & 0o777 == 0o600
    assert "body" not in json.dumps(shown)
    assert runner.invoke(app, ["privacy", "export", str(output)]).exit_code != 0
    assert json.loads(output.read_text()) == body


def test_confirm_does_not_invent_consent_and_only_completed_erasure_resets_local_pointers(tmp_path):
    body = tmp_path / "confirmation.json"
    payload = {
        "confirmation_nonce": "user-reviewed-nonce",
        "plan_hash": "f" * 64,
        "acknowledge_irreversible": True,
    }
    body.write_text(json.dumps(payload))
    for status in ["erasing", "completed"]:
        app, calls, _, saved = configured(
            {"state": status, "result": {"new_namespace_id": "fresh"}}
        )
        outcome = CliRunner().invoke(app, ["privacy", "confirm", "proposal", str(body)])
        assert outcome.exit_code == 0, outcome.output
        assert calls == [("POST", "/api/v1/privacy/deletion-proposals/proposal/confirm", payload)]
        if status == "completed":
            assert saved == [
                {"token": "test-token", "principal_id": "owner", "namespace_id": "fresh"}
            ]
        else:
            assert saved == []


def test_invalid_request_never_reaches_api(tmp_path):
    body = tmp_path / "bad.json"
    body.write_text('["not an object"]')
    app, calls, _, _ = configured()
    assert CliRunner().invoke(app, ["shared", "create", str(body)]).exit_code != 0
    assert calls == []
