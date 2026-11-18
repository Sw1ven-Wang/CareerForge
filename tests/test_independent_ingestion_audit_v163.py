"""Independent offline audit of research ingestion, authority, and recovery.

These are adversarial contract/mechanics checks using synthetic fixtures only.
They do not claim real model quality, repository execution, or live coverage.
"""

from copy import deepcopy

import httpx
import pytest
from sqlalchemy import func, select
from support import FILES

from careerforge.adapters.model import strict_schema
from careerforge.api import create_app
from careerforge.contracts import (
    AnalysisOutput,
    CoverageOutput,
    ImportSourceInput,
    KnowledgeBuildInput,
    ModelOutput,
    ReviewOutput,
    ToolCall,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    ArtifactDependency,
    CareerGoal,
    FundingAgreement,
    GoalVersion,
    ModelReceipt,
    ObjectRow,
    Publication,
    ReviewDecision,
    SourceRead,
)
from careerforge.services.build import ANALYSIS_SYSTEM, REVIEW_SYSTEM
from careerforge.services.dependencies import invalidate
from careerforge.services.investigation import INVESTIGATION_CONTRACT, Investigation, tool_specs
from careerforge.services.research_coverage import (
    ensure_goal_snapshot,
    evaluate_task_coverage,
    freeze_research_task,
    read_goal_version,
)
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint


def audit_source(env, *, namespace=None):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=namespace or env["ns"],
            repository_key="fixtures/audit",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )


def audit_goal(env):
    """Explicitly register an owned goal using the production snapshot function."""
    app = env["app"]
    run = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "knowledge_build", {}, 1, "audit-goal-origin"
    )
    text = "熟悉 Tool Calling 的调用流程和边界。"
    with app.db.transaction() as db:
        goal = CareerGoal(principal_id=env["actor"], namespace_id=env["ns"], title="audit")
        db.add(goal)
        db.flush()
        version = GoalVersion(
            goal_id=goal.id,
            version=1,
            run_id=run,
            source_text=text,
            content_hash=fingerprint(text),
            requirements={
                "requirements": [
                    {
                        "key": "tool",
                        "technology": "Tool Calling",
                        "keywords": ["tool"],
                        "depth": "熟悉",
                        "depth_basis": "explicit",
                        "quote": text,
                        "topic_key": "agent.tool_calling",
                    }
                ],
                "unresolved": [],
                "source_hash": fingerprint(text),
            },
        )
        db.add(version)
        db.flush()
        goal.active_version_id = version.id
        snapshot = ensure_goal_snapshot(db, env["actor"], version.id)
    return snapshot


def audit_build(env, source, **extra):
    return env["app"].build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source,
            question="Explain the implementation and its boundaries",
            topic_keys=["agent.tool_calling"],
            max_units_per_project=1,
            call_cap=10,
            idempotency_key="audit-build",
            **extra,
        ),
    )


async def test_audit_unanswered_preserves_accepted_facts_and_makes_root_partial(env, monkeypatch):
    source = audit_source(env)
    original = env["model"].complete

    async def with_unanswered(request):
        result = await original(request)
        if request["role"] == "analysis" and request.get("schema"):
            result.structured = {
                **result.structured,
                "unanswered_questions": ["Unverified error path"],
            }
        return result

    monkeypatch.setattr(env["model"], "complete", with_unanswered)
    run = audit_build(env, source)
    await env["app"].drain()
    with env["app"].db.read() as db:
        row = db.get(AgentRun, run)
        assert row.status == "partially_succeeded", row.status
    result = env["app"].runtime.stage(run, "result")
    assert result["publication_ids"], result
    assert result["unanswered_questions"] == ["Unverified error path"]
    assert "analysis_unanswered_questions" in result["gaps"]


@pytest.mark.parametrize("change", ["revoked", "suspended", "requirements_mutated"])
def test_audit_registered_goal_authority_is_shared_by_learning_and_research(env, change):
    snapshot = audit_goal(env)
    identifier = snapshot["goal_version_id"]
    with env["app"].db.transaction() as db:
        if change == "requirements_mutated":
            row = db.get(GoalVersion, identifier)
            row.requirements = {**row.requirements, "unresolved": ["tampered"]}
        else:
            invalidate(db, env["actor"], identifier, change, "independent audit")
    with env["app"].db.read() as db:
        with pytest.raises(DomainError):
            read_goal_version(db, env["actor"], identifier)
        with pytest.raises(DomainError):
            env["app"].learning._goal(db, env["actor"], identifier)


def test_audit_goal_cannot_be_adopted_by_another_principal_even_with_namespace_grant(env):
    snapshot = audit_goal(env)
    with env["app"].db.transaction() as db:
        other, _, _ = create_principal(db, "audit-other")
        set_grant(db, env["actor"], env["ns"], other.id, "writer", True)
        other_id = other.id
    with env["app"].db.read() as db:
        with pytest.raises(DomainError) as error:
            read_goal_version(db, other_id, snapshot["goal_version_id"])
    assert error.value.code == "NOT_FOUND"


def test_audit_goal_revocation_fences_runtime_before_any_new_receipt(env):
    snapshot = audit_goal(env)
    run = audit_build(env, audit_source(env), goal_version_id=snapshot["goal_version_id"])
    epoch = env["app"].runtime.claim(run, "audit-runtime")
    assert epoch is not None
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], snapshot["goal_version_id"], "revoked", "independent audit")
    with pytest.raises(DomainError) as error:
        env["app"].runtime.validate(run, epoch, "audit-runtime")
    assert error.value.code == "EVIDENCE_UNAVAILABLE"
    assert not env["model"].calls


@pytest.mark.parametrize("private_input", ["jd_text", "goal_version_id"])
def test_audit_public_build_rejects_private_goal_before_reserving_calls(env, private_input):
    snapshot = audit_goal(env)
    app = env["app"]
    public_ns = env["owner"]["public_namespace_id"]
    funding = app.runtime.create_funding(env["actor"], public_ns, 40)
    source = audit_source(env, namespace=public_ns)
    value = snapshot["source_text"] if private_input == "jd_text" else snapshot["goal_version_id"]
    with pytest.raises(DomainError) as error:
        app.build.start(
            env["actor"],
            KnowledgeBuildInput(
                namespace_id=public_ns,
                funding_id=funding,
                source_id=source,
                question="Read the code",
                topic_keys=["agent.tool_calling"],
                max_units_per_project=1,
                call_cap=20,
                idempotency_key="audit-public",
                **{private_input: value},
            ),
        )
    assert error.value.code == "PRIVATE_TO_PUBLIC"
    with app.db.read() as db:
        row = db.get(FundingAgreement, funding)
        assert row.calls_held == row.calls_consumed == 0


def audit_investigation(env, *, max_tools=1, max_reads=1):
    source = audit_source(env)
    app = env["app"]
    run = app.runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "analysis",
        {"source_ids": [source]},
        5,
        "audit-investigation",
    )
    epoch = app.runtime.claim(run, "audit-inv")
    contract = {
        "version": INVESTIGATION_CONTRACT,
        "max_rounds": 2,
        "max_tools": max_tools,
        "max_reads": max_reads,
        "max_batch": 3,
        "bootstrap_reads": 0,
    }
    return Investigation(
        app.sources,
        app.runtime,
        app.gateway,
        env["actor"],
        run,
        epoch,
        "audit-inv",
        source,
        "analysis",
        "audit-session",
        contract=contract,
    )


def reincarnate(inv):
    return Investigation(
        inv.sources,
        inv.runtime,
        inv.gateway,
        inv.actor,
        inv.run_id,
        inv.epoch,
        inv.worker,
        inv.source_id,
        inv.role,
        inv.session_key,
        contract=inv.contract,
    )


def test_audit_tool_budget_survives_crash_between_source_read_and_tool_receipt(env, monkeypatch):
    inv = audit_investigation(env)

    def crash(*args, **kwargs):
        raise RuntimeError("Synthetic crash after source receipt, before tool receipt")

    monkeypatch.setattr(inv, "_commit_tool", crash)
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        inv.execute_tool(
            "first", "read_span", {"path": "src/tools.py", "start_line": 1, "end_line": 1}
        )
    resumed = reincarnate(inv)
    with pytest.raises(DomainError) as error:
        resumed.execute_tool(
            "second", "read_span", {"path": "src/tools.py", "start_line": 2, "end_line": 2}
        )
    assert error.value.code in {"TOOL_BUDGET", "READ_BUDGET", "TOOL_REPLAY"}
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) <= 1


def assessment(subject="subject", claim="claim", **extra):
    return {
        "status": "known",
        "explanation": "Synthetic audited fixture",
        "anchors": [{"subject_key": subject, "claim_keys": [claim]}],
        **extra,
    }


def scoped_coverage(**extra):
    return {
        "question": assessment(),
        "dimensions": [],
        "requirements": [],
        "unresolved_questions": [],
        **extra,
    }


def coverage_fixture():
    task = freeze_research_task("Explain this exact fact", scope="focused")
    analysis = [{"source_id": "source-a", "coverage": scoped_coverage()}]
    review = [
        {"source_id": "source-a", "unit_version_id": "version-a", "coverage": scoped_coverage()}
    ]
    anchors = [
        {
            "source_id": "source-a",
            "subject_key": "subject",
            "claim_key": "claim",
            "claim_id": "claim-a",
            "publication_id": "publication-a",
            "unit_version_id": "version-a",
            "statement": "Supported scoped fact.",
            "conditions": [],
            "kind": "source_fact",
        }
    ]
    return task, analysis, review, anchors


def test_audit_coverage_requires_published_same_source_same_candidate_and_independent_anchors():
    task, analysis, review, anchors = coverage_fixture()
    assert evaluate_task_coverage(task, analysis, review, anchors)["complete"]
    assert not evaluate_task_coverage(task, analysis, review, [])["complete"]
    for field in ("source_id", "unit_version_id"):
        wrong = deepcopy(review)
        wrong[0][field] = "outside"
        assert not evaluate_task_coverage(task, analysis, wrong, anchors)["complete"]
    no_independent_anchor = deepcopy(review)
    no_independent_anchor[0]["coverage"]["question"]["anchors"] = []
    assert not evaluate_task_coverage(task, analysis, no_independent_anchor, anchors)["complete"]


def test_audit_unpublished_and_ambiguous_claims_never_count_as_complete():
    task, analysis, review, anchors = coverage_fixture()
    extra = deepcopy(anchors[0])
    extra.update(claim_id="different", unit_version_id="different")
    value = evaluate_task_coverage(task, analysis, review, [*anchors, extra])
    assert not value["complete"] and "ambiguous_candidate_claim" in value["reason_codes"]


def test_audit_sufficiency_evidence_preserves_approved_claim_conditions():
    task, analysis, review, anchors = coverage_fixture()
    anchors[0]["conditions"] = ["Only for a configured synchronous handler"]
    anchors[0]["kind"] = "source_fact"
    value = evaluate_task_coverage(task, analysis, review, anchors)
    assert value["complete"]
    evidence = value["question"]["evidence"][0]
    assert evidence.get("conditions") == anchors[0]["conditions"]
    assert evidence.get("kind") == "source_fact"


async def test_audit_public_knowledge_has_no_research_task_or_coverage_metadata(env):
    public_ns = env["owner"]["public_namespace_id"]
    app = env["app"]
    source = audit_source(env, namespace=public_ns)
    funding = app.runtime.create_funding(env["actor"], public_ns, 20)
    run = app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=public_ns,
            funding_id=funding,
            source_id=source,
            question="PRIVATE_TASK_SENTINEL Find echo",
            topic_keys=["agent.tool_calling"],
            max_units_per_project=1,
            call_cap=10,
            idempotency_key="audit-clean-public",
        ),
    )
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"], result
    assert result["coverage_visibility"] == "owner_private"
    with app.db.read() as db:
        payloads = [
            row.payload
            for row in db.scalars(select(ObjectRow).where(ObjectRow.namespace_id == public_ns))
        ]
        assert "PRIVATE_TASK_SENTINEL" not in str(payloads)
        assert "task_coverage" not in str(payloads)
        assert "research_task" not in str(payloads)
    with app.db.transaction() as db:
        _, _, other_token = create_principal(db, "public-audit-reader")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://audit.local"
    ) as client:
        response = await client.get(
            "/api/v1/runs/" + run, headers={"Authorization": "Bearer " + other_token}
        )
    assert response.status_code == 404


async def test_audit_final_task_anchors_revalidate_accepted_publication_pack(env, monkeypatch):
    app = env["app"]
    source = audit_source(env)
    original = app.build._save_build_result
    changed = []

    def tamper_before_final(run, epoch, worker, payload):
        if payload.get("publication_ids") and not changed:
            with app.db.transaction() as db:
                publication = db.get(Publication, payload["publication_ids"][0])
                review = db.get(ReviewDecision, publication.review_id)
                review.decision = "reject"
                changed.append(publication.id)
        return original(run, epoch, worker, payload)

    monkeypatch.setattr(app.build, "_save_build_result", tamper_before_final)
    run = audit_build(env, source)
    await app.drain()
    assert changed
    with app.db.read() as db:
        with pytest.raises(DomainError) as error:
            app.knowledge.publication_pack(db, env["actor"], changed[0])
        assert error.value.code == "REVIEW_HASH"
        row = db.get(AgentRun, run)
        assert row.status == "failed", row.status
    assert app.runtime.stage(run, "result") is None


async def test_audit_historical_task_result_rechecks_publication_projection(env):
    app = env["app"]
    run = audit_build(env, audit_source(env))
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"]
    with app.db.transaction() as db:
        publication = db.get(Publication, result["publication_ids"][0])
        db.get(ReviewDecision, publication.review_id).decision = "reject"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://audit.local"
    ) as client:
        response = await client.get(
            "/api/v1/runs/" + run, headers={"Authorization": "Bearer " + env["owner"]["token"]}
        )
    assert response.status_code >= 400, response.json()


def test_audit_legacy_provider_contracts_match_immutable_v162_fingerprints():
    # Calculated in an isolated PYTHONDONTWRITEBYTECODE process from the
    # untouched v1.6.2 baseline, not copied from the patched implementation.
    expected = {
        "legacy_tool_specs": "0a0f875c6bbbaf50410ae61eb763a844e751f211295f06a0dceb8997cdca19ad",
        "analysis_system": "6dfe3ce07a3c85e18c4ac95611e3aebd40f40dd3e0547fe8ca2dc1a6139ca4cb",
        "review_system": "63ee70fe96e0b44038aebf9828d140fb4ca2bed0cb39539651dec6b347ed3e3d",
        "analysis_schema": "29bbec38adcf06cb6ccb3f58fe3d00fae99fe87c811d2d82235faa84294ee18d",
        "review_schema": "1aac0d8f162cfe2ea9154d2fb25a8df06002887f65d3def8a96687cb3cd7913e",
        "coverage_schema": "4bfc6eb28d9b4c4eb63025791ff4215fe35c98e1d21942cf7f9752e1ebaa9363",
    }
    values = {
        "legacy_tool_specs": tool_specs(),
        "analysis_system": ANALYSIS_SYSTEM,
        "review_system": REVIEW_SYSTEM,
        "analysis_schema": strict_schema(AnalysisOutput.model_json_schema()),
        "review_schema": strict_schema(ReviewOutput.model_json_schema()),
        "coverage_schema": strict_schema(CoverageOutput.model_json_schema()),
    }
    assert {key: fingerprint(value) for key, value in values.items()} == expected


async def test_audit_unmarked_analysis_receipts_replay_without_upgrade_or_charge(env, monkeypatch):
    app = env["app"]
    source = audit_source(env)
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=source,
        question="Explain echo",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        call_cap=5,
        idempotency_key="audit-legacy",
    )
    with app.db.transaction() as db:
        # Use current admission only for common taxonomy/policy snapshots.
        # The durable legacy input below has the historical five-call cap.
        current = app.build.prepare_in(db, env["actor"], body.model_copy(update={"call_cap": 10}))
        legacy = {
            key: value
            for key, value in current.items()
            if key
            in {
                "namespace_id",
                "funding_id",
                "question",
                "repository_url",
                "source_id",
                "topic_keys",
                "jd_text",
                "call_cap",
                "max_nodes",
                "max_depth",
                "max_projects",
                "max_units_per_project",
                "github_request_cap",
                "idempotency_key",
                "policies",
                "policy_input_contract",
                "taxonomy_versions",
                "taxonomy_data_classes",
                "model_profile",
            }
        }
        legacy["call_cap"] = 5
        root = app.runtime.create_in(
            db,
            env["actor"],
            env["ns"],
            env["funding"],
            "knowledge_build",
            legacy,
            5,
            "audit-legacy",
        ).id
    epoch = app.runtime.claim(root, "audit-old-root")
    with app.db.read() as db:
        root_row = db.get(AgentRun, root)
    assert not await app.build.root(root_row, epoch, "audit-old-root")
    child = app.runtime.stage(root, "analysis_child")["run_id"]
    child_epoch = app.runtime.claim(child, "audit-old-analysis")
    with app.db.read() as db:
        child_row = db.get(AgentRun, child)
    original = app.knowledge.save_analysis

    def crash(*args, **kwargs):
        raise DomainError("AUDIT_CRASH", "After paid final response")

    monkeypatch.setattr(app.knowledge, "save_analysis", crash)
    with pytest.raises(DomainError, match="After paid final response"):
        await app.build.analyze(child_row, child_epoch, "audit-old-analysis")
    with app.db.read() as db:
        before = {
            row.id: (row.request_hash, deepcopy(row.response))
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == child))
        }
    assert len(before) == len(env["model"].calls) == 2
    monkeypatch.setattr(app.knowledge, "save_analysis", original)
    await app.build.analyze(child_row, child_epoch, "audit-old-analysis")
    with app.db.read() as db:
        after = {
            row.id: (row.request_hash, row.response)
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == child))
        }
        assert db.get(AgentRun, child).calls_consumed == 2
    assert after == before and len(env["model"].calls) == 2
    assert "task_coverages" not in app.runtime.stage(child, "result")


async def test_audit_impossible_jd_budget_fails_before_extract_spend(env):
    app = env["app"]
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=audit_source(env),
        question="Explain the implementation",
        jd_text="熟悉 Tool Calling 的边界。",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        call_cap=5,
        idempotency_key="audit-impossible-budget",
    )
    try:
        run = app.build.start(env["actor"], body)
    except DomainError as error:
        assert error.code == "BUILD_BUDGET"
    else:
        await app.drain()
        with app.db.read() as db:
            assert db.get(AgentRun, run).status == "budget_stopped"
    assert env["model"].calls == [], "An unaffordable task spent on JD extraction"
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0


def synthetic_task_coverage(task, subject="echo", claim="return"):
    def row(**extra):
        return assessment(subject=subject, claim=claim, **extra)

    return scoped_coverage(
        question=row(),
        dimensions=[row(dimension=name) for name in task["required_dimensions"]],
        requirements=[
            row(
                requirement_id=requirement["requirement_id"],
                demonstrated_depth=requirement["depth"],
                dimensions=[row(dimension=name) for name in requirement["required_dimensions"]],
            )
            for requirement in task["requirements"]
        ],
    )


def install_sufficiency_fixture(env, monkeypatch, *, bad_final=None, all_rounds=False):
    original = env["model"].complete

    async def scripted(request):
        result = await original(request)
        context = request["context"]
        if request["role"] in {"analysis", "reviewer"} and not request.get("schema"):
            if all_rounds:
                return ModelOutput(
                    synthetic=True,
                    tool_calls=[
                        ToolCall(
                            id="read-tree",
                            name="list_tree",
                            arguments={},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 5, "completion_tokens": 5},
                )
        elif request["role"] == "analysis":
            result.structured = {
                **result.structured,
                "task_coverage": synthetic_task_coverage(context["research_task"]),
            }
        elif context.get("assessment_kind") == "independent_task_sufficiency":
            assert "task_coverages" not in context and "review" not in context
            assert context["verified_units"] and context["coverage_anchors"]
            source = context["verified_units"][0]["source_id"]
            if bad_final == "foreign_source":
                source = "unauthorized-source"
            result.structured = {
                "source_coverages": [
                    {
                        "source_id": source,
                        "coverage": synthetic_task_coverage(context["research_task"]),
                    }
                ]
            }
            if bad_final == "malformed":
                result.structured = {"unexpected": "not a sufficiency contract"}
        return result

    monkeypatch.setattr(env["model"], "complete", scripted)


@pytest.mark.parametrize("cap", [6, 7, 8, 10, 11, 13, 30])
async def test_audit_worst_case_rounds_reserve_independent_review_and_final_sufficiency(
    env, monkeypatch, cap
):
    app = env["app"]
    install_sufficiency_fixture(env, monkeypatch, all_rounds=True)
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=audit_source(env),
        question="Explain the implementation",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        call_cap=cap,
        idempotency_key="audit-full-budget",
    )
    if cap < 8:
        # New admissions reserve one correction for each final. Old frozen
        # six-call contracts are covered by test_build_execution_v164 instead.
        with pytest.raises(DomainError) as error:
            app.build.start(env["actor"], body)
        assert error.value.code == "BUILD_BUDGET"
        assert not env["model"].calls
        with app.db.read() as db:
            assert db.get(FundingAgreement, env["funding"]).calls_held == 0
        return
    run = app.build.start(env["actor"], body)
    with app.db.read() as db:
        manifest = deepcopy(db.get(AgentRun, run).input_manifest)
    a = manifest["investigation_contracts"]["analysis"]["max_rounds"]
    r = manifest["investigation_contracts"]["reviewer"]["max_rounds"]
    reserved = (a + 1) + (r + 2) + manifest["sufficiency_call_reserve"]
    assert reserved + 2 <= cap and manifest["sufficiency_call_reserve"] == 1
    assert manifest["execution_plan"]["quote_correction_calls_per_source"] == 2
    assert 1 <= a <= 4 and 1 <= r <= 3
    if cap >= 13:
        assert (a, r) == (4, 3)
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"] and result["task_coverage"]["complete"], result
    assert (
        sum(
            call["context"].get("assessment_kind") == "independent_task_sufficiency"
            for call in env["model"].calls
        )
        == 1
    )
    with app.db.read() as db:
        row = db.get(AgentRun, run)
        assert row.status == "succeeded" and row.calls_consumed == reserved
        assert row.held_calls == 0
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


@pytest.mark.parametrize(
    "bad_final,code", [("foreign_source", "TASK_COVERAGE_SCOPE"), ("malformed", "MODEL_SCHEMA")]
)
async def test_audit_bad_final_sufficiency_cannot_approve_task_or_erase_accepted_facts(
    env, monkeypatch, bad_final, code
):
    install_sufficiency_fixture(env, monkeypatch, bad_final=bad_final)
    run = audit_build(env, audit_source(env))
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    assert result["publication_ids"]
    assert not result["task_coverage"]["complete"]
    assert "task_sufficiency:" + code in result["gaps"]
    with env["app"].db.read() as db:
        assert db.get(AgentRun, run).status == "partially_succeeded"


async def test_audit_goal_context_and_private_egress_survive_every_research_stage(env, monkeypatch):
    app = env["app"]
    snapshot = audit_goal(env)
    install_sufficiency_fixture(env, monkeypatch)
    public_ns = env["owner"]["public_namespace_id"]
    source = audit_source(env, namespace=public_ns)
    run = audit_build(env, source, goal_version_id=snapshot["goal_version_id"])
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"] and result["task_coverage"]["complete"], result
    task_calls = [call for call in env["model"].calls if "research_task" in call["context"]]
    assert {call["role"] for call in task_calls} == {"analysis", "reviewer", "coverage"}
    for call in task_calls:
        task = call["context"]["research_task"]
        assert task["goal_version_id"] == snapshot["goal_version_id"]
        assert task["requirements"] == snapshot["requirements"]
        assert "private_jd" in call["data_classes"]
    with app.db.read() as db:
        public_payloads = [
            row.payload
            for row in db.scalars(select(ObjectRow).where(ObjectRow.namespace_id == public_ns))
        ]
        assert snapshot["source_text"] not in str(public_payloads)
        edges = db.scalars(
            select(ArtifactDependency).where(
                (ArtifactDependency.source_id == snapshot["goal_version_id"])
                | (ArtifactDependency.dependent_id == snapshot["goal_version_id"])
            )
        ).all()
        assert edges == [], "Goal registry must remain a standalone private authority root"


async def test_audit_public_source_does_not_waive_private_candidate_egress_authorization(
    env, monkeypatch
):
    app = env["app"]
    snapshot = audit_goal(env)
    install_sufficiency_fixture(env, monkeypatch)
    allowed = ["public_source", "private_jd"]
    funding = app.runtime.create_funding(env["actor"], env["ns"], 20, data_classes=allowed)
    source = audit_source(env, namespace=env["owner"]["public_namespace_id"])
    run = app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=funding,
            source_id=source,
            goal_version_id=snapshot["goal_version_id"],
            question="Explain the implementation",
            topic_keys=["agent.tool_calling"],
            max_units_per_project=1,
            call_cap=10,
            idempotency_key="audit-narrow-egress",
        ),
    )
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"] == []
    assert not any(call["role"] == "reviewer" for call in env["model"].calls)
    assert not any(
        call["context"].get("assessment_kind") == "independent_task_sufficiency"
        for call in env["model"].calls
    )
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).allowed_data_classes == allowed
