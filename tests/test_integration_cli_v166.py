"""Actual Typer rendering and real API integration; all source fixtures are synthetic.

No hidden/evaluator data or real provider calls are used. Terminal output is not
evidence that a human read a screen, and a candidate assessment is not mastery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from io import StringIO

import pytest
import typer
from fastapi.testclient import TestClient
from rich.console import Console
from sqlalchemy import select
from typer.testing import CliRunner

from careerforge.api import create_app
from careerforge.cli import main
from careerforge.cli.integration_v166 import register_cli
from careerforge.contracts import ImportSourceInput, TurnInput
from careerforge.models import AnswerBlock, Conversation
from careerforge.services.dependencies import invalidate


def navigation_response():
    text = "    def run(self):\r\n        return 42\n"
    return {
        "answer_block_id": "block-1",
        "citation_index": 0,
        "source_id": "source-1",
        "commit": "b" * 40,
        "path": "src/agent.py",
        "start_line": 2,
        "end_line": 3,
        "server_location_valid": True,
        "client_highlight_ack": False,
        "display": {
            "text": text,
            "start_line": 2,
            "end_line": 3,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "file_sha256": "a" * 64,
            "content_complete": True,
        },
    }


@pytest.fixture
def cli():
    app, calls, shown = typer.Typer(), [], []
    output = StringIO()
    console = Console(file=output, color_system=None, width=180)
    response = navigation_response()

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST" and path == "/api/v1/learning/navigation-acks":
            # The sink already contains the full range when the POST begins.
            assert "> 2 |     def run(self):" in output.getvalue()
            assert "> 3 |         return 42" in output.getvalue()
        return response

    register_cli(app, request, shown.append, lambda: console, main.clean)
    return app, calls, shown, output, response


def write_json(tmp_path, value):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_real_main_registers_new_commands_without_changing_existing_paths():
    runner = CliRunner()
    for args in (
        ["--help"],
        ["jd", "--help"],
        ["assessment", "request", "--help"],
        ["source-show", "--help"],
        ["source-targets", "--help"],
        ["learning", "session-propose", "--help"],
        ["learning", "accept", "--help"],
        ["build", "--help"],
        ["chat", "--help"],
    ):
        result = runner.invoke(main.app, args)
        assert result.exit_code == 0, result.output


def test_literal_jd_keeps_bytes_and_null_unspecified_metadata(cli, tmp_path):
    app, calls, _, _, _ = cli
    path = tmp_path / "Do not infer my role or source.txt"
    raw = "\ufeff岗位\r\n\r\n  熟悉 Python 或 Go；加分项：K8s。\r\n".encode()
    path.write_bytes(raw)
    result = CliRunner().invoke(
        app, ["jd", "import", str(path), "--namespace", "private", "--jd-id", "chosen-id"]
    )
    assert result.exit_code == 0, result.output
    method, endpoint, body = calls[0]
    assert (method, endpoint) == ("POST", "/api/v1/jds")
    assert body["raw_text"].encode() == raw
    assert body["raw_text_bytes"] == len(raw)
    assert body["raw_text_sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["namespace_id"] == "private" and body["jd_id"] == "chosen-id"
    assert all(body[key] is None for key in ("source", "source_version", "job_title", "paragraphs"))


def test_jd_source_metadata_is_explicit_and_url_is_not_fetched(cli, tmp_path):
    app, calls, _, _, _ = cli
    path = tmp_path / "jd.txt"
    path.write_text("Python")
    result = CliRunner().invoke(
        app,
        [
            "jd",
            "import",
            str(path),
            "--namespace",
            "n",
            "--jd-id",
            "j",
            "--source-url",
            "https://user-chosen.example/job?id=7",
            "--job-title",
            "工程师",
            "--source-version",
            "2026-11-05",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][1] == "/api/v1/jds"
    assert calls[0][2]["source"] == {
        "kind": None,
        "source_label": None,
        "url": "https://user-chosen.example/job?id=7",
    }
    assert calls[0][2]["job_title"] == "工程师"


@pytest.mark.parametrize("body", [b"", b"\xff", b"x" * 50_001, b"x" * 200_001])
def test_invalid_jd_is_rejected_before_any_request(cli, tmp_path, body):
    app, calls, _, _, _ = cli
    path = tmp_path / "jd.txt"
    path.write_bytes(body)
    result = CliRunner().invoke(
        app, ["jd", "import", str(path), "--namespace", "n", "--jd-id", "j"]
    )
    assert result.exit_code != 0 and calls == []
    assert "xxxxx" not in result.output


def test_missing_jd_scope_or_file_never_sends(cli, tmp_path):
    app, calls, _, _, _ = cli
    path = tmp_path / "missing.txt"
    assert CliRunner().invoke(app, ["jd", "import", str(path)]).exit_code != 0
    assert (
        CliRunner()
        .invoke(app, ["jd", "import", str(path), "--namespace", "n", "--jd-id", "j"])
        .exit_code
        != 0
    )
    assert calls == []


@pytest.mark.parametrize(
    "args,endpoint,body",
    [
        (
            ["jd", "extract"],
            "/api/v1/jds/extractions",
            {
                "namespace_id": "private",
                "funding_id": "authorized",
                "jd_revision_id": "revision-1",
                "idempotency_key": "jd-call",
            },
        ),
        (
            ["assessment", "request"],
            "/api/v1/learning/assessments",
            {
                "namespace_id": "private",
                "funding_id": "authorized",
                "attempt_id": "attempt-1",
                "idempotency_key": "assessment-call",
            },
        ),
    ],
)
def test_budgeted_requests_preserve_exact_contract_without_auto_followup(
    cli, tmp_path, args, endpoint, body
):
    app, calls, _, _, _ = cli
    result = CliRunner().invoke(app, [*args, write_json(tmp_path, body)])
    assert result.exit_code == 0, result.output
    assert calls == [("POST", endpoint, body)]


@pytest.mark.parametrize("args", [["jd", "extract"], ["assessment", "request"]])
def test_budget_or_private_contract_failure_never_sends_or_echoes(cli, tmp_path, args):
    app, calls, _, _, _ = cli
    result = CliRunner().invoke(
        app, [*args, write_json(tmp_path, {"private": "SECRET-MUST-NOT-ECHO"})]
    )
    assert result.exit_code != 0 and calls == []
    assert "SECRET-MUST-NOT-ECHO" not in result.output


def test_jd_revision_read_is_get_only(cli):
    app, calls, _, _, _ = cli
    result = CliRunner().invoke(app, ["jd", "show", "revision-1"])
    assert result.exit_code == 0, result.output
    assert calls == [("GET", "/api/v1/jds/revision-1", None)]


def test_source_show_is_read_only_unless_ack_key_is_explicit(cli):
    app, calls, _, output, _ = cli
    result = CliRunner().invoke(app, ["source-show", "block-1"])
    assert result.exit_code == 0, result.output
    assert calls == [("GET", "/api/v1/learning/navigation/block-1?citation_index=0", None)]
    assert "src/agent.py:2-3" in output.getvalue()
    assert "> 2 |     def run(self):" in output.getvalue()
    assert "未发送ACK" in output.getvalue()


def test_ack_matches_displayed_target_and_follows_completed_output(cli):
    app, calls, _, _, _ = cli
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "chosen-receipt"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == (
        "POST",
        "/api/v1/learning/navigation-acks",
        {
            "answer_block_id": "block-1",
            "citation_index": 0,
            "source_id": "source-1",
            "commit": "b" * 40,
            "path": "src/agent.py",
            "start_line": 2,
            "end_line": 3,
            "client_event": "source_highlight_ack",
            "idempotency_key": "chosen-receipt",
        },
    )


def test_nonzero_citation_index_is_used_in_get_display_and_ack(cli):
    app, calls, _, _, response = cli
    response["citation_index"] = 2
    result = CliRunner().invoke(
        app, ["source-show", "block-1", "--citation-index", "2", "--ack-key", "k"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].endswith("?citation_index=2") and calls[-1][2]["citation_index"] == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"server_location_valid": False},
        {"server_location_valid": 1},
        {"answer_block_id": "other-block"},
        {"citation_index": 1},
        {"citation_index": False},
        {"commit": "not-a-commit"},
        {"start_line": "2"},
        {"display": None},
        {"display": {"content_complete": False}},
        {"display": {"text_sha256": "f" * 64}},
        {"display": {"file_sha256": "invalid"}},
        {"display": {"end_line": 4}},
        {"display": {"start_line": True}},
    ],
)
def test_unverified_incomplete_or_mismatched_window_never_displays_or_acks(cli, patch):
    app, calls, _, output, response = cli
    if isinstance(patch.get("display"), dict):
        response["display"].update(patch["display"])
    else:
        response.update(patch)
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "k"])
    assert result.exit_code != 0 and len(calls) == 1 and output.getvalue() == ""


def test_hashed_but_wrong_line_count_never_acks(cli):
    app, calls, _, output, response = cli
    response["display"].update(text="one line", text_sha256=hashlib.sha256(b"one line").hexdigest())
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "k"])
    assert result.exit_code != 0 and len(calls) == 1 and output.getvalue() == ""


@pytest.mark.parametrize(
    "args",
    [
        ["source-show", "../other", "--ack-key", "k"],
        ["source-show", "http://example.com", "--ack-key", "k"],
        ["source-show", "block?citation_index=99", "--ack-key", "k"],
        ["source-show", "block-1", "--ack-key", ""],
        ["source-show", "block-1", "--citation-index", "-1"],
        ["source-show", "block-1", "--citation-index", "101"],
        ["jd", "show", "../../etc/passwd"],
    ],
)
def test_user_input_cannot_interpolate_urls_or_fabricate_ack(cli, args):
    app, calls, _, _, _ = cli
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0 and calls == []


def test_source_control_bytes_and_markup_are_not_terminal_commands():
    app, output, calls = typer.Typer(), StringIO(), []
    response = navigation_response()
    raw = "[bold]literal[/bold]\x1b]52;c;clipboard\x07\u202e\nsecond\x1b[31m\n"
    response["display"].update(text=raw, text_sha256=hashlib.sha256(raw.encode()).hexdigest())
    response["path"] = "src/[red]agent.py\x1b]8;;https://evil.example\x07"

    def request(method, path, body=None):
        calls.append((method, path, body))
        return response

    register_cli(
        app, request, lambda _: None, lambda: Console(file=output, color_system=None), main.clean
    )
    result = CliRunner().invoke(app, ["source-show", "block-1"])
    assert result.exit_code == 0, result.output
    assert "[bold]literal[/bold]" in output.getvalue() and "[red]agent.py" in output.getvalue()
    assert not any(
        value in output.getvalue() for value in ("\x1b", "\u202e", "clipboard", "evil.example")
    )
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_output_or_flush_failure_cannot_send_ack(failure):
    class FailingOutput(StringIO):
        def write(self, value):
            if failure == "write":
                raise OSError("sink unavailable")
            return super().write(value)

        def flush(self):
            if failure == "flush":
                raise OSError("sink unavailable")
            return super().flush()

    app, calls = typer.Typer(), []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return navigation_response()

    register_cli(app, request, lambda _: None, lambda: Console(file=FailingOutput()), main.clean)
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "k"])
    assert result.exit_code != 0 and len(calls) == 1


@pytest.fixture
def real_cli(env, monkeypatch):
    output = StringIO()
    monkeypatch.setattr(main, "console", Console(file=output, color_system=None, width=180))
    requests = []
    server = TestClient(
        create_app(env["app"]), headers={"Authorization": "Bearer " + env["owner"]["token"]}
    )

    @contextmanager
    def client():
        class CapturingClient:
            def request(self, method, path, json=None):
                requests.append((method, path, deepcopy(json), output.getvalue()))
                return server.request(method, path, json=json)

        yield CapturingClient()

    monkeypatch.setattr(main, "client", client)
    yield CliRunner(), output, requests
    server.close()


def _real_navigation(env):
    service, actor = env["app"], env["actor"]
    source = service.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/cli-navigation",
            commit_sha="b" * 40,
            license_id="MIT",
            source_metadata={"synthetic": True},
            files={"src/agent.py": "class Target:\n    def run(self):\n        return 42\n"},
        ),
    )
    conversation = service.conversations.create(actor)
    with service.db.transaction() as db:
        db.get(Conversation, conversation).focus = {"project_source_ids": [source]}
    turn = service.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="Locate symbol Target.run",
            expected_revision=1,
            idempotency_key="cli-navigation",
        ),
    )
    asyncio.run(service.conversations.process(turn["turn_id"]))
    with service.db.read() as db:
        block = db.scalar(
            select(AnswerBlock).where(
                AnswerBlock.turn_id == turn["turn_id"], AnswerBlock.kind == "raw_source_observation"
            )
        )
        return source, block.id


def test_real_cli_to_api_navigation_is_display_then_ack_then_readback(env, real_cli):
    runner, output, requests = real_cli
    _, block = _real_navigation(env)
    before = env["app"].learning.navigation_status(env["actor"], block)
    assert not before["client_highlight_ack"]
    result = runner.invoke(main.app, ["source-show", block, "--ack-key", "cli-real-ack"])
    assert result.exit_code == 0, result.output
    after = env["app"].learning.navigation_status(env["actor"], block)
    assert after["client_highlight_ack"] and len(after["ack_ids"]) == 1
    assert after["ack_source"] == "client_reported_not_server_observed"
    assert [row[0] for row in requests] == ["GET", "POST"]
    assert "def run(self):" in requests[1][3] and "return 42" in requests[1][3]
    assert '"client_highlight_ack": true' in output.getvalue()
    assert env["model"].calls == []
    # Idempotent replay still renders before reporting the same receipt.
    replay = runner.invoke(main.app, ["source-show", block, "--ack-key", "cli-real-ack"])
    assert replay.exit_code == 0
    assert env["app"].learning.navigation_status(env["actor"], block)["ack_ids"] == after["ack_ids"]


def test_real_revoked_source_cannot_be_displayed_or_acked(env, real_cli):
    runner, output, requests = real_cli
    source, block = _real_navigation(env)
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], source, "revoked", "cli-test")
    result = runner.invoke(main.app, ["source-show", block, "--ack-key", "revoked-ack"])
    assert result.exit_code != 0 and len(requests) == 1
    assert "return 42" not in output.getvalue()


def test_real_literal_jd_import_then_budgeted_extract_only_admits_one_run(env, real_cli, tmp_path):
    runner, output, requests = real_cli
    path = tmp_path / "jd.txt"
    raw = "熟悉Python或Go；\r\n  Kubernetes是加分项。\r\n"
    path.write_bytes(raw.encode())
    result = runner.invoke(
        main.app, ["jd", "import", str(path), "--namespace", env["ns"], "--jd-id", "literal"]
    )
    assert result.exit_code == 0, result.output
    imported = json.loads(output.getvalue())
    reread = env["app"].jd.read(env["actor"], imported["jd_revision_id"])
    assert reread["raw_text"] == raw and reread["source"] is None and reread["job_title"] is None
    body = {
        "namespace_id": env["ns"],
        "funding_id": env["funding"],
        "jd_revision_id": imported["jd_revision_id"],
        "idempotency_key": "cli-extract",
    }
    result = runner.invoke(main.app, ["jd", "extract", write_json(tmp_path, body)])
    assert result.exit_code == 0, result.output
    assert requests[-1][0:3] == ("POST", "/api/v1/jds/extractions", body)
    assert len(requests) == 2 and env["model"].calls == []


def test_existing_session_propose_returns_gap_without_auto_acceptance(env, real_cli, tmp_path):
    runner, output, requests = real_cli
    body = {
        "namespace_id": env["ns"],
        "learning_intent": "function",
        "title": "A bounded session",
        "total_minutes": 30,
        "expected_profile_revision": 1,
        "idempotency_key": "cli-gap",
    }
    result = runner.invoke(main.app, ["learning", "session-propose", write_json(tmp_path, body)])
    assert result.exit_code == 0, result.output
    assert requests[0][1] == "/api/v1/learning/session-plan-proposals" and len(requests) == 1
    gap = json.loads(output.getvalue())
    assert gap["status"] == "unbuildable" and not gap["can_accept"] and not gap["accepted"]
    assert env["model"].calls == []
    result = runner.invoke(main.app, ["learning", "accept", gap["draft_id"], "no-version", "1"])
    assert result.exit_code != 0 and len(requests) == 1


def test_target_list_exposes_exact_block_and_citation_without_ack(cli):
    app, calls, shown, _, response = cli
    citation = {
        key: response[key] for key in ("source_id", "commit", "path", "start_line", "end_line")
    }
    response.clear()
    response.update(
        status="completed",
        events=[
            {
                "type": "answer.block.validated",
                "payload": {"block_id": "first", "content": {"citations": [{}, citation]}},
            },
            {
                "type": "answer.block.validated",
                "payload": {"block_id": "revoked", "content": {"citations": [citation]}},
            },
            {"type": "answer.evidence_invalidated", "payload": {"block_id": "revoked"}},
        ],
    )
    result = CliRunner().invoke(app, ["source-targets", "turn-1"])
    assert result.exit_code == 0, result.output
    assert calls == [("GET", "/api/v1/turns/turn-1", None)]
    assert shown == [
        {
            "turn_id": "turn-1",
            "status": "completed",
            "targets": [{"answer_block_id": "first", "citation_index": 1, **citation}],
            "ack_sent": False,
        }
    ]


def test_real_target_discovery_provides_the_viewable_block(env, real_cli):
    runner, output, requests = real_cli
    _, block = _real_navigation(env)
    with env["app"].db.read() as db:
        turn_id = db.get(AnswerBlock, block).turn_id
    result = runner.invoke(main.app, ["source-targets", turn_id])
    assert result.exit_code == 0, result.output
    targets = json.loads(output.getvalue())
    assert targets["targets"][0]["answer_block_id"] == block
    assert not targets["ack_sent"] and len(requests) == 1
    assert not env["app"].learning.navigation_status(env["actor"], block)["client_highlight_ack"]


def test_existing_session_propose_then_exact_acceptance_activates_only_that_draft(
    env, real_cli, tmp_path
):
    from conftest import publish_fixture
    from test_session_plans_v166 import planner

    runner, output, requests = real_cli
    service, actor = env["app"], env["actor"]
    _, publication, _ = asyncio.run(publish_fixture(env, "cli-session"))
    planner(env, [10])
    before = service.learning.profile(actor)
    body = {
        "namespace_id": env["ns"],
        "funding_id": env["funding"],
        "publication_ids": [publication],
        "learning_intent": "function",
        "title": "阅读已审材料",
        "total_minutes": 12,
        "expected_profile_revision": before["revision"],
        "idempotency_key": "cli-session-propose",
    }
    result = runner.invoke(main.app, ["learning", "session-propose", write_json(tmp_path, body)])
    assert result.exit_code == 0, result.output
    run_id = json.loads(output.getvalue())["run_id"]
    asyncio.run(service.execute(run_id))
    proposal = service.runtime.stage(run_id, "result")
    assert not proposal["accepted"] and service.learning.profile(actor)["active_plan_id"] is None
    assert proposal["planned_minutes"] == 10 <= proposal["total_minutes"] == 12
    args = ["learning", "accept", proposal["plan_id"], proposal["version_id"], "1"]
    assert runner.invoke(main.app, args).exit_code != 0 and len(requests) == 1
    output.seek(0)
    output.truncate(0)
    result = runner.invoke(main.app, [*args, "--confirm", "ACCEPT"])
    assert result.exit_code == 0, result.output
    accepted = json.loads(output.getvalue())
    assert accepted["schedule_status"] == "accepted"
    assert accepted["session_schedule"] == proposal["schedule"]
    assert service.learning.profile(actor)["active_plan_id"] == proposal["plan_id"]
    assert requests[-1][0:3] == (
        "POST",
        f"/api/v1/plans/{proposal['plan_id']}/accept",
        {"version_id": proposal["version_id"], "expected_revision": 1},
    )


def test_real_assessment_command_returns_provisional_result_via_existing_run_read(
    env, real_cli, tmp_path
):
    from test_model_assessment_v166 import setup_model, submitted

    runner, output, requests = real_cli
    _, attempt = asyncio.run(submitted(env, runtime=True))
    setup_model(env)
    before = env["app"].learning.profile(env["actor"])
    body = {
        "namespace_id": env["ns"],
        "funding_id": env["funding"],
        "attempt_id": attempt["attempt_id"],
        "idempotency_key": "cli-assessment",
    }
    result = runner.invoke(main.app, ["assessment", "request", write_json(tmp_path, body)])
    assert result.exit_code == 0, result.output
    run_id = json.loads(output.getvalue())["run_id"]
    assert len(requests) == 1 and requests[0][0:3] == ("POST", "/api/v1/learning/assessments", body)
    asyncio.run(env["app"].drain())
    output.seek(0)
    output.truncate(0)
    result = runner.invoke(main.app, ["tasks", "--run", run_id])
    assert result.exit_code == 0, result.output
    candidate = json.loads(output.getvalue())["result"]
    assert candidate["evaluator_kind"] == "model_assessment_candidate"
    assert candidate["feedback"]["requires_review"] and not candidate["competence_verified"]
    runtime = next(
        item for item in candidate["rubric_item_decisions"] if item["rubric_key"] == "runtime"
    )
    assert runtime["outcome"] == "unknown" and runtime["guard_reason_codes"] == [
        "runtime_receipt_missing"
    ]
    assert not candidate["human_observation_created"]
    assert env["app"].learning.profile(env["actor"]) == before


def test_partial_source_output_failure_leaves_no_ack():
    class PartialOutput(StringIO):
        def write(self, value):
            if "return 42" in value:
                raise OSError("sink stopped after the first selected line")
            return super().write(value)

    app, output, calls = typer.Typer(), PartialOutput(), []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return navigation_response()

    register_cli(app, request, lambda _: None, lambda: Console(file=output), main.clean)
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "k"])
    assert result.exit_code != 0 and len(calls) == 1
    assert "def run(self):" in output.getvalue() and "return 42" not in output.getvalue()


def test_completed_flush_is_before_ack_and_uncertain_send_is_not_retried():
    import httpx

    events = []

    class TrackedOutput(StringIO):
        def write(self, value):
            events.append(("write", value))
            return super().write(value)

        def flush(self):
            events.append(("flush", None))
            return super().flush()

    app, output, calls = typer.Typer(), TrackedOutput(), []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            assert events[-1][0] == "flush"
            assert "return 42" in output.getvalue()
            raise httpx.ReadError("ACK response was lost; outcome unknown")
        return navigation_response()

    register_cli(app, request, lambda _: None, lambda: Console(file=output), main.clean)
    result = CliRunner().invoke(app, ["source-show", "block-1", "--ack-key", "k"])
    assert result.exit_code != 0
    assert [row[0] for row in calls] == ["GET", "POST"]
