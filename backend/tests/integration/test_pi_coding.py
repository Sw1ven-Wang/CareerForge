"""Real Pi SDK loop with a fake provider only; no model/network requests."""

import json
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients

from careerforge.application.run_service import cancel
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, ToolCall, Usage
from careerforge.workflows.studio_agent import execute

pytestmark = pytest.mark.infrastructure


def setup_run(client, monkeypatch, budget):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-only-never-send"))
    workspace = post(
        client,
        "/studio/workspaces",
        {
            "name": "Pi offline",
            "files": {
                "main.py": "def registry():\n    return 1\n",
                "unselected.py": "PRIVATE_SCOPE_SENTINEL",
            },
        },
    ).json()["data"]
    response = post(
        client,
        f"/studio/workspaces/{workspace['workspace_id']}/agent-runs",
        {
            "expected_revision": 1,
            "instruction": "Inspect registry and propose a change",
            "paths": ["main.py"],
            "coding_call_limit": budget,
        },
    )
    assert response.status_code == 202, response.text
    return workspace["workspace_id"], UUID(response.json()["data"]["run_id"])


def tool_result(request, name):
    return [
        json.loads(message["content"])
        for message in request.messages
        if message["role"] == "tool" and message["tool_call_id"] == name
    ][-1]


def test_pi_reads_selected_files_then_proposes_review_only(clients, monkeypatch):
    a, _ = clients
    wid, rid = setup_run(a, monkeypatch, 3)
    calls = []

    async def complete(self, request):
        calls.append(request)
        assert request.model == get_settings().model_name and request.reasoning_effort == "low"
        assert request.max_output_tokens == get_settings().model_max_output_tokens
        assert request.response_schema is None
        assert "PRIVATE_SCOPE_SENTINEL" not in json.dumps(request.messages)
        names = {item["function"]["name"] for item in request.tools}
        assert names == {"list_files", "read_file", "search_text", "submit_reply"}
        if len(calls) == 1:
            tools = [
                ToolCall(
                    id="blocked",
                    name="read_file",
                    arguments={"path": "unselected.py", "start_line": 1, "end_line": 1},
                ),
                ToolCall(id="search", name="search_text", arguments={"query": "registry"}),
            ]
        elif len(calls) == 2:
            assert tool_result(request, "blocked")["is_error"] is True
            assert tool_result(request, "search")["matches"][0]["line"] == 1
            tools = [
                ToolCall(
                    id="read",
                    name="read_file",
                    arguments={"path": "main.py", "start_line": 1, "end_line": 200},
                )
            ]
        else:
            assert tool_result(request, "read")["text"] == "def registry():\n    return 1"
            tools = [
                ToolCall(
                    id="proposal",
                    name="submit_reply",
                    arguments={
                        "answer": "建议已生成，未应用或执行。",
                        "citation_ids": [],
                        "edits": [
                            {
                                "path": "main.py",
                                "before": "def registry():\n    return 1\n",
                                "after": "def registry():\n    return 2\n",
                                "reason": "offline",
                            }
                        ],
                        "commands": [
                            {"argv": ["python", "main.py"], "reason": "需要确认"},
                            {"argv": ["python", "-V"], "reason": "需另行确认"},
                        ],
                    },
                )
            ]
        if len(calls) == 3:
            proposal = tools[0].arguments
            proposal["reason"] = proposal["edits"][0].pop("reason")
        return ModelResult(
            provider="offline-mock",
            finish_reason="tool_calls",
            tool_calls=tools,
            synthetic=True,
            usage=Usage(input_tokens=10, output_tokens=20, status="known"),
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    execute(rid, "pi-offline")
    execute(rid, "duplicate")
    data = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    assert data["files"]["main.py"].endswith("return 1\n")
    assert len(calls) == 3
    visible = a.get(f"/api/v1/runs/{rid}")
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["status"] == "succeeded"
    assert [item["status"] for item in data["actions"] if item["kind"] == "command"] == [
        "pending",
        "pending",
    ]
    assert next(item for item in data["actions"] if item["kind"] == "patch")["status"] == "pending"
    message = next(item for item in data["actions"] if item["kind"] == "message")
    assert message["usage"] == {"input_tokens": 30, "output_tokens": 60, "status": "known"}
    with Session(get_engine()) as db:
        run = db.get(AgentRun, rid)
        assert run.status == "succeeded" and run.calls_reserved == run.call_budget == 3
        assert (
            len(db.scalars(select(ModelCallReceipt).where(ModelCallReceipt.run_id == rid)).all())
            == 3
        )
        transcript = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == rid, StageArtifact.stage_key == "pi_transcript"
            )
        )
        assert transcript.payload["calls"] == 3 and transcript.payload["proposed"] is True


@pytest.mark.parametrize("stop", ["budget", "over_limit", "cancel"])
def test_pi_stops_without_extra_calls_or_side_effects(clients, monkeypatch, stop):
    a, _ = clients
    wid, rid = setup_run(a, monkeypatch, 1 if stop == "budget" else 3)
    calls = []

    async def complete(self, request):
        calls.append(request)
        if stop == "cancel":
            with Session(get_engine()) as db, db.begin():
                run = db.get(AgentRun, rid)
                cancel(db, run.owner_id, rid, run.version)
        return ModelResult(
            provider="offline-mock",
            finish_reason="tool_calls",
            synthetic=True,
            tool_calls=[ToolCall(id="search", name="search_text", arguments={"query": "registry"})],
            usage=Usage(
                input_tokens=10,
                output_tokens=request.max_output_tokens + 1 if stop == "over_limit" else 10,
                status="known",
            ),
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    execute(rid, "pi-stop")
    data = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    assert len(calls) == 1 and data["actions"] == []
    with Session(get_engine()) as db:
        run = db.get(AgentRun, rid)
        assert (
            run.status
            == {"budget": "budget_stopped", "over_limit": "failed", "cancel": "cancelled"}[stop]
        )
        assert run.calls_reserved == 1


def test_pi_saved_format_recovery_keeps_budget_and_review_gates(clients, monkeypatch):
    from careerforge.application.pi_coding_service import recover_saved_reply
    from careerforge.domain.errors import BusinessError
    from careerforge.workflows import pi_coding

    a, _ = clients
    wid, rid = setup_run(a, monkeypatch, 1)
    calls = []

    async def complete(self, request):
        calls.append(request)
        return ModelResult(
            provider="offline-mock",
            finish_reason="tool_calls",
            synthetic=True,
            usage=Usage(input_tokens=10, output_tokens=20, status="known"),
            tool_calls=[
                ToolCall(
                    id="proposal",
                    name="submit_reply",
                    arguments={
                        "answer": "offline saved reply",
                        "citation_ids": [],
                        "reason": "single edit reason",
                        "edits": [
                            {
                                "path": "main.py",
                                "before": "def registry():\n    return 1\n",
                                "after": "def registry():\n    return 2\n",
                            }
                        ],
                        "commands": [{"argv": ["python", "main.py"], "reason": "requires RUN"}],
                    },
                )
            ],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    # Reproduce the pre-fix SDK rejection without a real provider call.
    with monkeypatch.context() as context:
        context.setattr(pi_coding, "normalize_reply_arguments", lambda value: (value, []))
        execute(rid, "pi-format-offline")
    with Session(get_engine()) as db, db.begin():
        run = db.get(AgentRun, rid)
        assert run.status == "budget_stopped" and run.calls_reserved == 1
        owner = run.owner_id
        receipt = db.scalar(select(ModelCallReceipt).where(ModelCallReceipt.run_id == rid))
        receipt.status = "unknown"
        db.flush()
        with pytest.raises(BusinessError) as error:
            recover_saved_reply(db, owner, rid, 1)
        assert error.value.code == "PI_RECOVERY_UNCERTAIN"
        db.rollback()
    with Session(get_engine()) as db, db.begin():
        raw = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == rid, StageArtifact.stage_key == "model_result:1"
            )
        ).payload
        result = recover_saved_reply(db, owner, rid, 1)
        assert result["files"]["main.py"] == "def registry():\n    return 1\n"
        assert len(result["actions"]) == 3
        assert (
            next(item for item in result["actions"] if item["kind"] == "patch")["status"]
            == "pending"
        )
        assert (
            next(item for item in result["actions"] if item["kind"] == "command")["status"]
            == "pending"
        )
        assert len(recover_saved_reply(db, owner, rid, 1)["actions"]) == 3
        assert db.get(AgentRun, rid).status == "budget_stopped"
        assert db.get(AgentRun, rid).calls_reserved == 1
        assert (
            db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == rid, StageArtifact.stage_key == "model_result:1"
                )
            ).payload
            == raw
        )
    assert len(calls) == 1
