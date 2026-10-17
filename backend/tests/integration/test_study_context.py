"""D package: local provider substitutes, no remote model or project commands."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from careerforge.application import study_service as service
from careerforge.application.maintenance_service import authorized
from careerforge.application.run_service import claim, commit_stage, db_now, reap
from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.db.models import AgentRun, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ToolCall
from careerforge.workflows import study
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients
from test_studio_learning_agent import model_result
from test_study_practice import CODE, frozen_command_fixture, setup_module_plan, start, submit, work

pytestmark = pytest.mark.infrastructure


def tutor_run(client, fixture, calls=3):
    state = client.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    response = post(
        client,
        "/study/tracks/" + fixture["tid"] + "/runs",
        {
            "expected_revision": state["revision"],
            "role": "tutor",
            "plan_id": fixture["pid"],
            "module_key": "leaf",
            "question": "继续解释参数分发与失败边界",
            "model_call_limit": calls,
            "max_output_tokens": 2048,
        },
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["data"]["run_id"])


def seed_history(fixture):
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        plan = service.artifact_row(db, track.owner_id, track.id, fixture["pid"], "plan")
        turn = service.put(
            db,
            track,
            "tutor",
            {
                "plan_id": fixture["pid"],
                "module_key": "leaf",
                "requirements_id": track.payload["requirements_id"],
                "review_ids": plan.payload["review_ids"],
                "question": "这是一段历史问题",
                "blocks": [{"kind": "text", "text": "原始解释。" * 2500 + "TAIL-EVIDENCE"}],
                "points": [],
                "sources": [],
                "trace": [],
                "status": "insufficient_evidence",
                "questions": [],
                "limitations": ["synthetic prior answer"],
                "focus_citation_id": None,
                "synthetic": True,
            },
        )
        return str(turn.id)


def test_context_recall_skills_learner_evidence_and_saved_result_recovery(clients, monkeypatch):
    a, b = clients
    fixture = setup_module_plan(a, monkeypatch)
    attempt, _ = start(a, fixture)
    work(a, attempt, CODE)
    submitted = submit(a, attempt, frozen_command_fixture(attempt))
    old = seed_history(fixture)
    rid = tutor_run(a, fixture)
    sent = []

    async def complete(self, request):
        data = json.JSONDecoder().raw_decode(request.prompt.split("INPUT_UNTRUSTED_JSON\n")[1])[0]
        sent.append(data)
        assert data["context_manifest"]["task"]["model_call_limit"] == 3
        assert "TAIL-EVIDENCE" not in json.dumps(data, ensure_ascii=False)
        assert any(r["kind"] == "attempt" for r in data["context_manifest"]["evidence"])
        if len(sent) == 1:
            return model_result(
                [
                    ToolCall(
                        id="history",
                        name="read_context_evidence",
                        arguments={"evidence_id": "turn:" + old, "offset": 0, "limit": 1000},
                    ),
                    ToolCall(
                        id="skill",
                        name="load_learning_skill",
                        arguments={"skill_id": "failure_boundary"},
                    ),
                ]
            )
        if len(sent) == 2:
            assert data["recalled_evidence"][0]["source_id"] == old
            assert data["loaded_skills"][0]["kind"] == "skill"
            hit = next(
                r
                for obs in data["observations"]
                for r in obs.get("results", [])
                if r.get("symbol") == "dispatch"
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
                "blocks": [{"kind": "text", "text": "合成的教学组织"}],
                "points": [
                    {
                        "title": "参数展开",
                        "citation_id": read["citation_id"],
                        "quote": "return tool(**arguments)",
                        "explanation": "离线夹具说明",
                    }
                ],
                "focus_citation_id": read["citation_id"],
                "questions": [],
                "limitations": ["未运行"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    original_store = study.Runner.store
    crashed = []

    def crash_after_saved(self, *args, **kwargs):
        if not crashed:
            crashed.append(True)
            raise LeaseLost()
        return original_store(self, *args, **kwargs)

    monkeypatch.setattr(study.Runner, "store", crash_after_saved)
    study.execute(rid, "offline-D-first")
    assert len(sent) == 3
    with Session(get_engine()) as db, db.begin():
        run = db.get(AgentRun, rid)
        assert run.status == "running" and run.workflow_version == "agent-study-2.3"
        assert "history" not in run.input_manifest
        authorized(db, run)
        assert len(json.dumps(run.input_manifest["context_scope"], ensure_ascii=False)) < 8000
        run.lease_until = db_now(db) - timedelta(seconds=1)
    with Session(get_engine()) as db, db.begin():
        assert reap(db) >= 1

    async def forbidden(*args, **kwargs):
        raise AssertionError("saved result recovery must not call a provider")

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", forbidden)
    study.execute(rid, "offline-D-recover")
    path = "/api/v1/study/tracks/" + fixture["tid"] + "/runs/" + str(rid) + "/context"
    trace = a.get(path)
    assert trace.status_code == 200, trace.text
    data = trace.json()["data"]
    assert data["status"] == "succeeded" and len(data["packets"]) == 3
    assert len(data["provider_usage"]) == 3 and len(sent) == 3
    assert all(p["prompt_chars"] < 47000 for p in data["packets"])
    assert b.get(path).status_code == 404
    assert a.get(path + "/evidence?evidence_id=turn:" + str(uuid4())).status_code == 404
    ref = next(r for r in data["catalog"] if r["source_id"] == old)
    first = a.get(
        path + "/evidence", params={"evidence_id": ref["evidence_id"], "limit": 1000}
    ).json()["data"]
    last = a.get(
        path + "/evidence",
        params={"evidence_id": ref["evidence_id"], "offset": max(0, first["total_chars"] - 2500)},
    ).json()["data"]
    assert first["truncated"] and "TAIL-EVIDENCE" in last["content"]
    assert last["content_hash"] == first["content_hash"] == ref["content_hash"]
    learner = next(r for r in data["catalog"] if r["kind"] == "attempt")
    recalled = a.get(path + "/evidence", params={"evidence_id": learner["evidence_id"]}).json()[
        "data"
    ]
    assert recalled["competence_verified"] is False
    assert "assisted_completion" in learner["summary"]
    assert recalled["source_id"] == attempt["attempt_id"]
    # Revoke the same-account observation: the frozen historical summary must stop being usable.
    revoke = post(
        a,
        f"/studio/workspaces/{attempt['workspace_id']}/practices/{attempt['practice_id']}"
        f"/assessments/{submitted['assessment']['assessment_id']}/revoke",
        {
            "expected_version": submitted["assessment"]["version"],
            "expected_task_version": submitted["task"]["version"],
            "reason": "D fixture withdrawal",
        },
    )
    assert revoke.status_code == 200, revoke.text
    assert a.get(path).status_code == 409


def test_unknown_model_marker_and_source_revocation_never_call_provider(clients, monkeypatch):
    a, _ = clients
    fixture = setup_module_plan(a, monkeypatch)
    rid = tutor_run(a, fixture, 1)
    with Session(get_engine()) as db, db.begin():
        epoch = claim(db, rid, "offline-D-marker")
        commit_stage(
            db,
            rid,
            epoch,
            "offline-D-marker",
            "attempted:tutor_reply",
            "agent-study-2.3",
            {"automatic_retry": False},
        )
        run = db.get(AgentRun, rid)
        run.lease_until = db_now(db) - timedelta(seconds=1)
    with Session(get_engine()) as db, db.begin():
        reap(db)

    async def forbidden(*args, **kwargs):
        raise AssertionError("uncertain result must not be paid again")

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", forbidden)
    study.execute(rid, "offline-D-uncertain")
    state = a.get("/api/v1/runs/" + str(rid)).json()["data"]
    assert state["status"] == "failed" and state["usage"]["calls_reserved"] == 0
    with Session(get_engine()) as db:
        assert (
            db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == rid, StageArtifact.stage_key == "tutor_reply"
                )
            )
            is None
        )
    path = "/api/v1/study/tracks/" + fixture["tid"] + "/runs/" + str(rid) + "/context"
    assert a.get(path).status_code == 200
    removed = a.request(
        "DELETE",
        "/api/v1/sources/" + fixture["source"]["source_id"],
        json={"expected_version": fixture["source"]["version"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert removed.status_code == 202
    assert a.get(path).status_code == 404
