"""One local-provider investigation → evidence → admission → replay exercise."""

import json
from uuid import UUID

import pytest
from careerforge.application import study_service as service
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import AgentRun, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ToolCall
from careerforge.workflows import study
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post, source
from test_owner_scope import clients as clients
from test_studio_learning_agent import model_result

pytestmark = pytest.mark.infrastructure
FILES = {
    "a_entry.py": (
        "def execute_agent(tool, arguments):\n"
        "    record = persist(tool)\n"
        "    return hydrate(record, arguments)\n"
    ),
    "b_persist.py": "def persist(tool):\n    return tool\n",
    "z_storage.py": "def hydrate(record, arguments):\n    return record(**arguments)\n",
    "tests/test_entry.py": "def test_entry():\n    assert execute_agent(str, {}) is not None\n",
    "LICENSE": "MIT License",
}


def test_review_supplemental_reads_are_frozen_cited_and_replayed(clients, monkeypatch):
    a, b = clients
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-only"))
    imported = source(a, "岗位要求：熟悉 Agent 工具调用与参数传递。")
    created = post(
        a,
        "/study/tracks",
        {"name": "B offline", "source_snapshot_ids": [imported["source_snapshot_id"]]},
    ).json()["data"]
    tid = created["track_id"]
    path = f"/study/tracks/{tid}"
    requirement = {
        "key": "tools",
        "technology": "Agent",
        "keywords": ["execute_agent", "agent", "tool"],
        "depth": "熟悉",
        "depth_basis": "explicit",
        "rationale": "原文要求",
        "evidence": [],
    }
    with Session(get_engine()) as db, db.begin():
        row = db.get(StudyTrack, UUID(tid))
        item = service.put(
            db, row, "requirements", {"requirements": [requirement], "limitations": []}
        )
        row.payload = {**row.payload, "requirements_id": str(item.id)}
    project = post(
        a,
        "/knowledge/imports",
        {
            "name": "offline B",
            "github_url": "https://github.com/example/investigation",
            "requested_ref": "b" * 40,
            "files": FILES,
        },
    ).json()["data"]
    state = post(
        a,
        path + "/candidates",
        {
            "expected_revision": created["revision"],
            "repository_snapshot_id": project["repository_snapshot_id"],
        },
    ).json()["data"]
    candidate = state["candidates"][0]
    calls = []

    async def complete(self, request):
        data = json.JSONDecoder().raw_decode(request.prompt.split("INPUT_UNTRUSTED_JSON\n")[1])[0]
        calls.append(request)
        if data["phase"] == "investigate":
            if len(calls) == 1:
                target = next(
                    t for t in data["investigation"]["repo_map"] if t["symbol"] == "hydrate"
                )
                return model_result(
                    [
                        ToolCall(id="symbol", name="find_symbol", arguments={"query": "hydrate"}),
                        ToolCall(
                            id="read",
                            name="read_span",
                            arguments={
                                "target_id": target["target_id"],
                                "start_line": None,
                                "end_line": None,
                            },
                        ),
                    ]
                )
            return model_result(
                [
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={
                            "reason": "evidence_sufficient",
                            "explanation": "Read parameter forwarding and related test source",
                        },
                    )
                ]
            )
        assert data["phase"] == "final_review" and not request.tools
        assert any(r["path"] == "z_storage.py" for r in data["reading"]["code"])
        assert data["reading"]["investigation"]["test_reads"]
        return model_result(
            structured={
                "verdict": "strong",
                "summary": "静态参数传递实现",
                "technology_stack": ["Python"],
                "quality_notes": ["MIT; no execution"],
                "matches": [
                    {
                        "requirement_key": "tools",
                        "path": "z_storage.py",
                        "start_line": 2,
                        "end_line": 2,
                        "quote": "return record(**arguments)",
                        "explanation": "将参数字典传给已选 callable，未声称持久化",
                    }
                ],
                "counterevidence": [],
                "limitations": ["Tests read, not run"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    response = post(
        a,
        path + "/runs",
        {
            "expected_revision": state["revision"],
            "role": "review",
            "candidate_id": candidate["id"],
            "review_call_limit": 3,
            "max_output_tokens": 2048,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["data"]["model_call_limit"] == 3
    rid = UUID(response.json()["data"]["run_id"])
    study.execute(rid, "offline-investigation")
    state = a.get("/api/v1" + path).json()["data"]
    review = state["reviews"][0]
    assert review["admitted"] and review["investigation"]["stop_reason"] == "evidence_sufficient"
    assert review["investigation"]["decision_calls"] == 2 and len(calls) == 3
    assert (
        review["investigation"]["read_count"] <= 6 and review["investigation"]["tool_count"] <= 12
    )
    claim = review["claims"][0]
    assert claim["review_status"] == "needs_semantic_review" and not claim["counterevidence_refs"]
    evidence = next(e for e in review["evidence"] if e["evidence_id"] in claim["support_refs"])
    cid = evidence["citation_id"]
    assert evidence["quote"] == "return record(**arguments)" and cid
    assert a.get("/api/v1/citations/" + cid).status_code == 200
    assert b.get("/api/v1/citations/" + cid).status_code == 404
    with Session(get_engine()) as db:
        run = db.get(AgentRun, rid)
        assert run.calls_reserved == run.call_budget == 3
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == rid, StageArtifact.stage_key == "review_investigation"
            )
        )
        assert (
            stage.payload["investigation"]["archive_hash"]
            == review["investigation"]["archive_hash"]
        )
    study.execute(rid, "duplicate-investigation")
    assert len(calls) == 3
