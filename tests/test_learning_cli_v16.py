import json

import pytest
import typer
from typer.testing import CliRunner

from careerforge.cli.learning_ext import register_cli


@pytest.fixture
def learning_cli():
    app, calls, shown = typer.Typer(), [], []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"path": path}

    register_cli(app, request, shown.append)
    return app, calls, shown


def test_new_learning_and_replay_help(learning_cli):
    app, calls, _ = learning_cli
    for args in [["--help"], ["learning", "--help"], ["replay", "--help"]]:
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
    assert calls == []


def test_profile_and_reviews_are_read_only_and_do_not_schedule(learning_cli):
    app, calls, _ = learning_cli
    assert CliRunner().invoke(app, ["learning", "profile"]).exit_code == 0
    assert CliRunner().invoke(app, ["learning", "reviews"]).exit_code == 0
    assert calls == [
        ("GET", "/api/v1/learning/profile", None),
        ("GET", "/api/v1/learning/review-suggestions", None),
    ]


def test_cli_will_not_invent_funding_scope_or_idempotency(learning_cli, tmp_path):
    app, calls, _ = learning_cli
    file = tmp_path / "plan.json"
    file.write_text(
        json.dumps({"publication_ids": ["p"], "learning_intent": "function", "title": "学习"})
    )
    result = CliRunner().invoke(app, ["learning", "propose", str(file)])
    assert result.exit_code != 0 and calls == []
    file.write_text(
        json.dumps(
            {
                "namespace_id": "my-ns",
                "funding_id": "explicit-funding",
                "publication_ids": ["p"],
                "learning_intent": "function",
                "title": "学习",
                "expected_profile_revision": 1,
                "idempotency_key": "explicit-key",
            }
        )
    )
    result = CliRunner().invoke(app, ["learning", "propose", str(file)])
    assert result.exit_code == 0, result.output
    assert (
        calls[0][2]["funding_id"] == "explicit-funding"
        and calls[0][2]["idempotency_key"] == "explicit-key"
    )


def test_plan_accept_needs_explicit_confirmation_and_revision(learning_cli):
    app, calls, _ = learning_cli
    runner = CliRunner()
    assert runner.invoke(app, ["learning", "accept", "p", "v", "2"]).exit_code != 0
    assert (
        runner.invoke(app, ["learning", "accept", "p", "v", "2", "--confirm", "NO"]).exit_code != 0
    )
    assert calls == []
    good = runner.invoke(app, ["learning", "accept", "p", "v", "2", "--confirm", "ACCEPT"])
    assert good.exit_code == 0, good.output
    assert calls == [
        ("POST", "/api/v1/plans/p/accept", {"version_id": "v", "expected_revision": 2})
    ]


def test_replay_budget_is_explicit_and_not_promoted(learning_cli, tmp_path):
    app, calls, _ = learning_cli
    file = tmp_path / "replay.json"
    file.write_text(
        json.dumps(
            {
                "dataset_id": "frozen-dataset",
                "funding_id": "approved",
                "per_arm_call_cap": 3,
                "idempotency_key": "request",
            }
        )
    )
    result = CliRunner().invoke(app, ["replay", "run", "candidate", str(file)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][1].endswith("/candidate/replays")
    assert calls[0][2]["per_arm_call_cap"] == 3


def test_malformed_private_file_diagnostics_do_not_echo_body(learning_cli, tmp_path):
    app, calls, _ = learning_cli
    file = tmp_path / "sensitive.json"
    file.write_text('{"private": "secret-body-MUST-NOT-PRINT", "invalid":true}')
    result = CliRunner().invoke(app, ["learning", "observe", "attempt", str(file)])
    assert (
        result.exit_code != 0 and "secret-body-MUST-NOT-PRINT" not in result.output and calls == []
    )
