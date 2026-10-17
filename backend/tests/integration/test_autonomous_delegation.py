"""Synthetic offline cases for model-led roles, scope guards, cancellation and recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from careerforge.application import assistance_service as assistance
from careerforge.application import autonomous_service as service
from careerforge.application import delegation_service as delegation
from careerforge.application import study_service as study
from careerforge.application.run_service import cancel, claim, db_now, leased, reap, reserve_call
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt, RunEvent, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyArtifact, StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.delegation import DelegateArgs
from careerforge.schemas.model_io import ToolCall
from careerforge.workflows import autonomous
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients
from test_review_subtasks import children, family, fixture_candidates
from test_studio_learning_agent import model_result

pytestmark = pytest.mark.infrastructure


def start(a, fixture, budget=4, roles=None):
    track = a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    response = post(
        a,
        "/study/tracks/" + fixture["tid"] + "/delegations",
        {
            "expected_revision": track["revision"],
            "objective": "synthetic source investigation",
            "requirement_keys": ["python"],
            "candidate_ids": fixture["candidate_ids"][:1],
            "allowed_roles": roles or ["requirement_analysis", "code_investigation"],
            "call_budget": budget,
            "parent_call_limit": 2,
            "max_output_tokens": 2048,
        },
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["data"]["run_id"])


def proposal(candidate=None, role="code_investigation", calls=1, key="code"):
    return {
        "task_key": key,
        "role": role,
        "objective": "Check synthetic dispatch only",
        "requirement_keys": ["python"],
        "candidate_id": str(candidate) if candidate else None,
        "call_limit": calls,
    }


def provider(monkeypatch, proposals=None):
    calls = []

    async def complete(self, request):
        data = json.JSONDecoder().raw_decode(request.prompt.split("INPUT_UNTRUSTED_JSON\n")[1])[0]
        calls.append(data)
        if request.tools:
            assert [t["function"]["name"] for t in request.tools] == ["delegate_task"]
            assert (
                request.tools[0]["function"]["parameters"]["properties"]["call_limit"]["maximum"]
                == data["delegation_capacity"]["max_child_calls_after_this_decision"]
            )
            choices = (
                proposals(data)
                if proposals
                else [
                    proposal(data["allowed_projects"][0]["candidate_id"]),
                    proposal(role="requirement_analysis", key="requirements"),
                ]
            )
            return model_result(
                [
                    ToolCall(id=str(i), name="delegate_task", arguments=p)
                    for i, p in enumerate(choices)
                ]
            )
        if "reading" in data:
            contract = data["context_manifest"]["contract"]
            assert "delegate_task" not in contract["allowed_tools"] and contract["max_depth"] == 0
            assert "handoffs" not in data and "allowed_projects" not in data
            reading = next((r for r in data["reading"] if r["kind"] == "code"), data["reading"][0])
            quote = "return tool(**arguments)" if reading["kind"] == "code" else reading["content"]
            return model_result(
                structured={
                    "summary": "synthetic bounded task",
                    "findings": [
                        {
                            "statement": "synthetic finding",
                            "evidence": [{"ref_id": reading["ref_id"], "quote": quote}],
                        }
                    ],
                    "unresolved": ["semantic support unverified"],
                    "counterevidence": [],
                    "stop_reason": "evidence_sufficient",
                }
            )
        refs = [e["evidence_id"] for h in data["handoffs"] for e in h["evidence"]]
        return model_result(
            structured={
                "summary": "synthetic parent summary",
                "claims": [{"statement": "synthetic aggregation", "evidence_ids": refs}]
                if refs
                else [],
                "unresolved": [],
                "counterevidence": [],
                "stop_reason": "completed" if refs else "insufficient_evidence",
            }
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    return calls


def published(rid):
    with Session(get_engine()) as db:
        return list(
            db.scalars(
                select(StudyArtifact).where(
                    StudyArtifact.run_id == rid, StudyArtifact.kind == "delegation"
                )
            )
        )


def test_model_roles_parallel_handoffs_and_scoped_publication(clients, monkeypatch):
    a, b = clients
    fixture = fixture_candidates(a, monkeypatch)
    calls = provider(monkeypatch)
    rid = start(a, fixture)
    autonomous.execute(rid, "parent-decision")
    ids = children(a, rid)
    assert len(ids) == 2 and family(a, rid)["family"]["held_calls"] == 2
    assert not published(rid) and b.get("/api/v1/runs/" + str(ids[0])).status_code == 404
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda cid: autonomous.execute(cid, "child-" + str(cid)), ids))
    assert not published(rid) and len(calls) == 3
    autonomous.execute(rid, "parent-final")
    autonomous.execute(rid, "duplicate")
    assert family(a, rid)["status"] == "succeeded" and len(calls) == 4 and len(published(rid)) == 1
    assert family(a, rid)["family"]["held_calls"] == 0
    reports = a.get("/api/v1/study/tracks/" + fixture["tid"] + "/delegations").json()["data"]
    assert len(reports) == 1 and reports[0]["competence_verified"] is False
    code = next(e for e in reports[0]["evidence"] if e["kind"] == "code")
    assert code["start_line"] == 2 and code["end_line"] == 2
    assert a.get("/api/v1/citations/" + code["citation_id"]).status_code == 200
    assert b.get("/api/v1/study/tracks/" + fixture["tid"] + "/delegations").status_code == 404
    with Session(get_engine()) as db:
        source = next(
            db.get(AgentRun, cid)
            for cid in ids
            if db.get(AgentRun, cid).input_manifest["task_role"] == "requirement_analysis"
        )
        assert source.input_manifest["candidate"] is None
        assert source.input_manifest["delegation_scope"]["allowed_tools"] == []


def test_model_scope_proposals_rejected_without_expansion(clients, monkeypatch):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    calls = provider(
        monkeypatch,
        lambda data: [
            proposal(uuid4()),
            proposal(data["allowed_projects"][0]["candidate_id"], calls=2, key="too-much"),
        ],
    )
    rid = start(a, fixture, budget=3, roles=["code_investigation"])
    autonomous.execute(rid, "guarded-parent")
    state = family(a, rid)
    assert state["status"] == "partially_succeeded" and not state["family"]["children"]
    assert (
        state["family"]["consumed_calls"] == 2
        and state["family"]["held_calls"] == 0
        and len(calls) == 2
    )
    with Session(get_engine()) as db:
        errors = [
            e.payload["error"]
            for e in db.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == rid, RunEvent.type == "task.proposal_denied"
                )
            )
        ]
        assert set(errors) == {"TASK_SCOPE_FORBIDDEN", "FINAL_CALL_RESERVED"}


def test_contract_role_depth_budget_and_extra_tool_constraints(clients, monkeypatch):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    rid = start(a, fixture, budget=3, roles=["code_investigation"])
    with Session(get_engine()) as db, db.begin():
        epoch = claim(db, rid, "contract")
        root = leased(db, rid, epoch, "contract")
        cid = fixture["candidate_ids"][0]
        for raw in [
            proposal(cid, role="learning_design"),
            {**proposal(cid), "requirement_keys": ["outside"]},
        ]:
            with pytest.raises(BusinessError, match="TASK_SCOPE_FORBIDDEN"):
                service.child_manifest(root, DelegateArgs.model_validate(raw))
        with pytest.raises(ValueError):
            DelegateArgs.model_validate({**proposal(cid), "allowed_tools": ["execute_command"]})
        assert reserve_call(db, rid, epoch, "contract", {"max_output_tokens": 2048})
        args = DelegateArgs.model_validate(proposal(cid, calls=2))
        with pytest.raises(BusinessError, match="FINAL_CALL_RESERVED"):
            delegation.create_child(db, root, "code", service.child_manifest(root, args), 2, 2048)
        args = DelegateArgs.model_validate(proposal(cid))
        child = delegation.create_child(
            db, root, "code", service.child_manifest(root, args), 1, 2048
        )
        with pytest.raises(BusinessError, match="DELEGATION_FORBIDDEN"):
            delegation.create_child(db, child, "nested", {}, 1, 2048)
        cancel(db, root.owner_id, root.id, root.version)


def test_cancel_waiting_parent_blocks_independent_race_and_keeps_unknown_charge(
    clients, monkeypatch
):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    calls = provider(
        monkeypatch, lambda data: [proposal(data["allowed_projects"][0]["candidate_id"], calls=2)]
    )
    rid = start(a, fixture, budget=5)
    autonomous.execute(rid, "parent")
    child_id = children(a, rid)[0]
    with Session(get_engine()) as db, db.begin():
        root = db.get(AgentRun, rid)
        with pytest.raises(BusinessError, match="ASSISTANCE_RUN_ACTIVE"):
            assistance.before_independent(db, root.owner_id)
        epoch = claim(db, child_id, "inflight")
        assert reserve_call(db, child_id, epoch, "inflight", {"max_output_tokens": 2048})
    with Session(get_engine()) as db, db.begin():
        root = delegation.lock_run(db, rid)
        cancel(db, root.owner_id, root.id, root.version)
    autonomous.execute(child_id, "late")
    autonomous.execute(rid, "late-parent")
    state = family(a, rid)
    assert state["status"] == "cancelled" and state["family"]["consumed_calls"] == 2
    assert state["family"]["held_calls"] == 0 and not published(rid) and len(calls) == 1
    with Session(get_engine()) as db:
        assert (
            db.scalar(select(ModelCallReceipt.status).where(ModelCallReceipt.run_id == child_id))
            == "unknown"
        )


def test_saved_decision_recovery_no_extra_call_and_changed_requirement_blocks_join(
    clients, monkeypatch
):
    a, _ = clients
    fixture = fixture_candidates(a, monkeypatch)
    calls = provider(
        monkeypatch, lambda data: [proposal(data["allowed_projects"][0]["candidate_id"])]
    )
    rid = start(a, fixture, budget=3)
    original = delegation.create_child

    def lost(*args, **kwargs):
        raise LeaseLost("simulate response saved before proposal applied")

    monkeypatch.setattr(delegation, "create_child", lost)
    autonomous.execute(rid, "crash")
    assert len(calls) == 1 and not children(a, rid)
    with Session(get_engine()) as db, db.begin():
        row = db.get(AgentRun, rid)
        row.lease_until = db_now(db) - timedelta(seconds=1)
    with Session(get_engine()) as db, db.begin():
        reap(db)
    monkeypatch.setattr(delegation, "create_child", original)
    autonomous.execute(rid, "recover")
    assert len(calls) == 1 and len(children(a, rid)) == 1
    autonomous.execute(children(a, rid)[0], "child")
    with Session(get_engine()) as db, db.begin():
        track = db.get(StudyTrack, UUID(fixture["tid"]))
        requirement = study.current_requirements(db, track)
        replacement = study.put(db, track, "requirements", requirement.payload)
        track.payload = {**track.payload, "requirements_id": str(replacement.id)}
    autonomous.execute(rid, "invalid-join")
    assert len(calls) == 2 and family(a, rid)["status"] == "failed" and not published(rid)
    with Session(get_engine()) as db:
        audits = list(
            db.scalars(
                select(StageArtifact).where(
                    StageArtifact.run_id == rid, StageArtifact.stage_key == "context:decision:0"
                )
            )
        )
        assert len(audits) == 1 and audits[0].payload["request_hash"]
