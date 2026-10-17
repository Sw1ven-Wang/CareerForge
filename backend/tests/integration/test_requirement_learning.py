from uuid import UUID, uuid4

import pytest
from careerforge.application.studio_learning_service import requirement_links
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ToolCall
from careerforge.schemas.studio import LearningGuide
from careerforge.workflows.studio_learning import execute
from test_market_research import post
from test_owner_scope import clients as clients
from test_studio_learning_agent import model_result, prompt_data, setup_learning

pytestmark = pytest.mark.infrastructure


def test_manual_requirement_to_code_and_local_snapshot(clients, monkeypatch):
    a, b = clients
    project, workspace, request = setup_learning(a, monkeypatch)
    text = "技术学习：需要理解只读工具注册与输入参数校验机制。"
    imported = post(
        a,
        "/sources/import",
        {"source_type": "technical_material", "title": "工具调用技术", "content": text},
    ).json()["data"]
    request.update(
        objective_version_id=None,
        source_snapshot_ids=[imported["source_snapshot_id"]],
        model_call_limit=2,
    )
    wid = workspace["workspace_id"]
    response = post(a, f"/studio/workspaces/{wid}/agent-runs", request)
    assert response.status_code == 202, response.text
    calls = []

    async def complete(self, value):
        calls.append(value)
        data = prompt_data(value)
        assert data["task"]["requirements_untrusted"][0]["content"] == text
        if value.tools:
            hit = next(
                h
                for h in data["observations_untrusted"][0]["results"]
                if h["symbol"] == "execute_tool"
            )
            return model_result(
                [
                    ToolCall(
                        id="read",
                        name="read_code",
                        arguments={
                            "chunk_id": hit["chunk_id"],
                            "start_line": None,
                            "end_line": None,
                        },
                    )
                ]
            )
        source = data["read_sources_untrusted"][0]
        return model_result(
            structured={
                "status": "grounded",
                "summary": "从人工技术资料匹配工具调用代码",
                "points": [
                    {
                        "title": "调用入口",
                        "explanation": "这里只观察到工具调用，尚未证明权限和参数校验。",
                        "citation_id": source["citation_id"],
                        "quote": "return tool(**arguments)",
                    }
                ],
                "requirement_links": [
                    {
                        "source_snapshot_id": imported["source_snapshot_id"],
                        "quote": "需要理解只读工具注册与输入参数校验机制。",
                        "citation_id": source["citation_id"],
                        "explanation": "调用入口可作为阅读起点，所选片段未覆盖注册和校验。",
                    }
                ],
                "reading_order": [source["citation_id"]],
                "practice": [],
                "questions": [],
                "limitations": ["仅静态源码"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    run_id = UUID(response.json()["data"]["run_id"])
    execute(run_id, "offline-requirement")
    saved = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    report = next(item for item in saved["actions"] if item["kind"] == "learning_report")
    assert report["status"] == "completed", report
    assert report["objective_version_id"] is None
    assert len(calls) == 2
    link = report["guide"]["requirement_links"][0]
    assert text[link["quote_start"] : link["quote_end"]] == link["quote"]
    assert report["requirements"][0]["content_hash"] == imported["content_hash"]
    assert "content" not in report["requirements"][0]
    guide = LearningGuide.model_validate(
        {
            **report["guide"],
            "points": [],
            "requirement_links": [
                {
                    "source_snapshot_id": imported["source_snapshot_id"],
                    "quote": "这段原文实际不存在。",
                    "citation_id": link["citation_id"],
                    "explanation": "fake",
                }
            ],
        }
    )
    with pytest.raises(ValueError):
        requirement_links(
            guide,
            [{"source_snapshot_id": imported["source_snapshot_id"], "content": text}],
            report["sources"],
        )

    payload = {"expected_revision": saved["revision"], "files": {"selected.py": "print(2)\n"}}
    endpoint = f"/api/v1/studio/workspaces/{wid}/snapshot"
    key = str(uuid4())
    synced = a.put(endpoint, json=payload, headers={"Idempotency-Key": key})
    assert synced.status_code == 200, synced.text
    assert synced.json()["data"]["files"] == payload["files"]
    assert (
        a.put(endpoint, json=payload, headers={"Idempotency-Key": key}).json()["data"]["revision"]
        == synced.json()["data"]["revision"]
    )
    assert (
        a.put(endpoint, json=payload, headers={"Idempotency-Key": str(uuid4())}).status_code == 409
    )
    assert (
        b.put(endpoint, json=payload, headers={"Idempotency-Key": str(uuid4())}).status_code == 404
    )
    assert post(a, "/collection/runs", {}).status_code == 410
    assert (
        a.request(
            "DELETE",
            f"/api/v1/sources/{imported['source_id']}",
            json={"expected_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 202
    )
    hidden = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    assert (
        next(item for item in hidden["actions"] if item["id"] == report["id"])["status"]
        == "unavailable"
    )
