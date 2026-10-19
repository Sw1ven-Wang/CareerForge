"""Targeted synthetic regressions. No real providers, commands, repositories or user data."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients
from test_studio_delivery import confirmation
from test_studio_learning_agent import model_result
from test_study_practice import CODE, base, setup_module_plan, start

from careerforge.application import context_service
from careerforge.application import studio_service as studio
from careerforge.application import study_practice_service as practice
from careerforge.application import study_service as study
from careerforge.application.study_versions import family
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure import runtime_capabilities as runtime
from careerforge.infrastructure.db.models import AgentRun, LearningWorkspace
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.workflows.assessment.workflow import execute as assess
from careerforge.workflows.study import execute

pytestmark = pytest.mark.infrastructure


def test_frozen_history_survives_reextract_and_rereview(clients, monkeypatch):
    client, _ = clients
    fixture = setup_module_plan(client, monkeypatch)
    attempt, _ = start(client, fixture)
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        original = study.current_requirements(db, track)
        plan = study.artifact_row(db, track.owner_id, track.id, fixture["pid"], "plan")
        review = study.artifact_row(
            db, track.owner_id, track.id, plan.payload["review_ids"][0], "review"
        )
        study.put(db, track, "review", deepcopy(review.payload))
        newest = study.put(db, track, "requirements", deepcopy(original.payload))
        track.payload = {**track.payload, "requirements_id": str(newest.id)}
    state = client.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    old = next(p for p in state["plans"] if p["id"] == fixture["pid"])
    assert old["historical_requirements"]
    assert client.get("/api/v1" + base(attempt) + "/delivery").status_code == 200
    # A frozen plan can still start another practice without borrowing the latest review scope.
    newer_attempt, _ = start(client, fixture)
    assert newer_attempt["attempt_id"] != attempt["attempt_id"]


def test_plan_target_and_history_stay_in_their_family(clients, monkeypatch):
    client, _ = clients
    fixture = setup_module_plan(client, monkeypatch)
    attempt, _ = start(client, fixture)
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        old = study.artifact_row(db, track.owner_id, track.id, fixture["pid"], "plan")
        turn = study.put(
            db,
            track,
            "tutor",
            {
                "plan_id": str(old.id),
                "module_key": "leaf",
                "requirements_id": old.payload["requirements_id"],
                "review_ids": old.payload["review_ids"],
                "question": "prior question",
                "blocks": [{"kind": "text", "text": "prior explanation"}],
            },
        )
        upgraded = study.put(
            db,
            track,
            "plan",
            {**deepcopy(old.payload), "source_plan_id": str(old.id), "revision_no": 2},
        )
        other = study.put(
            db,
            track,
            "plan",
            {**deepcopy(old.payload), "intent": {"kind": "learn_project", "focus": "other"}},
        )
        track.payload = {**track.payload, "active_plan_id": str(other.id)}
        workspace = db.get(LearningWorkspace, UUID(attempt["workspace_id"]))
        item = studio.action(workspace, attempt["practice_id"])
        assert practice.plan_target(db, track.owner_id, item)[0]["plan_id"] == str(upgraded.id)
        assert family(db, upgraded) != family(db, other)
        manifest = {
            "track_id": str(track.id),
            "plan_id": str(upgraded.id),
            "module": fixture["module"],
            "requirements_id": old.payload["requirements_id"],
            "review_ids": old.payload["review_ids"],
            "scope": [],
            "model_call_limit": 1,
            "max_output_tokens": 2048,
        }
        packet = context_service.capture(db, track.owner_id, manifest)
        assert any(p["source_id"] == str(turn.id) for p in packet["catalog"])


def test_report_submission_acceptance_without_fake_code_or_commands(clients, monkeypatch):
    client, _ = clients
    fixture = setup_module_plan(client, monkeypatch)
    attempt, _ = start(client, fixture)
    state = client.get("/api/v1" + base(attempt) + "/delivery").json()["data"]
    assert {c["dimension"] for c in state["criteria"]} == {"artifact", "explanation"}
    response = post(
        client,
        base(attempt) + "/submissions",
        {
            "expected_revision": state["workspace_revision"],
            "summary": "解释参数分发的架构、关键流程和失败边界，引用本模块固定源码。",
            "command_ids": [],
            "explanation_answers": [
                {
                    "question": "解释关键机制",
                    "answer": "参数以关键字传递；异常边界未在此摘录中实现。",
                    "criterion_version_ids": [c["criterion_version_id"]],
                }
                for c in state["criteria"]
                if c["dimension"] == "explanation"
            ],
        },
    )
    assert response.status_code == 201, response.text
    value = response.json()["data"]
    assert value["submission"]["workspace_snapshot"]["files"] == {}
    assert value["submission"]["workspace_snapshot"]["report"]["references"]
    assess(UUID(value["assessment_run_id"]), "offline-report")
    value = client.get("/api/v1" + base(attempt) + "/delivery").json()["data"]
    accepted = post(
        client,
        base(attempt) + "/assessments/" + value["assessment"]["assessment_id"] + "/confirm",
        confirmation(value),
    )
    assert accepted.status_code == 200, accepted.text


def test_workspace_mutex_idempotency_and_terminal_release(clients, monkeypatch):
    client, _ = clients
    setup_module_plan(client, monkeypatch)
    workspace = post(
        client, "/studio/workspaces", {"name": "isolated coding fixture", "files": {}}
    ).json()["data"]
    path = "/studio/workspaces/" + workspace["workspace_id"] + "/agent-runs"
    body = {
        "expected_revision": workspace["revision"],
        "instruction": "offline fixture: do not execute",
    }
    keys = [str(uuid4()), str(uuid4())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: post(client, path, body, key), keys))
    assert sorted(r.status_code for r in results) == [202, 409]
    winner = next(i for i, r in enumerate(results) if r.status_code == 202)
    first, key = results[winner], keys[winner]
    assert results[1 - winner].json()["error"]["code"] == "WORKSPACE_AGENT_ACTIVE"
    assert post(client, path, body, key).json()["data"]["run_id"] == first.json()["data"]["run_id"]
    with Session(get_engine()) as db, db.begin():
        run = db.get(AgentRun, UUID(first.json()["data"]["run_id"]))
        run.status = "cancelled"
    assert post(client, path, body).status_code == 202


def test_runtime_preflight_preserves_confirmation_and_rejects_missing_tool(clients, monkeypatch):
    client, _ = clients
    monkeypatch.setattr(
        runtime,
        "capabilities",
        lambda: {
            "status": "ready",
            "ready": True,
            "tools": {"python": True, "uv": True, "node": False},
            "limits": runtime.LIMITS,
        },
    )
    workspace = post(client, "/studio/workspaces", {"name": "runtime fixture", "files": {}}).json()[
        "data"
    ]
    path = "/studio/workspaces/" + workspace["workspace_id"] + "/commands"
    body = {
        "expected_revision": workspace["revision"],
        "argv": ["node", "main.js"],
        "reason": "fixture",
    }
    assert post(client, path, body).json()["error"]["code"] == "SANDBOX_TOOL_UNAVAILABLE"
    body["argv"] = ["python", "main.py"]
    proposed = post(client, path, body).json()["data"]
    command = next(a for a in proposed["actions"] if a["kind"] == "command")
    assert command["status"] == "pending"
    assert (
        post(
            client,
            path + "/" + command["id"] + "/execute",
            {"expected_revision": workspace["revision"]},
        ).status_code
        == 422
    )
    with pytest.raises(BusinessError) as missing:
        runtime.planning(
            {"kind": "build_project", "requirement_keys": ["go"], "execution_runtime": "go"},
            [{"key": "go", "technology": "Go", "depth": "熟悉"}],
            [],
        )
    assert missing.value.code == "PLAN_RUNTIME_UNAVAILABLE"


@pytest.mark.parametrize("extra_calls", [0, 1])
def test_reextract_runs_one_bounded_reassessment_and_preserves_original(
    clients, monkeypatch, extra_calls
):
    client, _ = clients
    fixture = setup_module_plan(client, monkeypatch)
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        plan = study.artifact_row(db, track.owner_id, track.id, fixture["pid"], "plan")
        review = study.artifact_row(
            db, track.owner_id, track.id, plan.payload["review_ids"][0], "review"
        )
        review.payload = {
            **review.payload,
            "candidate": {**review.payload["candidate"], "license": "MIT"},
        }
        rid = str(review.id)
        before = deepcopy(plan.payload)
    calls = []

    async def complete(self, request):
        calls.append(request)
        if len(calls) == 1:
            return model_result(
                structured={
                    "requirements": [
                        {
                            "key": "python",
                            "technology": "Python",
                            "keywords": ["dispatch"],
                            "depth": "熟悉",
                            "depth_basis": "explicit",
                            "rationale": "原文要求",
                            "evidence": [
                                {
                                    "source_snapshot_id": fixture["source"]["source_snapshot_id"],
                                    "quote": "熟悉 Python 参数分发和失败边界",
                                }
                            ],
                        }
                    ],
                    "limitations": [],
                }
            )
        return model_result(
            structured={
                "status": "usable",
                "summary": "已有固定实现仍覆盖参数分发目标；原文重审通过。",
                "project_reviews": [
                    {
                        "review_id": rid,
                        "review": {
                            "verdict": "strong",
                            "summary": "实现参数分发",
                            "technology_stack": ["Python"],
                            "quality_notes": ["固定源码"],
                            "matches": [
                                {
                                    "requirement_key": "python",
                                    "path": "tool.py",
                                    "start_line": 1,
                                    "end_line": 2,
                                    "quote": CODE.strip(),
                                    "explanation": "直接实现参数分发，异常边界仍未覆盖",
                                }
                            ],
                            "counterevidence": [],
                            "limitations": [],
                        },
                    }
                ],
                "plan": None,
                "limitations": ["有界旧窗口重读，不代替完整项目调查"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    state = client.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    response = post(
        client,
        "/study/tracks/" + fixture["tid"] + "/runs",
        {
            "expected_revision": state["revision"],
            "role": "extract",
            "recheck_call_budget": extra_calls,
            "max_output_tokens": 2048,
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["data"]["run_id"])
    execute(run_id, "offline-recheck")
    state = client.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    assert len(calls) == 1 + extra_calls
    if not extra_calls:
        assert state["recheck_pending"] and not state["reconciliations"]
        assert state["active_plan_id"] == fixture["pid"]
        return
    assert state["reconciliations"], state
    result = state["reconciliations"][-1]
    assert result["status"] == "usable", result
    assert result["new_plan_id"] != fixture["pid"]
    assert state["active_plan_id"] == result["new_plan_id"]
    assert (
        next(p for p in state["plans"] if p["id"] == fixture["pid"])["modules"] == before["modules"]
    )
    with Session(get_engine()) as db:
        run = db.get(AgentRun, run_id)
        assert run.calls_reserved == 2 and run.call_budget == 2
