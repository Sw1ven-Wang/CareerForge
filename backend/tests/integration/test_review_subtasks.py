"""Root/child races and accounting; no remote provider or project command execution."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from careerforge.application import delegation_service as delegation
from careerforge.application import review_batch_service as tasks
from careerforge.application import study_service as study_service
from careerforge.application.model_call_service import settle_receipt
from careerforge.application.run_service import (
    cancel,
    claim,
    commit_stage,
    db_now,
    leased,
    reap,
    reserve_call,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt, RunAllocation
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyArtifact, StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.workflows import review_batch, study
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients
from test_studio_learning_agent import model_result
from test_study_practice import CODE, setup_module_plan

pytestmark = pytest.mark.infrastructure


def fixture_candidates(a, monkeypatch):
    fixture = setup_module_plan(a, monkeypatch)
    first = a.get("/api/v1/citations/" + fixture["cid"]).json()["data"]["repository_snapshot_id"]
    second = post(
        a,
        "/knowledge/imports",
        {
            "name": "second fixed fixture",
            "github_url": "https://github.com/example/d-child-two",
            "requested_ref": "d" * 40,
            "files": {"tool.py": CODE, "LICENSE": "MIT License"},
        },
    ).json()["data"]["repository_snapshot_id"]
    for sid in (first, second):
        response = post(
            a,
            "/study/tracks/" + fixture["tid"] + "/candidates",
            {
                "repository_snapshot_id": sid,
                "expected_revision": a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"][
                    "revision"
                ],
            },
        )
        assert response.status_code == 201, response.text
    fixture["candidate_ids"] = [c["id"] for c in response.json()["data"]["candidates"]]
    return fixture


def start_batch(a, fixture, per=1, count=2):
    track = a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    body = {
        "expected_revision": track["revision"],
        "candidate_ids": fixture["candidate_ids"][:count],
        "per_child_call_limit": per,
        "call_budget": per * count,
        "max_output_tokens": 2048,
    }
    key = str(uuid4())
    result = post(a, "/study/tracks/" + fixture["tid"] + "/review-batches", body, key)
    assert result.status_code == 202, result.text
    assert (
        post(a, "/study/tracks/" + fixture["tid"] + "/review-batches", body, key).json()["data"][
            "run_id"
        ]
        == result.json()["data"]["run_id"]
    )
    return UUID(result.json()["data"]["run_id"])


def family(a, rid):
    result = a.get("/api/v1/runs/" + str(rid))
    assert result.status_code == 200, result.text
    return result.json()["data"]


def children(a, rid):
    return [UUID(c["run_id"]) for c in family(a, rid)["family"]["children"]]


def fake_provider(monkeypatch):
    calls = []

    async def complete(self, request):
        value = json.JSONDecoder().raw_decode(request.prompt.split("INPUT_UNTRUSTED_JSON\n")[1])[0]
        calls.append(request)
        assert "reading" in value and "history" not in value
        return model_result(
            structured={
                "verdict": "strong",
                "summary": "offline fixed source",
                "technology_stack": ["Python"],
                "quality_notes": ["synthetic static reading only"],
                "matches": [
                    {
                        "requirement_key": "python",
                        "path": "tool.py",
                        "start_line": 1,
                        "end_line": 2,
                        "quote": "return tool(**arguments)",
                        "explanation": "synthetic parameter forwarding",
                    }
                ],
                "limitations": ["not executed"],
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    return calls


def published(rid):
    with Session(get_engine()) as db:
        return list(
            db.scalars(
                select(StudyArtifact.id).where(
                    StudyArtifact.run_id == rid, StudyArtifact.kind == "review"
                )
            )
        )


def test_parallel_children_stage_then_parent_publishes_once(clients, monkeypatch):
    a, b = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start_batch(a, fixture)
    review_batch.execute(rid, "parent-first")
    state = family(a, rid)
    assert state["status"] == "waiting_children" and state["family"]["held_calls"] == 2
    ids = children(a, rid)
    assert len(ids) == 2 and not published(rid)
    assert b.get("/api/v1/runs/" + str(ids[0])).status_code == 404
    calls = fake_provider(monkeypatch)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda cid: study.execute(cid, "child-" + str(cid)), ids))
    assert len(calls) == 2 and not published(rid)
    state = family(a, rid)
    assert state["status"] == "queued" and state["family"]["consumed_calls"] == 2
    assert state["family"]["held_calls"] == 0
    review_batch.execute(rid, "parent-join")
    review_batch.execute(rid, "duplicate-parent")
    assert family(a, rid)["status"] == "succeeded" and len(published(rid)) == 2 and len(calls) == 2
    with Session(get_engine()) as db:
        assert all(db.get(AgentRun, cid).parent_run_id == rid for cid in ids)
        assert all(
            r.settled_at
            for r in db.scalars(select(RunAllocation).where(RunAllocation.parent_run_id == rid))
        )


def test_reservation_idempotency_overallocation_and_request_token_cap(clients, monkeypatch):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start_batch(a, fixture, count=1)
    with Session(get_engine()) as db, db.begin():
        epoch = claim(db, rid, "budget-parent")
        parent = leased(db, rid, epoch, "budget-parent")
        frozen = parent.input_manifest["candidates"][0]
        manifest = tasks.child_manifest(parent, frozen)
        child = delegation.create_child(db, parent, frozen["candidate_id"], manifest, 1, 2048)
        cid = child.id
        assert (
            delegation.create_child(db, parent, frozen["candidate_id"], manifest, 1, 2048).id == cid
        )
    with (
        Session(get_engine()) as db,
        db.begin(),
        pytest.raises(BusinessError, match="ROOT_BUDGET_UNAVAILABLE"),
    ):
        parent = leased(db, rid, epoch, "budget-parent")
        delegation.create_child(db, parent, "extra-task", manifest, 1, 2048)
    with Session(get_engine()) as db, db.begin():
        ce = claim(db, cid, "bounded-child")
    with (
        Session(get_engine()) as db,
        db.begin(),
        pytest.raises(BusinessError, match="CHILD_TOKEN_LIMIT"),
    ):
        reserve_call(db, cid, ce, "bounded-child", {"max_output_tokens": 4096})
    with Session(get_engine()) as db, db.begin():
        assert reserve_call(db, cid, ce, "bounded-child", {"max_output_tokens": 2048})
        assert reserve_call(db, cid, ce, "bounded-child", {"max_output_tokens": 2048}) is None
    state = family(a, rid)["family"]
    assert state["consumed_calls"] == 1 and state["held_calls"] == 0
    with Session(get_engine()) as db, db.begin():
        parent = delegation.lock_run(db, rid)
        cancel(db, parent.owner_id, rid, parent.version)


def test_cancel_parent_after_child_completed_prevents_any_domain_publication(clients, monkeypatch):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start_batch(a, fixture)
    review_batch.execute(rid, "dispatch-cancel")
    ids = children(a, rid)
    calls = fake_provider(monkeypatch)
    study.execute(ids[0], "first-finished")
    current = family(a, rid)
    result = a.post(
        "/api/v1/runs/" + str(rid) + "/cancel", json={"expected_version": current["version"]}
    )
    assert result.status_code == 200, result.text
    study.execute(ids[1], "late-queued")
    review_batch.execute(rid, "late-join")
    state = family(a, rid)
    assert state["status"] == "cancelled" and state["family"]["held_calls"] == 0
    assert state["family"]["consumed_calls"] == 1 and len(calls) == 1 and not published(rid)
    assert sorted(c["status"] for c in state["family"]["children"]) == ["cancelled", "succeeded"]
    assert sum(c["released_calls"] for c in state["family"]["children"]) == 1


def test_cancel_running_child_keeps_unknown_charge_and_accepts_only_late_accounting(
    clients, monkeypatch
):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start_batch(a, fixture, per=2)
    review_batch.execute(rid, "dispatch-running")
    cid = children(a, rid)[0]
    with Session(get_engine()) as db, db.begin():
        ce = claim(db, cid, "inflight")
        receipt_id = reserve_call(db, cid, ce, "inflight", {"max_output_tokens": 2048})
    current = family(a, rid)
    result = a.post(
        "/api/v1/runs/" + str(rid) + "/cancel", json={"expected_version": current["version"]}
    )
    assert result.status_code == 200, result.text
    state = family(a, rid)["family"]
    assert state["consumed_calls"] == 1 and state["held_calls"] == 0
    assert sum(c["released_calls"] for c in state["children"]) == 3
    with Session(get_engine()) as db:
        assert db.get(ModelCallReceipt, receipt_id).status == "unknown"
    settle_receipt(receipt_id, {"status": "known", "input_tokens": 10, "output_tokens": 20})
    with Session(get_engine()) as db, db.begin(), pytest.raises(LeaseLost):
        commit_stage(db, cid, ce, "inflight", "late-result", delegation.CHILD_VERSION, {})
    assert not published(rid) and family(a, rid)["family"]["consumed_calls"] == 1


def test_deadline_releases_queued_reservations_and_changed_requirements_block_join(
    clients, monkeypatch
):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start_batch(a, fixture)
    review_batch.execute(rid, "deadline-dispatch")
    with Session(get_engine()) as db, db.begin():
        delegation.lock_run(db, rid).deadline_at = db_now(db) - timedelta(seconds=1)
    with Session(get_engine()) as db, db.begin():
        reap(db)
    assert family(a, rid)["status"] == "failed" and family(a, rid)["family"]["held_calls"] == 0
    rid = start_batch(a, fixture)
    review_batch.execute(rid, "second-dispatch")
    calls = fake_provider(monkeypatch)
    for cid in children(a, rid):
        study.execute(cid, "source-change-child")
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        old = study_service.current_requirements(db, track)
        new = study_service.put(db, track, "requirements", dict(old.payload))
        track.payload = {**track.payload, "requirements_id": str(new.id)}
    review_batch.execute(rid, "stale-parent-join")
    assert family(a, rid)["status"] == "failed" and not published(rid) and len(calls) == 2
