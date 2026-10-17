import json
from uuid import UUID, uuid4

import pytest
from careerforge.application.studio_learning_service import validate_guide
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, ToolCall, Usage
from careerforge.schemas.studio import LearningGuide
from careerforge.workflows.studio_learning import execute
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients
from test_planning import objective_body

pytestmark = pytest.mark.infrastructure

SOURCE = """def execute_tool(tool, arguments):
    return tool(**arguments)


def run_agent(tool):
    return execute_tool(tool, {})
"""


def setup_learning(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-placeholder"))
    project = post(
        client,
        "/knowledge/imports",
        {
            "name": "Tool learning",
            "github_url": "https://github.com/example/study",
            "requested_ref": "c" * 40,
            "files": {"agent.py": SOURCE},
            "tags": ["工具调用"],
        },
    ).json()["data"]
    body = objective_body(goal(client))
    body.update(title="工具调用", target_behavior="理解 run_agent 到 execute_tool 的关系")
    objective = post(client, "/learning-objectives", body).json()["data"]["objective_version_id"]
    workspace = post(
        client, "/studio/workspaces", {"name": "Learning", "files": {"mine.py": "print(1)\n"}}
    ).json()["data"]
    request = {
        "expected_revision": 1,
        "mode": "learn",
        "instruction": "解释工具调用并给出迁移练习",
        "objective_version_id": objective,
        "knowledge_snapshot_ids": [project["repository_snapshot_id"]],
        "model_call_limit": 4,
    }
    return project, workspace, request


def prompt_data(request):
    start = request.prompt.index('{"task"')
    return json.JSONDecoder().raw_decode(request.prompt[start:])[0]


def model_result(calls=None, structured=None):
    return ModelResult(
        provider="offline-mock",
        finish_reason="tool_calls" if calls else "stop",
        tool_calls=calls or [],
        structured=structured,
        usage=Usage(input_tokens=100, output_tokens=50, status="known"),
        synthetic=True,
    )


def test_agent_discovers_reads_and_saves_verified_explanation(clients, monkeypatch):
    a, b = clients
    project, workspace, request = setup_learning(a, monkeypatch)
    wid = workspace["workspace_id"]
    key = str(uuid4())
    response = post(a, f"/studio/workspaces/{wid}/agent-runs", request, key)
    assert response.status_code == 202, response.text
    rid = UUID(response.json()["data"]["run_id"])
    assert post(a, f"/studio/workspaces/{wid}/agent-runs", request, key).json()["data"][
        "run_id"
    ] == str(rid)
    assert post(a, f"/studio/workspaces/{wid}/agent-runs", request).status_code == 409
    assert b.get(f"/api/v1/studio/workspaces/{wid}").status_code == 404
    calls = []

    async def complete(self, req):
        calls.append(req)
        data = prompt_data(req)
        if len(calls) == 1:
            hit = next(
                i
                for i in data["observations_untrusted"][0]["results"]
                if i["symbol"] == "run_agent"
            )
            return model_result(
                [
                    ToolCall(
                        id="read-entry",
                        name="read_code",
                        arguments={
                            "chunk_id": hit["chunk_id"],
                            "start_line": None,
                            "end_line": None,
                        },
                    ),
                    ToolCall(
                        id="follow-reference",
                        name="related_code",
                        arguments={"chunk_id": hit["chunk_id"]},
                    ),
                ]
            )
        if len(calls) == 2:
            hit = next(
                i
                for i in data["observations_untrusted"][-1]["results"]
                if i["symbol"] == "execute_tool"
            )
            return model_result(
                [
                    ToolCall(
                        id="read-definition",
                        name="read_code",
                        arguments={
                            "chunk_id": hit["chunk_id"],
                            "start_line": None,
                            "end_line": None,
                        },
                    )
                ]
            )
        if not req.response_schema:
            return model_result()
        source = next(i for i in data["read_sources_untrusted"] if i["symbol"] == "execute_tool")
        assert "mine.py" not in req.prompt
        return model_result(
            structured={
                "status": "grounded",
                "summary": "围绕工具调用读取两个定义。",
                "points": [
                    {
                        "title": "参数展开",
                        "explanation": "这个函数将参数字典展开给工具。",
                        "citation_id": source["citation_id"],
                        "quote": "return tool(**arguments)",
                    }
                ],
                "reading_order": [i["citation_id"] for i in data["read_sources_untrusted"]],
                "practice": ["在自己的项目中实现一个只读工具分发练习。"],
                "questions": ["工具不存在时如何处理？"],
                "limitations": ["仅静态阅读，未运行。"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    execute(rid, "offline-study")
    execute(rid, "duplicate-delivery")
    assert len(calls) == 4 and all(
        c.max_output_tokens == get_settings().model_max_output_tokens for c in calls
    )
    data = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    assert data["files"] == workspace["files"]
    assert len(data["actions"]) == 1 and data["actions"][0]["kind"] == "learning_report"
    report = data["actions"][0]
    assert report["synthetic"] and report["guide"]["status"] == "grounded"
    assert report["usage"]["calls"] == 4 and report["usage"]["input_tokens"] == 400
    point = report["guide"]["points"][0]
    assert point["quote_start_line"] == point["quote_end_line"] == 2
    assert point["path"] == "agent.py" and point["commit"] == "c" * 40
    assert {t["tool"] for t in report["trace"]} == {"search_code", "read_code", "related_code"}
    assert a.get(f"/api/v1/runs/{rid}").json()["data"]["status"] == "succeeded"
    # Plausible explanations cannot substitute fabricated or unread quotations.
    bad = {
        **report["guide"],
        "points": [
            {
                "title": "bad",
                "explanation": "bad evidence",
                "citation_id": point["citation_id"],
                "quote": "return invented_result",
            }
        ],
    }
    with Session(get_engine()) as db:
        from careerforge.infrastructure.db.models import LearningWorkspace

        owner = db.get(LearningWorkspace, UUID(wid)).owner_id
        with pytest.raises(ValueError):
            validate_guide(db, owner, LearningGuide.model_validate(bad), report["sources"])
        bad["points"][0]["citation_id"] = str(uuid4())
        with pytest.raises(ValueError):
            validate_guide(db, owner, LearningGuide.model_validate(bad), report["sources"])
    a.request(
        "DELETE",
        "/api/v1/repositories/" + project["repository_id"],
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    hidden = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]["actions"][0]
    assert (
        hidden["status"] == "unavailable"
        and hidden["sources"] == []
        and hidden["guide"]["points"] == []
    )


def test_unread_and_unknown_tools_do_not_create_evidence(clients, monkeypatch):
    a, _ = clients
    _, workspace, request = setup_learning(a, monkeypatch)
    wid = workspace["workspace_id"]
    response = post(a, f"/studio/workspaces/{wid}/agent-runs", request)
    rid = UUID(response.json()["data"]["run_id"])
    calls = []

    async def complete(self, req):
        calls.append(req)
        if len(calls) == 1:
            return model_result(
                [
                    ToolCall(
                        id="forbidden",
                        name="execute_command",
                        arguments={"argv": ["python", "mine.py"]},
                    ),
                    ToolCall(
                        id="undiscovered",
                        name="read_code",
                        arguments={"chunk_id": str(uuid4()), "start_line": None, "end_line": None},
                    ),
                ]
            )
        return model_result()

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    execute(rid, "offline-invalid-tools")
    report = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]["actions"][0]
    assert report["kind"] == "learning_report" and report["generated_by"] == "system"
    assert report["guide"]["status"] == "insufficient_evidence" and report["guide"]["points"] == []
    assert {t["status"] for t in report["trace"]} >= {"UNKNOWN_TOOL", "UNDISCOVERED_CHUNK"}
    assert len(calls) == 2 and not any(c.response_schema for c in calls)
    # Cancel before execution: no provider call or fabricated report.
    created = post(a, f"/studio/workspaces/{wid}/agent-runs", request).json()["data"]
    run = a.get("/api/v1/runs/" + created["run_id"]).json()["data"]
    assert (
        a.post(
            "/api/v1/runs/" + created["run_id"] + "/cancel",
            json={"expected_version": run["version"]},
        ).status_code
        == 200
    )
    execute(UUID(created["run_id"]), "cancelled-before-start")
    assert len(calls) == 2
    latest = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]["actions"][-1]
    assert latest["kind"] == "learning_run" and latest["status"] == "cancelled"
