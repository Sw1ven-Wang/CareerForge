from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from careerforge.infrastructure.db.models import (
    AlignmentReport,
    LearningPlanVersion,
    PlanMembership,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.alignment.workflow import execute
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import A, goal, post, run_report, source
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure


def objective_body(target, dependencies=None):
    return {
        "goal_version_id": target,
        "title": "一个明确增量",
        "target_behavior": "重复通知只写入一次结果",
        "scope": "固定输入、一个用户",
        "scope_confirmed": True,
        "deliverables": ["版本化代码", "日志"],
        "effort_hours": {"min": 2, "max": 4, "is_estimate": True},
        "prerequisite_objective_ids": dependencies or [],
        "criteria": [
            {
                "dimension": "artifact",
                "scenario": "重复投递",
                "expected_behavior": "结果行只有一份",
                "pass_rule": "记录含版本及两次投递后的查询结果",
                "fail_rule": "生成两行",
                "unknown_rule": "没有实际查询记录",
                "critical": True,
                "allowed_methods": ["user_reported", "human_review"],
                "required_artifact_types": ["execution_record"],
            }
        ],
    }


def create_alignment(client, target, report, evidence=None):
    response = post(
        client,
        "/alignment-runs",
        {
            "goal_version_id": target,
            "market_report_id": report["market_report_id"],
            "personal_evidence_version_ids": evidence or [],
        },
    )
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["data"]["run_id"])
    execute(identifier, "test-alignment")
    with Session(get_engine()) as db:
        row = db.scalar(select(AlignmentReport).where(AlignmentReport.run_id == identifier))
        assert row is not None
        return client.get(f"/api/v1/alignment-reports/{row.id}").json()["data"]


def test_unknown_alignment_and_subset_adoption_keep_draft_immutable(clients):
    a, b = clients
    target = goal(a)
    _, report = run_report(a, target, [source(a, A)])
    evidence = post(
        a,
        "/evidence",
        {
            "kind": "readme",
            "title": "joblearn",
            "description": "自述已做 RAG",
            "claimed_skills": ["rag"],
        },
    )
    ev = evidence.json()["data"]
    assert ev["verification_scope"] == "self_reported"
    alignment = create_alignment(a, target, report, [ev["evidence_version_id"]])
    assert all(r["category"] == "unknown" for r in alignment["alignments"])
    identifier, version_id = alignment["plan_id"], alignment["plan_version_id"]
    old = a.get(f"/api/v1/plans/{identifier}/versions/{version_id}").json()["data"]
    selected = old["objective_version_ids"][:1]
    body = {
        "expected_version": old["version"],
        "plan_version_id": version_id,
        "expected_goal_version_id": target,
        "selected_objective_version_ids": selected,
    }
    assert post(b, f"/plans/{identifier}/adopt", body).status_code == 404
    key = str(uuid4())
    result = post(a, f"/plans/{identifier}/adopt", body, key)
    assert result.status_code == 201, result.text
    data = result.json()["data"]
    assert post(a, f"/plans/{identifier}/adopt", body, key).json()["data"] == data
    with Session(get_engine()) as db:
        original = db.get(LearningPlanVersion, UUID(version_id))
        assert original.kind == "draft"
        original_ids = list(
            db.scalars(
                select(PlanMembership.objective_version_id).where(
                    PlanMembership.plan_version_id == original.id
                )
            )
        )
        adopted = db.get(LearningPlanVersion, UUID(data["active_plan_version_id"]))
        assert adopted.kind == "adopted" and len(original_ids) == len(old["objective_version_ids"])
    new = a.get(f"/api/v1/plans/{identifier}/versions/{data['active_plan_version_id']}").json()[
        "data"
    ]
    assert new["objective_version_ids"] == selected
    assert (
        a.get("/api/v1/career-goals").json()["data"][0]["active_plan_version_id"]
        == data["active_plan_version_id"]
    )
    assert post(a, f"/plans/{identifier}/adopt", body).status_code == 409


def test_manual_objectives_cycle_complete_criteria_and_new_versions(clients):
    a, b = clients
    target = goal(a)
    body = objective_body(target)
    first = post(a, "/learning-objectives", body).json()["data"]
    assert first["origin"] == "user_goal" and len(first["criteria"]) == 1
    assert post(a, "/learning-objectives", {**body, "criteria": []}).status_code == 422
    assert post(a, "/learning-objectives", {**body, "origin": "trusted"}).status_code == 422
    second = post(
        a, "/learning-objectives", objective_body(target, [first["objective_id"]])
    ).json()["data"]
    updated = {
        **body,
        "expected_version": 1,
        "change_reason": "变更先修",
        "prerequisite_objective_ids": [second["objective_id"]],
    }
    assert (
        post(a, f"/learning-objectives/{first['objective_id']}/versions", updated).status_code
        == 422
    )
    assert b.get(f"/api/v1/learning-objectives/{first['objective_id']}").status_code == 404
    new = post(
        a,
        f"/learning-objectives/{first['objective_id']}/versions",
        {**updated, "prerequisite_objective_ids": []},
    )
    assert new.status_code == 201, new.text
    assert new.json()["data"]["criterion_version_ids"] != first["criterion_version_ids"]
    fixed = a.get(
        f"/api/v1/learning-objectives/{first['objective_id']}/versions/{first['objective_version_id']}"
    )
    assert fixed.json()["data"]["criterion_version_ids"] == first["criterion_version_ids"]


def test_plan_requires_prerequisites_and_preserves_effort_budget(clients):
    a, _ = clients
    target = goal(a)
    _, report = run_report(a, target, [source(a, A)])
    alignment = create_alignment(a, target, report)
    first = post(a, "/learning-objectives", objective_body(target)).json()["data"]
    second = post(
        a, "/learning-objectives", objective_body(target, [first["objective_id"]])
    ).json()["data"]
    pid, pvid = alignment["plan_id"], alignment["plan_version_id"]
    old = a.get(f"/api/v1/plans/{pid}").json()["data"]
    body = {
        "expected_version": old["version"],
        "base_plan_version_id": pvid,
        "adjustment_reason": "核对依赖",
        "objective_version_ids": [second["objective_version_id"]],
    }
    assert post(a, f"/plans/{pid}/versions", body).status_code == 422
    body["objective_version_ids"] = [first["objective_version_id"], second["objective_version_id"]]
    assert (
        post(
            a, f"/plans/{pid}/versions", {**body, "user_constraints": {"available_hours": 7}}
        ).status_code
        == 422
    )
    draft = post(a, f"/plans/{pid}/versions", {**body, "user_constraints": {"available_hours": 8}})
    assert draft.status_code == 201, draft.text
    d = draft.json()["data"]
    omitted = {
        "expected_version": d["version"],
        "plan_version_id": d["plan_version_id"],
        "expected_goal_version_id": target,
        "selected_objective_version_ids": [second["objective_version_id"]],
    }
    assert post(a, f"/plans/{pid}/adopt", omitted).status_code == 422
    assert (
        post(
            a,
            f"/plans/{pid}/adopt",
            {**omitted, "selected_objective_version_ids": body["objective_version_ids"]},
        ).status_code
        == 201
    )


def test_evidence_root_and_report_revocation(clients):
    a, b = clients
    target = goal(a)
    _, report = run_report(a, target, [source(a, A)])
    upload = a.post(
        "/api/v1/attachments",
        files={"file": ("note.md", b"static evidence")},
        data={"purpose": "personal_evidence"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    aid = upload.json()["data"]["attachment_id"]
    body = {
        "kind": "readme",
        "title": "joblearn",
        "description": "只读材料",
        "attachment_ids": [aid],
    }
    evidence = post(a, "/evidence", body).json()["data"]
    assert post(a, "/evidence", body).status_code == 409
    assert post(b, "/evidence", body).status_code == 404
    alignment = create_alignment(a, target, report, [evidence["evidence_version_id"]])
    assert (
        a.request(
            "DELETE",
            f"/api/v1/evidence/{evidence['evidence_id']}",
            json={"expected_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 202
    )
    assert a.get(f"/api/v1/attachments/{aid}/content").status_code == 404
    assert a.get(f"/api/v1/alignment-reports/{alignment['alignment_report_id']}").status_code == 404


def test_concurrent_adoptions_conflict_without_overwriting(clients):
    a, _ = clients
    target = goal(a)
    _, report = run_report(a, target, [source(a, A)])
    alignment = create_alignment(a, target, report)
    pid, pvid = alignment["plan_id"], alignment["plan_version_id"]
    plan = a.get(f"/api/v1/plans/{pid}").json()["data"]
    body = {
        "expected_version": plan["version"],
        "plan_version_id": pvid,
        "expected_goal_version_id": target,
        "selected_objective_version_ids": plan["objective_version_ids"][:1],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: post(a, f"/plans/{pid}/adopt", body).status_code, range(2))
        )
    assert sorted(results) == [201, 409]
