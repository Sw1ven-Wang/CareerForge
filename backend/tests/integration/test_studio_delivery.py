from uuid import UUID, uuid4

import pytest
from careerforge.application import planning_service as planning
from careerforge.application import studio_service as studio
from careerforge.application.common import fingerprint
from careerforge.application.run_service import db_now
from careerforge.application.studio_learning_service import validate_guide
from careerforge.infrastructure.db.models import (
    CareerGoalVersion,
    LearningPlan,
    LearningPlanVersion,
    LearningWorkspace,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.studio import LearningGuide
from careerforge.workflows.assessment.workflow import execute
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients
from test_planning import objective_body
from test_studio_learning_agent import SOURCE

pytestmark = pytest.mark.infrastructure


def ready(client, monkeypatch, bound=True):
    async def forbidden_model(*args, **kwargs):
        raise AssertionError("This workflow must not call a model")

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", forbidden_model)
    target = goal(client)
    body = objective_body(target)
    body["criteria"][0]["required_artifact_types"] = ["code_artifact", "execution_record"]
    body["criteria"].append(
        {
            **body["criteria"][0],
            "dimension": "explanation",
            "required_artifact_types": ["explanation_answer"],
        }
    )
    obj = post(client, "/learning-objectives", body).json()["data"]
    project = post(
        client,
        "/knowledge/imports",
        {
            "name": "delivery fixture",
            "github_url": "https://github.com/example/delivery",
            "requested_ref": "d" * 40,
            "files": {"agent.py": SOURCE},
        },
    ).json()["data"]
    hit = client.get(
        "/api/v1/knowledge/search",
        params={"q": "execute_tool", "snapshot_id": project["repository_snapshot_id"]},
    ).json()["data"]["results"][0]
    cid = post(
        client,
        "/knowledge/links",
        {"chunk_id": hit["chunk_id"], "objective_version_id": obj["objective_version_id"]},
    ).json()["data"]["citation_id"]
    source = client.get("/api/v1/citations/" + cid).json()["data"]
    workspace = post(
        client,
        "/studio/workspaces",
        {"name": "delivery fixture", "files": {"mine.py": "print('ok')\n"}},
    ).json()["data"]
    wid, report_id = workspace["workspace_id"], str(uuid4())
    with Session(get_engine()) as db, db.begin():
        row = db.get(LearningWorkspace, UUID(wid))
        guide = LearningGuide(
            status="grounded",
            summary="offline fixture",
            points=[
                {
                    "title": "dispatch",
                    "explanation": "parameter dispatch",
                    "citation_id": cid,
                    "quote": "return tool(**arguments)",
                }
            ],
            reading_order=[cid],
            practice=["迁移只读参数分发"],
            questions=[],
            limitations=["offline fixture"],
        )
        points, sources = validate_guide(db, row.owner_id, guide, [source])
        studio.append(
            row,
            {
                "id": report_id,
                "kind": "learning_report",
                "status": "completed",
                "objective_version_id": obj["objective_version_id"],
                "guide": {**guide.model_dump(), "points": points},
                "sources": sources,
                "synthetic": True,
            },
        )
        career = db.get(CareerGoalVersion, UUID(target))
        plan = LearningPlan(owner_id=row.owner_id, goal_id=career.goal_id)
        db.add(plan)
        db.flush()
        plan_version = planning.new_plan_version(
            db,
            plan,
            UUID(target),
            [UUID(obj["objective_version_id"])],
            "draft",
            {"user_constraints": {}, "deferred": [], "fixture": True},
        )
        plan_id, plan_version_id = str(plan.id), str(plan_version.id)
    created = post(
        client,
        f"/studio/workspaces/{wid}/learning-reports/{report_id}/practices",
        {"expected_revision": 1, "exercise_index": 0},
    ).json()["data"]
    pid, command_id = created["practice_id"], str(uuid4())
    with Session(get_engine()) as db, db.begin():
        row = db.get(LearningWorkspace, UUID(wid))
        command = {
            "id": command_id,
            "kind": "command",
            "status": "completed",
            "practice_id": pid,
            "argv": ["python", "mine.py"],
            "exit_code": 0,
            "stdout": "ok\n",
            "stderr": "",
            "execution_environment": {"runner_contract": "offline fixture"},
        }
        if bound:
            command.update(
                execution_input_hash=fingerprint(row.payload["files"]),
                execution_finished_at=db_now(db).isoformat(),
            )
        studio.append(row, command)
    base = f"/studio/workspaces/{wid}/practices/{pid}"
    return {
        "base": base,
        "wid": wid,
        "pid": pid,
        "obj": obj,
        "project": project,
        "body": {
            "expected_revision": 2,
            "summary": "offline frozen artifact",
            "command_ids": [command_id],
        },
        "plan": {
            "plan_id": plan_id,
            "base_plan_version_id": plan_version_id,
            "expected_plan_version": 1,
            "reason": "人工验收后的计划记录",
            "scope_acknowledged": True,
        },
    }


def assessed(client, fixture, body):
    response = post(client, fixture["base"] + "/submissions", body)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    execute(UUID(data["assessment_run_id"]), "offline-delivery-inventory")
    return client.get("/api/v1" + fixture["base"] + "/delivery").json()["data"]


def confirmation(data, decision="accept"):
    return {
        "expected_version": data["assessment"]["version"],
        "expected_task_version": data["task"]["version"],
        "decision": decision,
        "scope_acknowledged": True,
        "criterion_decisions": [
            {
                "criterion_version_id": c["criterion_version_id"],
                "verdict": "pass" if decision == "accept" else "unknown",
                "method": "human_review",
                "evidence_refs": data["submission"]["attachment_ids"],
                "reason": "offline self-review fixture",
                "self_review_record": {
                    "procedure": "inspect frozen code and log",
                    "observed": "expected fixture observation",
                    "environment": "offline fixture",
                    "code_version": data["submission"]["code_version"],
                },
            }
            for c in data["criteria"]
        ],
    }


def test_delivery_revision_accept_plan_and_revoke(clients, monkeypatch):
    a, b = clients
    f = ready(a, monkeypatch)
    assert b.get("/api/v1" + f["base"] + "/delivery").status_code == 404
    assert post(a, f["base"] + "/plan-updates", f["plan"]).status_code == 409
    first = assessed(a, f, f["body"])
    assert first["submission"]["code_commit"] is None
    assert first["submission"]["code_version"].startswith("workspace-sha256:")
    assert (
        first["submission"]["execution_records"][0]["execution_provenance"] == "careerforge_sandbox"
    )
    assert (
        first["submission"]["workspace_snapshot"]["commands"][0]["matches_submitted_code"] is True
    )
    first_url = f["base"] + "/assessments/" + first["assessment"]["assessment_id"] + "/confirm"
    incomplete = confirmation(first)
    incomplete["criterion_decisions"] = incomplete["criterion_decisions"][:1]
    assert post(a, first_url, incomplete).status_code == 422
    rejected = post(a, first_url, confirmation(first))
    assert (
        rejected.status_code == 422
        and rejected.json()["error"]["code"] == "REQUIRED_MATERIAL_MISSING"
    )
    revised = post(a, first_url, confirmation(first, "request_revision"))
    assert revised.status_code == 200, revised.text
    second_body = {
        **f["body"],
        "expected_revision": first["workspace_revision"],
        "explanation_answers": [
            {
                "question": "本人解释",
                "answer": "这是离线验收输入，不是生产用户理解声明。",
                "criterion_version_ids": [first["criteria"][1]["criterion_version_id"]],
            }
        ],
    }
    second = assessed(a, f, second_body)
    assert second["submission"]["version_no"] == 2
    assert post(a, first_url, confirmation(first)).status_code == 409
    second_url = f["base"] + "/assessments/" + second["assessment"]["assessment_id"] + "/confirm"
    accepted = post(a, second_url, confirmation(second))
    assert accepted.status_code == 200, accepted.text
    key = str(uuid4())
    updated = post(a, f["base"] + "/plan-updates", f["plan"], key)
    assert updated.status_code == 201, updated.text
    new = updated.json()["data"]
    assert (
        new["plan_id"] == f["plan"]["plan_id"]
        and new["kind"] == "draft"
        and new["active_plan_version_id"] is None
    )
    assert new["objective_version_ids"] == [f["obj"]["objective_version_id"]]
    assert new["practice_outcomes"][f["obj"]["objective_version_id"]]["effective"] is True
    assert (
        post(a, f["base"] + "/plan-updates", f["plan"], key).json()["data"]["plan_version_id"]
        == new["plan_version_id"]
    )
    with Session(get_engine()) as db:
        original = db.get(LearningPlanVersion, UUID(f["plan"]["base_plan_version_id"]))
        assert "practice_outcomes" not in original.payload and original.kind == "draft"
    current = accepted.json()["data"]
    revoke = post(
        a,
        f["base"] + "/assessments/" + current["assessment"]["assessment_id"] + "/revoke",
        {
            "expected_version": current["assessment"]["version"],
            "expected_task_version": current["task"]["version"],
            "reason": "offline fixture revocation",
        },
    )
    assert revoke.status_code == 200, revoke.text
    latest = a.get("/api/v1/plans/" + new["plan_id"]).json()["data"]
    assert latest["practice_outcomes"][f["obj"]["objective_version_id"]]["effective"] is False
    assert (
        post(
            a,
            f["base"] + "/plan-updates",
            {
                **f["plan"],
                "expected_plan_version": new["version"],
                "base_plan_version_id": new["plan_version_id"],
            },
        ).status_code
        == 409
    )


def test_unbound_logs_snapshot_immutability_and_source_revocation(clients, monkeypatch):
    a, _ = clients
    f = ready(a, monkeypatch, bound=False)
    payload = {
        **f["body"],
        "explanation_answers": [
            {
                "question": "解释",
                "answer": "offline answer",
                "criterion_version_ids": [f["obj"]["criterion_version_ids"][1]],
            }
        ],
    }
    data = assessed(a, f, payload)
    saved = data["submission"]
    assert (
        saved["execution_records"] == []
        and saved["workspace_snapshot"]["commands"][0]["matches_submitted_code"] is False
    )
    result = post(
        a,
        f["base"] + "/assessments/" + data["assessment"]["assessment_id"] + "/confirm",
        confirmation(data),
    )
    assert (
        result.status_code == 422 and result.json()["error"]["code"] == "REQUIRED_MATERIAL_MISSING"
    )
    changed = a.put(
        "/api/v1/studio/workspaces/" + f["wid"] + "/file",
        json={
            "expected_revision": data["workspace_revision"],
            "path": "mine.py",
            "content": "print('changed')\n",
        },
    )
    assert changed.status_code == 200, changed.text
    frozen = a.get("/api/v1" + f["base"] + "/delivery").json()["data"]["submission"]
    assert (
        frozen["workspace_snapshot"]["files"]["mine.py"] == "print('ok')\n"
        and frozen["code_version"] == saved["code_version"]
    )
    assert (
        post(
            a,
            f["base"] + "/submissions",
            {
                **payload,
                "expected_revision": changed.json()["data"]["revision"],
                "workspace_snapshot": {},
            },
        ).status_code
        == 422
    )
    deleted = a.request(
        "DELETE",
        "/api/v1/repositories/" + f["project"]["repository_id"],
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert deleted.status_code == 202, deleted.text
    assert a.get("/api/v1" + f["base"] + "/delivery").status_code == 404
