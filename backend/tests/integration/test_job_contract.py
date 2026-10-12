from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients

from careerforge.infrastructure.db.models import AgentRun, MarketReport
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.alignment.workflow import execute as align
from careerforge.workflows.job_contract import execute

pytestmark = pytest.mark.infrastructure


def test_offline_contract_pipeline_and_revocation(clients):
    a, b = clients
    target = goal(a)
    response = post(
        a,
        "/job-data/import",
        {"title": "Agent 实习", "raw_payload": "任职要求：熟悉 Python 或 Go；每周至少出勤 4 天。"},
    )
    assert response.status_code == 201, response.text
    job = response.json()["data"]
    sid = job["source_snapshot_id"]
    detail = a.get(f"/api/v1/job-data/source-snapshots/{sid}").json()["data"]
    assert len(detail["facts"]) == 2
    original = detail["facts"][0]["fact"]
    for row in detail["facts"]:
        fact = {**row["fact"], "review_status": "confirmed"}
        saved = post(
            a,
            f"/job-data/facts/{fact['requirement_id']}/versions",
            {
                "expected_fact_id": fact["requirement_id"],
                "correction": fact,
                "reason": "人工核对模拟岗位原文",
            },
        )
        assert saved.status_code == 201, saved.text
    frozen = a.get(f"/api/v1/job-data/artifacts/{original['requirement_id']}").json()["data"]
    assert frozen["fact"]["review_status"] != "confirmed"
    response = post(
        a, "/market-research-runs", {"goal_version_id": target, "source_snapshot_ids": [sid]}
    )
    assert response.status_code == 202, response.text
    run = UUID(response.json()["data"]["run_id"])
    execute(run, "contract-test")
    with Session(get_engine()) as db:
        report = db.scalar(select(MarketReport).where(MarketReport.run_id == run))
        state = db.get(AgentRun, run)
        assert report is not None, (state.status, state.input_manifest)
        rid = str(report.id)
    report = a.get(f"/api/v1/market-reports/{rid}").json()["data"]
    demand = report["job_contract"]["report"]
    assert demand["denominator"] == 1
    for item in demand["demands"]:
        if item["concept_id"] in ["technology.python", "technology.go"]:
            assert item["required_solo"]["count"] == 0
            assert item["required_alternative"]["count"] == 1
    assert len(demand["eligibility_requirement_ids"]) == 1
    response = post(
        a,
        "/alignment-runs",
        {"goal_version_id": target, "market_report_id": rid, "personal_evidence_version_ids": []},
    )
    assert response.status_code == 202, response.text
    align_run = UUID(response.json()["data"]["run_id"])
    align(align_run, "contract-align-test")
    with Session(get_engine()) as db:
        from careerforge.infrastructure.db.models import AlignmentReport

        alignment = db.scalar(select(AlignmentReport).where(AlignmentReport.run_id == align_run))
        assert alignment is not None, db.get(AgentRun, align_run).status
        aid = str(alignment.id)
    alignment = a.get(f"/api/v1/alignment-reports/{aid}").json()["data"]
    assert len(alignment["objective_version_ids"]) == 1
    plan = a.get(
        f"/api/v1/plans/{alignment['plan_id']}/versions/{alignment['plan_version_id']}"
    ).json()["data"]
    assert len(plan["objectives"]) == 1
    assert "python" in plan["objectives"][0]["title"]
    assert plan["eligibility_checks"][0]["status"] == "unknown"
    context = plan["planning_context_id"]
    assert a.get(f"/api/v1/job-data/artifacts/{context}").status_code == 200
    assert b.get(f"/api/v1/job-data/artifacts/{context}").status_code == 404
    deleted = a.request(
        "DELETE",
        f"/api/v1/sources/{job['source_id']}",
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert deleted.status_code == 202, deleted.text
    assert a.get(f"/api/v1/job-data/artifacts/{context}").status_code == 404
    assert a.get(f"/api/v1/market-reports/{rid}").status_code == 404
    assert a.get(f"/api/v1/alignment-reports/{aid}").status_code == 404


def test_manual_identities_duplicate_decision_and_stale_report(clients):
    a, _ = clients
    target = goal(a)
    sources = []
    jobs = []
    for _ in range(2):
        response = post(
            a,
            "/job-data/import",
            {"title": "Agent 实习", "raw_payload": "任职要求：熟悉 Python 或 Go。"},
        )
        assert response.status_code == 201, response.text
        job = response.json()["data"]
        jobs.append(job)
        sources.append(job["source_snapshot_id"])
        detail = a.get(f"/api/v1/job-data/source-snapshots/{job['source_snapshot_id']}").json()[
            "data"
        ]
        fact = {**detail["facts"][0]["fact"], "review_status": "confirmed"}
        response = post(
            a,
            f"/job-data/facts/{fact['requirement_id']}/versions",
            {
                "expected_fact_id": fact["requirement_id"],
                "correction": fact,
                "reason": "核对模拟原文",
            },
        )
        assert response.status_code == 201, response.text
    assert jobs[0]["snapshot"]["posting_id"] != jobs[1]["snapshot"]["posting_id"]

    def report(decisions=None):
        response = post(
            a,
            "/market-research-runs",
            {
                "goal_version_id": target,
                "source_snapshot_ids": sources,
                "dedup_decision_ids": decisions or [],
            },
        )
        assert response.status_code == 202, response.text
        run = UUID(response.json()["data"]["run_id"])
        execute(run, "contract-dedup-test")
        with Session(get_engine()) as db:
            row = db.scalar(select(MarketReport).where(MarketReport.run_id == run))
            assert row is not None
            return a.get(f"/api/v1/market-reports/{row.id}").json()["data"]

    pending = report()
    assert pending["job_contract"]["report"]["denominator"] == 0
    candidate = a.get(f"/api/v1/duplicate-candidates?goal_version_id={target}").json()["data"][0]
    decision = post(
        a,
        f"/duplicate-candidates/{candidate['candidate_id']}/decisions",
        {
            "expected_version": candidate["version"],
            "decision": "distinct",
            "reason": "模拟两个独立招聘岗位",
        },
    )
    assert decision.status_code == 201, decision.text
    fixed = report([decision.json()["data"]["decision_id"]])
    assert fixed["job_contract"]["report"]["denominator"] == 2
    context = post(
        a,
        "/job-data/planning-contexts",
        {
            "goal_version_id": target,
            "demand_report_id": fixed["demand_report_id"],
            "user_constraints": {"preferred_languages": ["Go"]},
        },
    )
    assert context.status_code == 201, context.text
    cid = context.json()["data"]["contract_artifact_id"]
    started = post(a, f"/job-data/planning-contexts/{cid}/alignment-runs", {})
    assert started.status_code == 202, started.text
    align(UUID(started.json()["data"]["run_id"]), "contract-go-preference")
    detail = a.get(f"/api/v1/job-data/source-snapshots/{sources[0]}").json()["data"]
    old = detail["facts"][0]["fact"]
    revised = post(
        a,
        f"/job-data/facts/{old['requirement_id']}/versions",
        {
            "expected_fact_id": old["requirement_id"],
            "correction": {**old, "importance": "preferred"},
            "reason": "更正重要性",
        },
    )
    assert revised.status_code == 201, revised.text
    assert (
        a.get(f"/api/v1/market-reports/{fixed['market_report_id']}").json()["data"]["job_contract"][
            "report"
        ]["denominator"]
        == 2
    )
    stale = post(
        a,
        "/job-data/planning-contexts",
        {"goal_version_id": target, "demand_report_id": fixed["demand_report_id"]},
    )
    assert stale.status_code == 409, stale.text
