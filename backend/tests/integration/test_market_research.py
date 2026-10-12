import copy
from uuid import UUID, uuid4

import pytest
from careerforge.infrastructure.db.models import (
    MarketReport,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.market_research.workflow import execute
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure


def post(client, path, body, key=None):
    return client.post(
        "/api/v1" + path, json=body, headers={"Idempotency-Key": key or str(uuid4())}
    )


def source(client, text, title="Agent 开发", kind="job_posting", url=None):
    response = post(
        client,
        "/sources/import",
        {
            "source_type": kind,
            "title": title,
            "source_url": url or f"https://example.test/{uuid4()}",
            "content": text,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def goal(client, regions=None):
    return post(
        client,
        "/career-goals",
        {"role_direction": "Agent开发", "job_type": "internship", "regions": regions or []},
    ).json()["data"]["goal_version_id"]


def run_report(client, target, sources, decisions=None):
    response = post(
        client,
        "/market-research-runs",
        {
            "goal_version_id": target,
            "source_snapshot_ids": [s["source_snapshot_id"] for s in sources],
            "dedup_decision_ids": decisions or [],
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["data"]["run_id"])
    execute(run_id, f"test-{uuid4()}")
    with Session(get_engine()) as db:
        report = db.scalar(select(MarketReport).where(MarketReport.run_id == run_id))
        assert report is not None
        report_id = report.id
    response = client.get(f"/api/v1/market-reports/{report_id}")
    assert response.status_code == 200, response.text
    return run_id, response.json()["data"]


A = (
    "实习 北京 上海\n工作职责：\n- Agent 开发\n任职要求：\n"
    "- Python 编程\n- Python 测试\n- RAG 经验优先\n"
)
C = "实习 北京\n工作职责：\n- Agent 开发\n任职要求：\n- Java 编程\n"
D = "实习 深圳\n任职要求：\n- Python 编程\n"


def test_fixed_counts_citations_and_non_jd_exclusion(clients):
    a, b = clients
    target = goal(a)
    samples = [
        source(a, A),
        source(a, A),
        source(a, C),
        source(a, D, "另一个岗位"),
        source(a, "面经 Python", kind="interview_experience"),
        source(a, "joblearn 个人材料", kind="user_note"),
    ]
    run_id, report = run_report(a, target, samples)
    counts = report["counts"]
    assert counts == {
        "raw": 6,
        "readable": 6,
        "job_postings": 4,
        "extraction_completed": 4,
        "deduplicated": 3,
        "interview_experience": 1,
        "other_material": 1,
        "unresolved_duplicates": 2,
    }
    stats = {r["skill_concept_id"]: r for r in report["skill_statistics"]}
    assert (stats["python"]["numerator"], stats["python"]["denominator"]) == (2, 3)
    assert (stats["java"]["numerator"], stats["rag"]["numerator"]) == (1, 1)
    assert len(report["regional_posting_groups"]["北京"]) == 2
    assert len(report["regional_posting_groups"]["上海"]) == 1
    assert all(p["published_at"] is None for p in report["posting_summaries"])
    assert all(p["experience"] is None for p in report["posting_summaries"])
    for req in report["requirements"]:
        citation = a.get("/api/v1/citations/" + req["citation"]["citation_id"])
        assert citation.status_code == 200 and citation.json()["data"]["quote"] == req["text"]
    assert b.get("/api/v1/market-reports/" + report["market_report_id"]).status_code == 404
    assert b.get("/api/v1/market-reports").json()["data"] == []
    # Replaying a completed workflow does not duplicate reports.
    execute(run_id, "duplicate")
    with Session(get_engine()) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(MarketReport).where(MarketReport.run_id == run_id)
            )
            == 1
        )


def test_scope_and_canonical_url_dedup(clients):
    a, _ = clients
    target = goal(a, ["北京市"])
    samples = [
        source(a, A, url="https://example.test/job?utm_source=a"),
        source(a, A + "\n附注", url="https://example.test/job?utm_source=b"),
        source(a, D),
    ]
    _, report = run_report(a, target, samples)
    assert report["counts"]["deduplicated"] == 2
    assert len(report["eligible_posting_version_ids"]) == 1
    assert len(report["scope_exclusions"]) == 1


def test_partial_failure_and_out_of_bounds_quote(clients, monkeypatch):
    import careerforge.workflows.market_research.workflow as workflow

    a, _ = clients
    target = goal(a)
    original = workflow.offline_extract

    def invalid(text, title):
        value = original(text, title)
        if "Java" in text:
            value["requirements"][0]["char_end"] = len(text) + 1
        return value

    monkeypatch.setattr(workflow, "offline_extract", invalid)
    run_id, report = run_report(a, target, [source(a, A), source(a, C)])
    assert report["counts"]["extraction_completed"] == 1
    assert report["unresolved_sources"][0]["reason"] == "INVALID_CITATION"
    assert all(s["denominator"] == 1 for s in report["skill_statistics"])
    assert a.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "partially_succeeded"


def test_corrections_preserve_frozen_report_and_validate_span(clients):
    a, _ = clients
    target = goal(a)
    sample = source(a, A)
    _, report = run_report(a, target, [sample])
    old = copy.deepcopy(report)
    req = next(r for r in report["requirements"] if r["normalized_skill"] == "python")
    fields = [
        "requirement_type",
        "origin",
        "normalized_skill",
        "skill_kind",
        "text",
        "char_start",
        "char_end",
        "logical_group",
    ]
    body = {k: req[k] for k in fields}
    body.update(expected_version=1, reason="人工调整类别", requirement_type="preferred")
    key = str(uuid4())
    corrected = post(a, "/requirements/" + req["requirement_id"] + "/corrections", body, key)
    assert corrected.status_code == 201, corrected.text
    assert (
        post(a, "/requirements/" + req["requirement_id"] + "/corrections", body, key).json()["data"]
        == corrected.json()["data"]
    )
    assert (
        post(a, "/requirements/" + req["requirement_id"] + "/corrections", body).status_code == 409
    )
    assert a.get("/api/v1/market-reports/" + report["market_report_id"]).json()["data"] == old
    _, updated = run_report(a, target, [sample])
    assert updated["config_fingerprint"] != old["config_fingerprint"]
    assert corrected.json()["data"]["requirement_id"] in [
        r["requirement_id"] for r in updated["requirements"]
    ]


def test_duplicate_decisions_frozen_and_transitive_conflicts(clients):
    a, _ = clients
    target = goal(a)
    samples = [source(a, A), source(a, C), source(a, D)]
    _, old = run_report(a, target, samples)
    candidates = a.get("/api/v1/duplicate-candidates", params={"goal_version_id": target}).json()[
        "data"
    ]
    assert len(candidates) == 3
    accepted = []
    for candidate, choice in zip(candidates, ["duplicate", "duplicate", "distinct"], strict=True):
        response = post(
            a,
            "/duplicate-candidates/" + candidate["candidate_id"] + "/decisions",
            {
                "expected_version": candidate["version"],
                "decision": choice,
                "reason": "合成测试判定",
            },
        )
        if choice == "distinct":
            assert response.status_code == 409
        else:
            assert response.status_code == 201, response.text
            accepted.append(response.json()["data"]["decision_id"])
    assert a.get("/api/v1/market-reports/" + old["market_report_id"]).json()["data"] == old
    _, new = run_report(a, target, samples, accepted)
    assert new["counts"]["deduplicated"] == 1
    assert new["counts"]["unresolved_duplicates"] == 0


def test_reject_non_jd_and_hidden_controls(clients):
    a, _ = clients
    target = goal(a)
    note = source(a, "个人 README", kind="user_note")
    body = {"goal_version_id": target, "source_snapshot_ids": [note["source_snapshot_id"]]}
    assert post(a, "/market-research-runs", body).status_code == 422
    jd = source(a, A)
    body["source_snapshot_ids"] = [jd["source_snapshot_id"]]
    assert post(a, "/market-research-runs", {**body, "worker_id": "forged"}).status_code == 422
    assert (
        post(a, "/market-research-runs", {**body, "retrieval_mode": "approved_sources"}).status_code
        == 422
    )


def test_revoked_input_hides_report_citations_and_idempotent_replay(clients):
    a, b = clients
    target = goal(a)
    sample = source(a, A)
    body = {"goal_version_id": target, "source_snapshot_ids": [sample["source_snapshot_id"]]}
    assert post(b, "/market-research-runs", body).status_code == 404
    foreign = source(b, C)
    assert (
        post(
            a,
            "/market-research-runs",
            {**body, "source_snapshot_ids": [foreign["source_snapshot_id"]]},
        ).status_code
        == 404
    )
    key = str(uuid4())
    created = post(a, "/market-research-runs", body, key)
    run_id = UUID(created.json()["data"]["run_id"])
    execute(run_id, "revocation-test")
    with Session(get_engine()) as db:
        report = db.scalar(select(MarketReport).where(MarketReport.run_id == run_id))
        report_id, citation_id = (
            report.id,
            report.payload["requirements"][0]["citation"]["citation_id"],
        )
    response = a.request(
        "DELETE",
        "/api/v1/sources/" + sample["source_id"],
        json={"expected_version": sample["version"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202
    assert a.get(f"/api/v1/market-reports/{report_id}").status_code == 404
    assert a.get(f"/api/v1/citations/{citation_id}").status_code == 404
    assert post(a, "/market-research-runs", body, key).status_code == 404


def test_no_eligible_posting_returns_diagnostic_failure_not_false_success(clients):
    a, _ = clients
    target = goal(a)
    sample = source(a, "北京\n任职要求：\n- Python 开发")
    run_id, report = run_report(a, target, [sample])
    assert report["counts"]["extraction_completed"] == 1
    assert report["eligible_posting_version_ids"] == []
    assert report["skill_statistics"][0]["denominator"] == 0
    assert report["skill_statistics"][0]["ratio"] is None
    assert a.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "failed"
