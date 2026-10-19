"""One offline end-to-end exercise of the new pipeline, not a broad regression suite."""

import json
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from test_market_research import post, source
from test_owner_scope import clients as clients
from test_studio_learning_agent import model_result

from careerforge.config.settings import get_settings
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.infrastructure.repository import github_research
from careerforge.schemas.model_io import ToolCall
from careerforge.workflows import study

pytestmark = pytest.mark.infrastructure
CODE = "def execute_tool(tool, arguments):\n    return tool(**arguments)\n"


def test_requirement_review_plan_tutor_chain(clients, monkeypatch, offline_python_sandbox):
    a, b = clients
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-only"))
    original = "岗位要求：熟悉 Python 工具调用与参数传递机制。"
    imported = source(a, original)
    created = post(
        a,
        "/study/tracks",
        {
            "name": "离线链路",
            "source_snapshot_ids": [imported["source_snapshot_id"]],
            "target_count": 2,
        },
    )
    assert created.status_code == 201, created.text
    tid = created.json()["data"]["track_id"]
    path = f"/study/tracks/{tid}"
    assert b.get("/api/v1" + path).status_code == 404
    assert a.get("/api/v1/study/tracks").status_code == 200
    calls = []

    def github_response(request):
        if request.url.path == "/search/repositories":
            return httpx.Response(
                200,
                json={
                    "incomplete_results": False,
                    "total_count": 2,
                    "items": [
                        {
                            "full_name": f"example/{n}",
                            "private": False,
                            "fork": False,
                            "archived": False,
                            "description": "工具调用参考",
                            "stargazers_count": 30,
                            "license": {"spdx_id": "MIT"},
                            "language": "Python",
                        }
                        for n in ["weak", "strong"]
                    ],
                },
            )
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(
            200,
            json={
                "default_branch": "main",
                "fork": False,
                "archived": False,
                "license": {"spdx_id": "MIT"},
            },
        )

    monkeypatch.setattr(
        github_research,
        "client",
        lambda: httpx.Client(transport=httpx.MockTransport(github_response)),
    )
    monkeypatch.setattr(
        study,
        "fetch_github",
        lambda *_: (
            {"tool.py": CODE, "README.md": "Tool implementation", "LICENSE": "MIT License"},
            {},
        ),
    )

    async def complete(self, request):
        data = json.JSONDecoder().raw_decode(request.prompt.split("INPUT_UNTRUSTED_JSON\n")[1])[0]
        calls.append(request)
        if "sources" in data and "requirements" not in data:
            return model_result(
                structured={
                    "requirements": [
                        {
                            "key": "python",
                            "technology": "Python",
                            "keywords": ["execute_tool", "tool"],
                            "depth": "熟悉",
                            "depth_basis": "explicit",
                            "rationale": "岗位原文要求熟悉",
                            "evidence": [
                                {
                                    "source_snapshot_id": imported["source_snapshot_id"],
                                    "quote": "熟悉 Python 工具调用与参数传递机制。",
                                }
                            ],
                        }
                    ],
                    "limitations": [],
                }
            )
        if "reading" in data:
            weak = data["candidate"]["full_name"].endswith("weak")
            return model_result(
                structured={
                    "verdict": "weak" if weak else "strong",
                    "summary": "静态相关性审核",
                    "technology_stack": ["Python"],
                    "quality_notes": ["MIT 许可，实际读取工具调用实现；未经运行。"],
                    "matches": []
                    if weak
                    else [
                        {
                            "requirement_key": "python",
                            "path": "tool.py",
                            "start_line": 1,
                            "end_line": 2,
                            "quote": "return tool(**arguments)",
                            "explanation": "直接体现参数传递",
                        }
                    ],
                    "limitations": ["仅选定窗口"],
                }
            )
        if "intent" in data:
            project = data["projects"][0]
            common = {
                "depth": "熟悉",
                "requirement_keys": ["python"],
                "project_review_ids": [project["id"]],
                "highlight_ids": [project["highlights"][-1]["citation_id"]],
                "prerequisite_keys": [],
                "steps": ["阅读参数传递实现"],
                "deliverable": "说明调用边界",
                "acceptance": ["解释参数如何传递"],
            }
            return model_result(
                structured={
                    "title": "工具实现学习",
                    "summary": "从需求定位调用入口",
                    "modules": [
                        {**common, "key": "root", "parent_key": None, "title": "工具模块"},
                        {**common, "key": "leaf", "parent_key": "root", "title": "execute_tool"},
                    ],
                    "limitations": ["静态代码"],
                }
            )
        if "module" in data:
            if request.tools:
                hit = next(
                    r for r in data["observations"][0]["results"] if r["symbol"] == "execute_tool"
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
            read = data["read_sources"][0]
            return model_result(
                structured={
                    "status": "grounded",
                    "blocks": [
                        {
                            "kind": "diagram",
                            "title": "调用概念图",
                            "nodes": [
                                {"key": "a", "label": "输入参数"},
                                {"key": "b", "label": "工具函数"},
                            ],
                            "edges": [{"source": "a", "target": "b", "label": "传递"}],
                        }
                    ],
                    "points": [
                        {
                            "title": "参数传递",
                            "citation_id": read["citation_id"],
                            "quote": "return tool(**arguments)",
                            "explanation": "将字典展开传给工具",
                        }
                    ],
                    "focus_citation_id": read["citation_id"],
                    "questions": ["如何处理参数错误？"],
                    "limitations": ["未执行代码"],
                }
            )
        return model_result(
            structured={
                "queries": [
                    {
                        "query": "Python tool",
                        "requirement_keys": ["python"],
                        "purpose": "定位工具调用机制",
                    }
                ]
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)

    def current():
        response = a.get("/api/v1" + path)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def run(role, **extra):
        state = current()
        response = post(
            a,
            path + "/runs",
            {
                "expected_revision": state["revision"],
                "role": role,
                "max_output_tokens": 2048,
                "model_call_limit": 2,
                **extra,
            },
        )
        assert response.status_code == 202, response.text
        rid = UUID(response.json()["data"]["run_id"])
        study.execute(rid, "offline-study")
        result = current()
        assert result["active_run"]["status"] in {"succeeded", "partially_succeeded"}, a.get(
            f"/api/v1/runs/{rid}"
        ).text
        return result

    state = run("extract")
    req = state["requirements"]["requirements"][0]
    ev = req["evidence"][0]
    assert original[ev["quote_start"] : ev["quote_end"]] == ev["quote"]
    state = run("discover", auto_review_count=2)
    assert len(state["candidates"]) == 2
    weak = next(c for c in state["candidates"] if c["full_name"].endswith("weak"))
    strong = next(c for c in state["candidates"] if c["full_name"].endswith("strong"))
    assert next(r for r in state["reviews"] if r["candidate_id"] == weak["id"])["admitted"] is False
    assert (
        next(r for r in state["reviews"] if r["candidate_id"] == strong["id"])["admitted"] is True
    )
    assert state["approved_count"] == 1
    accepted = state["approved_projects"][0]
    assert accepted["synthetic"]
    citation_id = accepted["highlights"][0]["citation_id"]
    assert a.get("/api/v1/citations/" + citation_id).status_code == 200
    assert b.get("/api/v1/citations/" + citation_id).status_code == 404
    hits = a.get(
        "/api/v1/knowledge/search",
        params={"q": "execute_tool", "snapshot_id": accepted["repository_snapshot_id"]},
    ).json()["data"]["results"]
    focus_id = next(h["chunk_id"] for h in hits if h["symbol"] == "execute_tool")
    intent = {
        "kind": "function",
        "focus_chunk_id": focus_id,
        "focus": "execute_tool 的参数传递",
        "requirement_keys": ["python"],
        "project_review_ids": [accepted["id"]],
    }
    blocked = post(
        a,
        path + "/runs",
        {"expected_revision": state["revision"], "role": "plan", "intent": intent},
    )
    assert (
        blocked.status_code == 409 and blocked.json()["error"]["code"] == "PROJECT_TARGET_NOT_MET"
    )
    changed = a.put(
        "/api/v1" + path,
        json={
            "expected_revision": state["revision"],
            "target_count": 2,
            "collection_policy": "coverage",
        },
    )
    assert changed.status_code == 200, changed.text
    state = current()
    assert state["collection_policy"] == "coverage"
    assert state["coverage"]["located"] == 1 and state["coverage"]["semantic_verified"] == 0
    unconfirmed = post(
        a,
        path + "/runs",
        {"expected_revision": state["revision"], "role": "plan", "intent": intent},
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["error"]["code"] == "COLLECTION_GAP_CONFIRMATION_REQUIRED"
    state = run("plan", intent=intent, acknowledge_collection_gap=True)
    plan = state["plans"][0]
    assert plan["modules"][1]["parent_key"] == "root"
    assert plan["planning_scope"]["shortfall"] == 1
    assert plan["planning_scope"]["collection_gap_acknowledged"]
    state = run("tutor", plan_id=plan["id"], module_key="leaf", question="解释 execute_tool 并画图")
    turn = state["turns"][0]
    assert turn["points"][0]["quote"] == "return tool(**arguments)"
    assert turn["focus_citation_id"] == turn["points"][0]["citation_id"]
    assert turn["blocks"][0]["kind"] == "diagram"
    assert len(calls) == 7  # Only local provider substitutes; no actual model calls.
    progress = a.put(
        "/api/v1" + path + "/progress",
        json={
            "expected_revision": state["revision"],
            "plan_id": plan["id"],
            "module_key": "leaf",
            "state": "completed",
        },
    )
    assert progress.status_code == 200, progress.text
    assert not progress.json()["data"]["progress"][plan["id"] + ":leaf"]["competence_verified"]
    state = run("extract")
    assert state["approved_count"] == 0 and state["candidates"] == []
    assert state["recheck_pending"] and state["active_plan_id"] == plan["id"]
    assert any(p["id"] == plan["id"] and p["historical_requirements"] for p in state["plans"])
    deleted = a.request(
        "DELETE",
        f"/api/v1/sources/{imported['source_id']}",
        json={"expected_version": 1},
        headers={"Idempotency-Key": "offline-delete-study"},
    )
    assert deleted.status_code in {200, 202, 204}, deleted.text
    assert a.get("/api/v1" + path).status_code == 404
