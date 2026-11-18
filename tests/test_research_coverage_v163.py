"""Offline sufficiency/identity tests, independent of live provider outputs."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from careerforge.contracts import AnalysisOutput, CoverageOutput, KnowledgeBuildInput, ReviewOutput
from careerforge.contracts_research_v163 import (
    ResearchAnalysisOutput,
    ResearchCoverageDraft,
    ResearchReviewOutput,
    ResearchSufficiencyOutput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    CareerGoal,
    GoalVersion,
    Namespace,
    ObjectRow,
    StageArtifact,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.research_coverage import (
    DEPTH_DIMENSIONS,
    RESEARCH_CONTRACT_VERSION,
    ensure_goal_snapshot,
    evaluate_task_coverage,
    freeze_research_task,
    iter_requirement_items,
    normalize_requirements,
    read_goal_version,
    requirement_identity,
    validate_goal_snapshot,
    validate_research_run,
)
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint


def requirement(depth="掌握"):
    return {
        "key": "event_delivery",
        "technology": "Event delivery",
        "keywords": ["events", "delivery"],
        "depth": depth,
        "depth_basis": "explicit",
        "quote": depth + "事件投递的实现与失败边界",
        "topic_key": None,
    }


def assessment(**kwargs):
    return {
        "status": "known",
        "explanation": "The candidate establishes this within its documented boundary.",
        "anchors": [{"subject_key": "delivery", "claim_keys": ["dispatch"]}],
        "unresolved_questions": [],
        **kwargs,
    }


def coverage(task):
    return {
        "question": assessment(),
        "dimensions": [assessment(dimension=dimension) for dimension in task.required_dimensions],
        "requirements": [
            assessment(
                requirement_id=req.requirement_id,
                demonstrated_depth=req.depth,
                dimensions=[
                    assessment(dimension=dimension) for dimension in req.required_dimensions
                ],
            )
            for req in task.requirements
        ],
        "unresolved_questions": [],
    }


def anchor(**kwargs):
    return {
        "source_id": "source-1",
        "subject_key": "delivery",
        "claim_key": "dispatch",
        "claim_id": "claim-1",
        "publication_id": "pub-1",
        "unit_version_id": "unit-1",
        "statement": "The dispatch module routes events to a registered handler and preserves failures.",
        "conditions": [],
        "kind": "source_fact",
        **kwargs,
    }


def evaluate(task=None, *, analysis=None, review=None, anchors=None, review_scope=None):
    task = task or freeze_research_task("Explain event delivery architecture")
    return evaluate_task_coverage(
        task,
        [{"source_id": "source-1", "coverage": coverage(task) if analysis is None else analysis}],
        [
            {
                "source_id": "source-1",
                "unit_version_id": "unit-1",
                "coverage": coverage(task) if review is None else review,
                **(review_scope or {}),
            }
        ],
        [anchor()] if anchors is None else anchors,
    )


def test_legacy_model_contracts_unchanged_and_new_missing_coverage_is_unknown():
    assert set(AnalysisOutput.model_fields) == {"units", "unanswered_questions"}
    assert "task_coverage" not in ReviewOutput.model_fields
    assert set(CoverageOutput.model_fields) == {
        "all_facts_bound",
        "conditions_preserved",
        "uncovered_elements",
        "unsupported_titles",
        "rationale_codes",
    }
    value = ResearchAnalysisOutput.model_validate({"units": []})
    assert value.task_coverage.question.status == "unknown"
    assert value.task_coverage.question.explanation == "not_reported"
    review = ResearchReviewOutput.model_validate(
        {
            "claim_decisions": [],
            "accepted_topic_link_ids": [],
            "accepted_facet_ids": [],
            "blocking_issues": [],
            "decision": "needs_evidence",
        }
    )
    assert review.task_coverage.question.status == "unknown"
    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate({"units": []})


def test_new_input_fields_are_optional_and_project_scope_preserves_dimensions():
    old = KnowledgeBuildInput(
        namespace_id="private", funding_id="fund", question="Explain", idempotency_key="k"
    )
    assert old.research_contract_version is None and old.goal_version_id is None
    task = freeze_research_task("Explain", required_dimensions=[])
    assert task.required_dimensions == ["project_structure", "module_responsibilities", "call_flow"]
    assert freeze_research_task("Explain", scope="focused").required_dimensions == []


def test_requirement_wrapper_legacy_list_and_keyed_dict_align():
    req = requirement()
    wrapped = {
        "requirements": [req],
        "unresolved": ["unmapped"],
        "source_hash": fingerprint(req["quote"]),
    }
    result, unresolved = normalize_requirements(wrapped, source_text=req["quote"])
    legacy, _ = normalize_requirements([req], source_text=req["quote"])
    keyed, _ = normalize_requirements({req["key"]: {k: v for k, v in req.items() if k != "key"}})
    assert result == legacy == keyed
    assert result[0].requirement_id == "event_delivery"
    assert result[0].quote == req["quote"] and result[0].depth == "掌握"
    assert unresolved == ["unmapped"]


def test_requirement_identity_is_not_list_order_and_legacy_partial_stays_data():
    req = {key: value for key, value in requirement().items() if key != "key"}
    reordered = {**req, "keywords": list(reversed(req["keywords"]))}
    assert requirement_identity(req) == requirement_identity(reordered)
    partial = {"technology": "Queue", "quote": "Queue familiarity"}
    assert iter_requirement_items({"requirements": [partial], "source_hash": "metadata"}) == [
        partial
    ]
    result, unresolved = normalize_requirements([partial])
    assert result == [] and unresolved == [requirement_identity(partial)]
    assert iter_requirement_items({"unresolved": ["Queue"], "source_hash": "metadata"}) == []


def test_duplicate_requirement_keys_cannot_hide_conflicting_content():
    req = requirement()
    exact, unresolved = normalize_requirements([req, deepcopy(req)])
    assert len(exact) == 1 and unresolved == []
    reordered, unresolved = normalize_requirements(
        [req, {**req, "keywords": list(reversed(req["keywords"]))}]
    )
    assert reordered == exact and unresolved == []
    result, unresolved = normalize_requirements([req, {**req, "quote": "different quote"}, req])
    assert result == [] and unresolved == [req["key"]]


@pytest.mark.parametrize("change", [{"quote": "absent"}, {"depth": "了解"}])
def test_requirement_quotes_and_explicit_depth_are_rechecked(change):
    req = requirement()
    rows, unresolved = normalize_requirements([{**req, **change}], source_text=req["quote"])
    assert rows == [] and unresolved == [req["key"]]


def test_true_facts_can_be_published_without_task_sufficiency():
    task = freeze_research_task("Explain the repository")
    shallow = coverage(task)
    shallow["dimensions"] = []
    value = evaluate(task, analysis=shallow, review=shallow)
    assert value["publication_ids"] == ["pub-1"]
    assert value["status"] == "partial" and not value["complete"]
    assert all(not item["complete"] for item in value["dimensions"])


def test_independent_anchored_assessments_can_establish_the_task():
    value = evaluate()
    assert value["status"] == "complete" and value["complete"]
    assert value["question"]["evidence"][0]["claim_id"] == "claim-1"
    assert value["fact_support_is_not_task_sufficiency"]


def test_published_anchor_scope_roundtrips_without_erasing_conditions_or_claim_kind():
    scoped = anchor(
        conditions=["Only for the fixed source version", "No runtime execution was observed"],
        kind="analytical_inference",
    )
    value = evaluate(anchors=[scoped])
    assert value["complete"]
    for item in [value["question"], *value["dimensions"]]:
        assert item["evidence"][0]["conditions"] == scoped["conditions"]
        assert item["evidence"][0]["kind"] == "analytical_inference"
    scoped["conditions"].append("Later caller mutation")
    assert len(value["question"]["evidence"][0]["conditions"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"conditions": None},
        {"conditions": "unbounded"},
        {"conditions": [False]},
        {"kind": "unknown"},
        {"kind": None},
        {"kind": ["source_fact"]},
    ],
)
def test_missing_or_invalid_published_claim_scope_fails_closed(change):
    value = evaluate(anchors=[anchor(**change)])
    assert not value["complete"] and not value["question"]["evidence"]
    assert "published_anchor_scope_invalid" in value["reason_codes"]
    absent = anchor()
    absent.pop("conditions")
    absent.pop("kind")
    assert not evaluate(anchors=[absent])["complete"]


@pytest.mark.parametrize("status", ["unknown", "partial", "conflicting", "not_applicable"])
def test_unknown_or_not_applicable_dimension_never_completes(status):
    task = freeze_research_task("Explain the repository")
    reviewed = coverage(task)
    reviewed["dimensions"][0]["status"] = status
    value = evaluate(task, review=reviewed)
    assert not value["complete"] and not value["dimensions"][0]["complete"]


def test_scoped_not_applicable_does_not_remove_global_jd_requirement():
    task = freeze_research_task("Explain", goal_version_id="goal-1", requirements=[requirement()])
    reviewed = coverage(task)
    reviewed["requirements"][0]["status"] = "not_applicable"
    value = evaluate(task, review=reviewed)
    assert not value["complete"]
    row = value["requirements"][0]
    assert row["requirement_id"] == "event_delivery" and not row["complete"]
    assert "project_not_applicable_does_not_satisfy_goal" in row["reason_codes"]
    assert row["scoped_assessments"][0]["status"] == "not_applicable"


def test_requirement_depth_requires_independently_supported_explanations():
    task = freeze_research_task("Explain", requirements=[requirement()])
    reviewed = coverage(task)
    reviewed["requirements"][0]["demonstrated_depth"] = "了解"
    value = evaluate(task, review=reviewed)
    assert not value["complete"]
    assert "insufficient_depth" in value["requirements"][0]["reason_codes"]
    reviewed = coverage(task)
    reviewed["requirements"][0]["dimensions"] = []
    value = evaluate(task, review=reviewed)
    assert not value["complete"]
    assert "missing_depth_dimension:implementation" in value["requirements"][0]["reason_codes"]
    assert evaluate(task)["requirements"][0]["demonstrated_depth"] == "掌握"
    assert task.requirements[0].required_dimensions == list(DEPTH_DIMENSIONS["掌握"])


def test_unresolved_requirement_or_question_never_completes():
    task = freeze_research_task("Explain", unresolved_requirement_ids=["missing"])
    assert not evaluate(task)["complete"]
    task = freeze_research_task("Explain")
    reviewed = coverage(task)
    reviewed["unresolved_questions"] = ["The failure path has not been established."]
    assert not evaluate(task, review=reviewed)["complete"]
    reviewed = coverage(task)
    reviewed["question"]["unresolved_questions"] = ["The failure path has not been established."]
    assert not evaluate(task, review=reviewed)["complete"]


@pytest.mark.parametrize(
    "anchors",
    [
        [],
        [anchor(claim_key="unrelated")],
        [anchor(), anchor(claim_id="collision", publication_id="pub-2")],
    ],
)
def test_unpublished_missing_or_ambiguous_claim_anchors_fail_closed(anchors):
    assert not evaluate(anchors=anchors)["complete"]


@pytest.mark.parametrize(
    "scope", [{"source_id": "different-source"}, {"unit_version_id": "different-unit"}]
)
def test_anchors_cannot_borrow_other_source_or_reviewed_candidate(scope):
    assert not evaluate(review_scope=scope)["complete"]


def test_review_cannot_upgrade_analysis_unknown_or_omit_a_required_anchor():
    task = freeze_research_task("Explain")
    analysis = coverage(task)
    analysis["question"]["status"] = "unknown"
    assert not evaluate(task, analysis=analysis)["complete"]
    analysis = coverage(task)
    analysis["question"]["anchors"][0]["claim_keys"].append("failure")
    assert not evaluate(
        task, analysis=analysis, anchors=[anchor(), anchor(claim_key="failure", claim_id="claim-2")]
    )["complete"]


def test_root_composition_audit_can_establish_cross_unit_question_without_fabricating_anchors():
    task = freeze_research_task("Explain the complete event delivery", scope="focused")
    analysis = coverage(task)
    analysis["question"]["anchors"].append({"subject_key": "failure", "claim_keys": ["retry"]})
    published = [
        anchor(),
        anchor(
            subject_key="failure",
            claim_key="retry",
            claim_id="claim-2",
            publication_id="pub-2",
            unit_version_id="unit-2",
        ),
    ]
    output = ResearchSufficiencyOutput.model_validate(
        {"source_coverages": [{"source_id": "source-1", "coverage": analysis}]}
    )
    reviewed = output.source_coverages[0].model_dump(mode="json")
    value = evaluate_task_coverage(
        task,
        [{"source_id": "source-1", "coverage": analysis}],
        [{**reviewed, "unit_version_ids": ["unit-1", "unit-2"]}],
        published,
    )
    assert value["complete"] and len(value["question"]["evidence"]) == 2
    # A single-unit reviewer cannot authorize the other unit's contribution.
    assert not evaluate_task_coverage(
        task,
        [{"source_id": "source-1", "coverage": analysis}],
        [{**reviewed, "unit_version_id": "unit-1"}],
        published,
    )["complete"]
    for scope in (["unit-1"], ["unit-1", "unit-2", "unpublished"], []):
        assert not evaluate_task_coverage(
            task,
            [{"source_id": "source-1", "coverage": analysis}],
            [{**reviewed, "unit_version_ids": scope}],
            published,
        )["complete"]


def test_new_analysis_anchors_must_point_to_its_own_candidates():
    with pytest.raises(ValidationError, match="unknown candidate claim"):
        ResearchAnalysisOutput.model_validate(
            {"units": [], "task_coverage": {"question": assessment()}}
        )
    with pytest.raises(ValidationError, match="Duplicate"):
        ResearchCoverageDraft.model_validate(
            {
                "question": assessment(),
                "dimensions": [
                    assessment(dimension="call_flow"),
                    assessment(dimension="call_flow"),
                ],
            }
        )


def test_legacy_unanswered_questions_are_preserved_as_task_gaps():
    output = ResearchAnalysisOutput.model_validate(
        {
            "units": [],
            "unanswered_questions": ["Does retry preserve order?"],
        }
    )
    assert output.task_coverage.unresolved_questions == ["Does retry preserve order?"]


def make_goal(env):
    app, actor = env["app"], env["actor"]
    run_id = app.runtime.create(actor, env["ns"], env["funding"], "research", {}, 1, "goal-fixture")
    req = requirement()
    with app.db.transaction() as db:
        goal = CareerGoal(principal_id=actor, namespace_id=env["ns"], title="Fixture goal")
        db.add(goal)
        db.flush()
        version = GoalVersion(
            goal_id=goal.id,
            version=1,
            source_text=req["quote"],
            content_hash=fingerprint(req["quote"]),
            requirements={"requirements": [req], "unresolved": []},
            run_id=run_id,
        )
        db.add(version)
        db.flush()
        goal.active_version_id = version.id
        identifier = version.id
    return identifier


def test_goal_snapshot_requires_explicit_registration_and_rechecks_projection(env):
    identifier = make_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.read() as db, pytest.raises(DomainError):
        read_goal_version(db, actor, identifier)
    with app.db.transaction() as db:
        snapshot = ensure_goal_snapshot(db, actor, identifier)
        assert ensure_goal_snapshot(db, actor, identifier) == snapshot
    with app.db.read() as db:
        assert validate_goal_snapshot(db, actor, snapshot) == snapshot
    with app.db.transaction() as db:
        row = db.get(GoalVersion, identifier)
        row.requirements = {"requirements": [{**requirement(), "technology": "silently changed"}]}
    with app.db.read() as db, pytest.raises(DomainError) as error:
        read_goal_version(db, actor, identifier)
    assert error.value.code == "GOAL_HASH"


def test_goal_snapshot_cannot_be_reused_by_namespace_grantee(env):
    identifier = make_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        ensure_goal_snapshot(db, actor, identifier)
        other, _, _ = create_principal(db, "other-reader")
        other_id = other.id
        set_grant(db, actor, env["ns"], other_id, "writer", True)
    with app.db.read() as db, pytest.raises(DomainError) as error:
        read_goal_version(db, other_id, identifier)
    assert error.value.code == "NOT_FOUND"


@pytest.mark.parametrize("change", ["revoked", "public", "privacy"])
def test_goal_read_and_new_write_fence_after_revocation_public_move_or_erasure(env, change):
    identifier = make_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        snapshot = ensure_goal_snapshot(db, actor, identifier)
    task = freeze_research_task(
        "Explain", goal_version_id=identifier, requirements=snapshot["requirements"]
    )
    manifest = {
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "question": "Explain",
        "goal_version_id": identifier,
        "goal_snapshot": snapshot,
        "research_task": task.model_dump(mode="json"),
    }
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "research", manifest, 1, "research-fixture"
    )
    with app.db.read() as db:
        validate_research_run(db, actor, db.get(AgentRun, run_id))
    with app.db.transaction() as db:
        if change == "revoked":
            db.get(ObjectRow, identifier).state = "revoked"
        elif change == "public":
            ns = Namespace(kind="shared_public", name="public-fixture", owner_id=actor)
            db.add(ns)
            db.flush()
            db.get(ObjectRow, identifier).namespace_id = ns.id
        else:
            db.get(PrivacyState, actor).blocked = True
    with app.db.read() as db, pytest.raises(DomainError):
        validate_goal_snapshot(db, actor, snapshot)
    with app.db.read() as db, pytest.raises(DomainError):
        validate_research_run(db, actor, db.get(AgentRun, run_id))


def test_new_run_goal_fence_resolves_root_context_and_old_runs_remain_unchanged(env):
    identifier = make_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        snapshot = ensure_goal_snapshot(db, actor, identifier)
    task = freeze_research_task(
        "Explain", goal_version_id=identifier, requirements=snapshot["requirements"]
    )
    manifest = {
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "question": "Explain",
        "goal_version_id": identifier,
        "goal_snapshot": snapshot,
    }
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "research", manifest, 1, "root-stage-fixture"
    )
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        db.add(
            StageArtifact(
                run_id=run.id,
                stage_key="research_context",
                input_hash=run.input_hash,
                payload={"research_task": task.model_dump(mode="json"), "goal_snapshot": snapshot},
            )
        )
    with app.db.read() as db:
        validate_research_run(db, actor, db.get(AgentRun, run_id))
    with app.db.transaction() as db:
        db.get(ObjectRow, identifier).state = "revoked"
    with app.db.read() as db, pytest.raises(DomainError):
        validate_research_run(db, actor, db.get(AgentRun, run_id))
    with app.db.read() as db:
        legacy_run = db.get(AgentRun, db.get(GoalVersion, identifier).run_id)
        validate_research_run(db, actor, legacy_run)


@pytest.mark.parametrize("changed", ["question", "dimensions", "scope"])
def test_root_stage_cannot_change_original_question_or_research_scope(env, changed):
    app, actor = env["app"], env["actor"]
    manifest = {
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "question": "Explain the entire project",
    }
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "research", manifest, 1, "stage-tampering"
    )
    task = freeze_research_task(manifest["question"]).model_dump(mode="json")
    if changed == "question":
        task["original_question"] = "Only explain one small helper"
    elif changed == "dimensions":
        task["required_dimensions"] = []
    else:
        task["scope"] = "focused"
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        db.add(
            StageArtifact(
                run_id=run.id,
                stage_key="research_context",
                input_hash=run.input_hash,
                payload={"research_task": task},
            )
        )
    with app.db.read() as db, pytest.raises(DomainError) as error:
        validate_research_run(db, actor, db.get(AgentRun, run_id))
    assert error.value.code == "RESEARCH_CONTRACT"
