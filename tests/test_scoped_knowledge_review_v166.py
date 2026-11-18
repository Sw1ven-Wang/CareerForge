"""Synthetic protocol/security tests. No external model or native quality claims."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import RetrievalTaskSpec, TaskType
from careerforge.contracts_scoped_review_v166 import (
    REVIEW_READ_COMPLETION,
    SCOPED_REVIEW_VERSION,
    ScopedCoverageOutput,
    ScopedReviewOutput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    ArtifactDependency,
    Claim,
    Evidence,
    FactAtom,
    ModelReceipt,
    ObjectRow,
    Publication,
    ReviewDecision,
    SourceRead,
    SourceSet,
    StageArtifact,
    UnitVersion,
)
from careerforge.services.dependencies import invalidate
from careerforge.services.scoped_review import derive_scope, scoped_review_enabled
from careerforge.services.security import create_principal, set_grant
from careerforge.util import digest, fingerprint


async def partial_fixture(env, monkeypatch, *, alter=None, key="partial"):
    """New actual synthetic gateway receipts; never fabricates old native decisions."""
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        value = deepcopy(output.structured)
        role, context = request["role"], request["context"]
        if not request.get("schema"):
            return output
        if role == "analysis":
            unit = value["units"][0]
            unit["title"] = "Unreviewed universal validation guarantee"
            unit["limitations"] = ["UNSUPPORTED_UNIT_NARRATIVE must never leak into a partial pack"]
            bad = deepcopy(unit["claims"][0])
            bad.update(key="overstrong", statement="该函数总能验证所有输入并保证执行成功。")
            unit["claims"].append(bad)
        elif role == "reviewer":
            subject = context["subject"]
            good = next(c["claim_id"] for c in subject["claims"] if "返回传入" in c["statement"])
            bad = next(c["claim_id"] for c in subject["claims"] if c["claim_id"] != good)
            for item in value["claim_decisions"]:
                if item["claim_id"] == bad:
                    item["outcome"] = "partially_supported"
                    item["boundary"] = (
                        "A return statement is not validation or a runtime guarantee."
                    )
            value.update(decision="needs_evidence", blocking_issues=["Unsupported guarantee"])
            value["scope_review"] = {
                **subject["scope_identity"],
                "standalone_claim_ids": [good],
                "issue_scopes": [{"issue_index": 0, "scope": "claim", "target_ids": [bad]}],
                "global_blocking_issues": [],
            }
        elif role == "coverage" and "scope_binding" in context:
            elements = context["subject"]["scope_elements"]
            checks = []
            for element in elements:
                ok = "返回传入" in element["text"]
                checks.append(
                    {
                        **{k: element[k] for k in ("element_id", "claim_id", "fact_atom_id")},
                        "fact_bound": ok,
                        "conditions_preserved": ok,
                        "standalone": ok,
                        "reason": "Synthetic exact-return support"
                        if ok
                        else "Synthetic unsupported guarantee",
                    }
                )
            value.update(
                all_facts_bound=False,
                conditions_preserved=False,
                uncovered_elements=[e["element_id"] for e in checks if not e["fact_bound"]],
                unsupported_titles=[context["subject"]["title"]],
                scope_audit={
                    **context["scope_binding"],
                    "elements": checks,
                    "global_blocking_issues": [],
                },
            )
        if alter is not None:
            alter(role, context, value)
        return output.model_copy(update={"structured": value})

    monkeypatch.setattr(env["model"], "complete", complete)
    return await publish_fixture(env, key=key)


@pytest.mark.asyncio
async def test_partial_publication_preserves_originals_and_reaches_retrieval_and_tutor(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    source, pid, root = await partial_fixture(env, monkeypatch)
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, pid)
        pub = db.get(Publication, pid)
        version = db.get(UnitVersion, pub.unit_version_id)
        review = db.get(ReviewDecision, pub.review_id)
        original = deepcopy(db.get(ObjectRow, version.id).payload)
        saved_review = deepcopy(review.report)
        assert len(original["draft"]["claims"]) == 2
        assert len(version.render_ast) == 2
        assert review.decision == "accept_partial_scoped"
        assert review.report["scoped_original_review"]["decision"] == "needs_evidence"
        assert not review.report["fact_coverage"]["all_facts_bound"]
        assert not review.report["fact_coverage"]["conditions_preserved"]
        assert len(review.report["scope_derivation"]["quarantined_claims"]) == 1
        assert (
            db.get(AgentRun, root).input_manifest["knowledge_review_contract_version"]
            == SCOPED_REVIEW_VERSION
        )
    assert len(pack["facts"]) == 1
    assert pack["facts"][0]["text"] == original["draft"]["claims"][0]["statement"]
    assert pack["facts"][0]["conditions"] == original["draft"]["claims"][0]["conditions"]
    assert pack["facts"][0]["kind"] == "source_fact"
    assert pack["title"] != original["draft"]["title"]
    assert not pack["topic_links"] and not pack["facets"]
    assert "UNSUPPORTED_UNIT_NARRATIVE" not in str(pack)
    assert not pack["runtime_observed"] and not pack["publication_scope"]["task_satisfied"]
    result = app.runtime.stage(root, "result")
    assert result["task_coverage"]["status"] == "partial"
    assert not result["task_coverage"]["complete"]
    assert any(g.startswith("partial_claim_publication:") for g in result["gaps"])
    request = RetrievalTaskSpec(
        turn_id="partial-retrieval",
        task_type=TaskType.IMPLEMENTATION,
        user_question="函数返回传入 value",
        focus_revision=1,
        project_source_ids=[source],
        topic_keys=[],
        max_evidence_tokens=12000,
    )
    retrieved = await app.retrieval.retrieve(actor, request)
    assert [u["publication_id"] for u in retrieved["knowledge_units"]] == [pid]
    assert retrieved["knowledge_units"][0]["facts"] == pack["facts"]
    run = app.runtime.create(actor, env["ns"], env["funding"], "chat", {}, 4, "partial-tutor")
    epoch = app.runtime.claim(run, "partial-worker")
    blocks = await app.answers.tutor(
        actor,
        "函数返回传入 value",
        retrieved,
        {"teaching_skill": {"content": {"explanation_order": "concept_first"}}},
        {},
        (run, epoch, "partial-worker"),
    )
    assert blocks and any(b.get("text") == pack["facts"][0]["text"] for b in blocks)
    tutor = next(c for c in reversed(env["model"].calls) if c["role"] == "tutor")
    transmitted = str(tutor["context"])
    assert original["draft"]["claims"][1]["statement"] not in transmitted
    assert original["draft"]["title"] not in transmitted
    assert "UNSUPPORTED_UNIT_NARRATIVE" not in transmitted
    for role in ("reviewer", "coverage"):
        native_input = next(
            c["context"]
            for c in env["model"].calls
            if c["role"] == role and "subject" in c["context"]
        )
        view = next(
            view
            for view in native_input["subject"]["standalone_publication_views"]
            if view["facts"][0]["claim_id"] == pack["facts"][0]["claim_id"]
        )
        assert view["facts"] == pack["facts"]
        for field in ("title", "topic_links", "facets", "limitations", "runtime_observed"):
            assert view[field] == pack[field]
    with app.db.read() as db:
        assert db.get(ObjectRow, version.id).payload == original
        assert db.get(ReviewDecision, pub.review_id).report == saved_review


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "commit",
        "source",
        "version",
        "kind",
        "fact",
        "condition",
        "quote",
        "read_hash",
        "bundle",
        "receipt_unknown",
        "receipt_response",
        "receipt_schema",
        "receipt_payer",
        "receipt_profile",
        "review_missing_scope",
        "review_wrong_workflow",
        "review_contract_downgrade",
        "derivation",
        "fact_dependency",
    ],
)
async def test_partial_publication_revalidates_every_authority(env, monkeypatch, mutation):
    app, actor = env["app"], env["actor"]
    source, pid, _ = await partial_fixture(env, monkeypatch)
    with app.db.transaction() as db:
        pub = db.get(Publication, pid)
        v = db.get(UnitVersion, pub.unit_version_id)
        r = db.get(ReviewDecision, pub.review_id)
        cid = r.report["scope_derivation"]["retained_claim_ids"][0]
        fact = db.scalar(select(FactAtom).where(FactAtom.claim_id == cid))
        if mutation == "commit":
            db.get(SourceSet, source).commit_sha = "f" * 40
        elif mutation == "source":
            db.get(SourceSet, source).repository_key = "wrong/project"
        elif mutation == "version":
            r.reviewed_content_hash = "0" * 64
        elif mutation == "kind":
            db.get(Claim, cid).kind = "runtime_observation"
        elif mutation == "fact":
            fact.approved_text += " forged"
        elif mutation == "condition":
            fact.conditions = ["new condition"]
        elif mutation == "quote":
            e = db.scalar(select(Evidence))
            e.exact_quote = "forged exact quote"
            e.quote_hash = digest(e.exact_quote)
        elif mutation == "read_hash":
            db.scalar(select(SourceRead)).file_hash = "0" * 64
        elif mutation == "bundle":
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == r.reviewer_run_id,
                    StageArtifact.stage_key == "independent:" + v.id,
                )
            )
            stage.payload = {**stage.payload, "reads": []}
        elif mutation.startswith("receipt_"):
            receipt = db.get(ModelReceipt, r.report["scoped_receipts"][-1]["receipt_id"])
            if mutation == "receipt_unknown":
                receipt.status = "unknown"
            elif mutation == "receipt_schema":
                receipt.response = {**receipt.response, "structured": {"invalid": True}}
            elif mutation == "receipt_payer":
                receipt.payer_id = create_principal(db, "wrong-receipt-payer")[0].id
            elif mutation == "receipt_profile":
                receipt.profile_version = "wrong-model-profile"
            else:
                receipt.response = {**receipt.response, "usage": {"tampered": 1}}
        elif mutation == "review_missing_scope":
            changed = deepcopy(r.report)
            changed.pop("scoped_original_review")
            r.report = changed
            db.get(ObjectRow, r.id).payload = changed
            db.get(ObjectRow, r.id).content_hash = fingerprint(changed)
        elif mutation == "review_wrong_workflow":
            db.get(AgentRun, r.reviewer_run_id).workflow = "analysis"
        elif mutation == "review_contract_downgrade":
            run = db.get(AgentRun, r.reviewer_run_id)
            changed = dict(run.input_manifest)
            changed.pop("knowledge_review_contract_version")
            changed.pop("review_read_completion")
            run.input_manifest, run.input_hash = changed, fingerprint(changed)
        elif mutation == "derivation":
            payload = deepcopy(db.get(ObjectRow, pid).payload)
            payload["scope_derivation"]["retained_claim_ids"].append("forged")
            db.get(ObjectRow, pid).payload = payload
            db.get(ObjectRow, pid).content_hash = fingerprint(payload)
        elif mutation == "fact_dependency":
            edge = db.scalar(
                select(ArtifactDependency).where(
                    ArtifactDependency.dependent_id == pid,
                    ArtifactDependency.kind == "requires_fact",
                )
            )
            db.delete(edge)
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["source", "retained_support", "excluded_support", "review", "publication"]
)
async def test_partial_does_not_evade_revocation_of_original_authority(env, monkeypatch, target):
    app, actor = env["app"], env["actor"]
    source, pid, _ = await partial_fixture(env, monkeypatch)
    with app.db.transaction() as db:
        pub = db.get(Publication, pid)
        review = db.get(ReviewDecision, pub.review_id)
        if target in {"retained_support", "excluded_support"}:
            index = 0 if target == "retained_support" else 1
            identifier = review.report["claim_decisions"][index]["independent_evidence_ids"][0]
        else:
            identifier = {"source": source, "review": review.id, "publication": pid}[target]
        invalidate(db, actor, identifier, "revoked", "synthetic negative control")
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pid)


@pytest.mark.asyncio
async def test_partial_respects_shared_access_then_revoked_permission(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, pid, _ = await partial_fixture(env, monkeypatch)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "scoped-reader")
        oid = other.id
        set_grant(db, actor, env["ns"], oid, "reader", True)
    with app.db.read() as db:
        assert app.knowledge.publication_pack(db, oid, pid)["facts"]
    with app.db.transaction() as db:
        set_grant(db, actor, env["ns"], oid, "reader", False)
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, oid, pid)


def synthetic_inputs():
    subject = {
        "unit_version_id": "v",
        "source_id": "s",
        "source_commit": "a" * 40,
        "source_scope_hash": "b" * 64,
        "content_hash": "c" * 64,
        "render_hash": "d" * 64,
        "claims": [
            {"claim_id": c, "kind": "source_fact", "counterevidence_ids": []}
            for c in ("good", "bad")
        ],
        "elements": [
            {
                "element_id": "e-" + c,
                "claim_id": c,
                "fact_atom_id": "f-" + c,
                "text": c,
                "conditions": [],
            }
            for c in ("good", "bad")
        ],
        "topic_link_ids": ["topic"],
        "facet_ids": ["facet"],
    }
    identity = {k: subject[k] for k in ("unit_version_id", "content_hash", "render_hash")}
    review = {
        "claim_decisions": [
            {
                "claim_id": c,
                "outcome": "supported_within_scope" if c == "good" else "partially_supported",
                "independent_reads": [{"read_id": "read", "quote": "exact quote"}],
                "counterevidence": [],
                "boundary": "static",
            }
            for c in ("good", "bad")
        ],
        "accepted_topic_link_ids": ["topic"],
        "accepted_facet_ids": ["facet"],
        "decision": "needs_evidence",
        "blocking_issues": ["local defect"],
        "scope_review": {
            **identity,
            "standalone_claim_ids": ["good"],
            "issue_scopes": [{"issue_index": 0, "scope": "claim", "target_ids": ["bad"]}],
            "global_blocking_issues": [],
        },
    }
    r = ScopedReviewOutput.model_validate(review)
    coverage = {
        "all_facts_bound": False,
        "conditions_preserved": False,
        "uncovered_elements": ["e-bad"],
        "unsupported_titles": [],
        "rationale_codes": [],
        "scope_audit": {
            **identity,
            "reviewer_output_hash": fingerprint(r.model_dump(mode="json")),
            "global_blocking_issues": [],
            "elements": [
                {
                    **{k: e[k] for k in ("element_id", "claim_id", "fact_atom_id")},
                    "fact_bound": e["claim_id"] == "good",
                    "conditions_preserved": e["claim_id"] == "good",
                    "standalone": e["claim_id"] == "good",
                    "reason": "synthetic",
                }
                for e in subject["elements"]
            ],
        },
    }
    return subject, review, coverage


@pytest.mark.parametrize(
    "change",
    [
        "unattributed_review",
        "global_review",
        "global_coverage",
        "unit_reject",
        "missing_scope_review",
        "missing_scope_audit",
        "counterevidence",
        "analyst_counterevidence",
        "not_standalone",
        "essential_condition",
    ],
)
def test_unsafe_claims_or_global_issues_cannot_be_salvaged(change):
    subject, r, c = synthetic_inputs()
    if change == "unattributed_review":
        r["scope_review"]["issue_scopes"] = [{"issue_index": 0, "scope": "unit", "target_ids": []}]
    elif change == "global_review":
        r["scope_review"]["global_blocking_issues"] = ["wrong authority"]
    elif change == "global_coverage":
        c["scope_audit"]["global_blocking_issues"] = ["unattributed counterevidence"]
    elif change == "unit_reject":
        r["decision"] = "reject"
    elif change == "missing_scope_review":
        r.pop("scope_review")
    elif change == "missing_scope_audit":
        c.pop("scope_audit")
    elif change == "counterevidence":
        r["claim_decisions"][0]["counterevidence"] = [{"read_id": "r", "quote": "counter quote"}]
    elif change == "analyst_counterevidence":
        subject["claims"][0]["counterevidence_ids"] = ["counter"]
    elif change == "not_standalone":
        c["scope_audit"]["elements"][0]["standalone"] = False
    elif change == "essential_condition":
        c["scope_audit"]["elements"][0]["conditions_preserved"] = False
        c["uncovered_elements"].append("e-good")
    review = ScopedReviewOutput.model_validate(r)
    if "scope_audit" in c:
        c["scope_audit"]["reviewer_output_hash"] = fingerprint(review.model_dump(mode="json"))
    partition = derive_scope(subject, review, ScopedCoverageOutput.model_validate(c))
    assert partition["status"] == "blocked"
    assert not partition["retained_claim_ids"]


@pytest.mark.parametrize(
    "change",
    [
        "aggregate",
        "empty_uncovered",
        "unknown_element",
        "duplicate_element",
        "wrong_claim",
        "wrong_fact",
        "wrong_version",
        "wrong_hash",
        "wrong_review_hash",
        "unscoped_issue",
        "unknown_scope_target",
        "duplicate_positive",
    ],
)
def test_scope_and_subset_tampering_fails_closed(change):
    subject, r, c = synthetic_inputs()
    if change == "aggregate":
        c["all_facts_bound"] = True
    elif change == "empty_uncovered":
        c["uncovered_elements"] = []
    elif change == "unknown_element":
        c["uncovered_elements"] = ["unknown"]
    elif change == "duplicate_element":
        c["scope_audit"]["elements"][1] = c["scope_audit"]["elements"][0]
    elif change == "wrong_claim":
        c["scope_audit"]["elements"][0]["claim_id"] = "bad"
    elif change == "wrong_fact":
        c["scope_audit"]["elements"][0]["fact_atom_id"] = "f-bad"
    elif change == "wrong_version":
        r["scope_review"]["unit_version_id"] = "wrong"
    elif change == "wrong_hash":
        r["scope_review"]["content_hash"] = "wrong"
    elif change == "wrong_review_hash":
        c["scope_audit"]["reviewer_output_hash"] = "wrong"
    elif change == "unscoped_issue":
        r["scope_review"]["issue_scopes"] = []
    elif change == "unknown_scope_target":
        r["scope_review"]["issue_scopes"][0]["target_ids"] = ["another-version-claim"]
    elif change == "duplicate_positive":
        r["scope_review"]["standalone_claim_ids"] = ["good", "good"]
    review = ScopedReviewOutput.model_validate(r)
    if change != "wrong_review_hash":
        c["scope_audit"]["reviewer_output_hash"] = fingerprint(review.model_dump(mode="json"))
    with pytest.raises(DomainError):
        derive_scope(subject, review, ScopedCoverageOutput.model_validate(c))


def test_annotations_never_reused_and_changed_claim_not_approved_by_old_review():
    subject, r, c = synthetic_inputs()
    review = ScopedReviewOutput.model_validate(r)
    result = derive_scope(subject, review, ScopedCoverageOutput.model_validate(c))
    assert result["retained_claim_ids"] == ["good"]
    assert result["quarantined_topic_link_ids"] == ["topic"]
    assert result["quarantined_facet_ids"] == ["facet"]
    changed = deepcopy(subject)
    changed["content_hash"] = "new claim version"
    with pytest.raises(DomainError):
        derive_scope(changed, review, ScopedCoverageOutput.model_validate(c))


def test_marker_is_strict_and_legacy_branch_is_explicit():
    from pydantic import ValidationError

    from careerforge.contracts import KnowledgeBuildInput

    with pytest.raises(ValidationError):
        KnowledgeBuildInput.model_validate(
            {
                "namespace_id": "n",
                "funding_id": "f",
                "question": "q",
                "idempotency_key": "k",
                "knowledge_review_contract_version": "legacy",
            }
        )
    assert not scoped_review_enabled({})
    assert scoped_review_enabled(
        {
            "knowledge_review_contract_version": SCOPED_REVIEW_VERSION,
            "review_read_completion": REVIEW_READ_COMPLETION,
        }
    )
    for marker in (None, "legacy", "future"):
        with pytest.raises(DomainError):
            scoped_review_enabled(
                {
                    "knowledge_review_contract_version": marker,
                    "review_read_completion": REVIEW_READ_COMPLETION,
                }
            )


@pytest.mark.asyncio
async def test_planning_project_projection_and_update_preview_cannot_rehydrate_exclusions(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    source, pid, root = await partial_fixture(env, monkeypatch)
    result = app.runtime.stage(root, "result")
    assert len(result["knowledge_units"]) == 1
    projected = result["knowledge_units"][0]
    assert len(projected["facts"]) == 1
    assert len(projected["reviewed_claims"]) == 1
    assert not projected["topic_links"] and not projected["facets"]
    assert "总能验证" not in str(projected)
    assert "Unreviewed universal" not in str(projected)
    plan = app.learning.propose_plan(actor, [pid], "function", "Partial facts study")
    with app.db.read() as db:
        module = db.get(ObjectRow, plan["module_ids"][0]).payload
        assert module["topic_keys"] == []
        assert not module["competence_verified"]
        assert "Unreviewed universal" not in module["title"]
        update = app.lifecycle._update_manifest(db, actor, source, source, env["ns"])
    unit = update["units"][0]
    assert len(unit["claims_to_recheck"]) == 1
    assert "总能验证" not in str(unit)
    assert unit["publication_scope"]["status"] == "partial"
    assert not unit["publication_scope"]["whole_unit_complete"]
    anchors, units = app.build._published_evidence(actor, [pid])
    assert len(anchors) == 1
    assert "总能验证" not in str(anchors)
    assert units[0]["publication_scope"]["status"] == "partial"


@pytest.mark.parametrize(
    "reviewed_key,expected_question_complete", [("kept", True), ("quarantined", False)]
)
def test_exact_question_sufficiency_is_independent_of_whole_unit_partial_status(
    reviewed_key, expected_question_complete
):
    from careerforge.services.research_coverage import evaluate_task_coverage
    from careerforge.services.scoped_review import mark_partial_publication_coverage

    # Explicit synthetic semantic-contract inputs, not real review evidence.
    anchor = {
        "source_id": "s",
        "subject_key": "subject",
        "claim_key": "kept",
        "claim_id": "c",
        "publication_id": "p",
        "unit_version_id": "v",
        "statement": "Returns its input.",
        "conditions": ["Static source only."],
        "kind": "source_fact",
    }
    assessment = {
        "question": {
            "status": "known",
            "explanation": "Synthetic exact-question assessment",
            "anchors": [{"subject_key": "subject", "claim_keys": [reviewed_key]}],
        }
    }
    coverage = evaluate_task_coverage(
        {"original_question": "What value is returned?", "scope": "focused"},
        [{"source_id": "s", "coverage": assessment}],
        [{"source_id": "s", "unit_version_ids": ["v"], "coverage": assessment}],
        [anchor],
    )
    assert coverage["question"]["complete"] is expected_question_complete
    original = deepcopy(coverage)
    marked = mark_partial_publication_coverage(
        coverage, [{"publication_scope": {"status": "partial"}}]
    )
    assert marked["status"] == "partial" and not marked["complete"]
    assert marked["question"] == original["question"]
    assert marked["question"]["complete"] is expected_question_complete
    assert coverage == original


def test_relation_only_rejection_does_not_discard_independent_facts():
    subject, raw, coverage = synthetic_inputs()
    for decision in raw["claim_decisions"]:
        decision["outcome"] = "supported_within_scope"
    raw["scope_review"]["standalone_claim_ids"] = ["good", "bad"]
    raw["scope_review"]["issue_scopes"] = [
        {"issue_index": 0, "scope": "topic_link", "target_ids": ["topic"]}
    ]
    raw["accepted_topic_link_ids"] = []
    for element in coverage["scope_audit"]["elements"]:
        element.update(fact_bound=True, conditions_preserved=True, standalone=True)
    coverage.update(all_facts_bound=True, conditions_preserved=True, uncovered_elements=[])
    review = ScopedReviewOutput.model_validate(raw)
    coverage["scope_audit"]["reviewer_output_hash"] = fingerprint(review.model_dump(mode="json"))
    result = derive_scope(subject, review, ScopedCoverageOutput.model_validate(coverage))
    assert result["retained_claim_ids"] == ["good", "bad"]
    assert result["quarantined_topic_link_ids"] == ["topic"]
    assert not result["whole_unit_complete"]


@pytest.mark.parametrize("rejecting_role", ["reviewer", "coverage"])
def test_supported_claim_depending_on_removed_premise_is_quarantined(rejecting_role):
    subject, raw, coverage = synthetic_inputs()
    subject["elements"][0]["text"] = "Therefore this behavior guarantees isolation."
    # Nominal original support is not standalone support. The referent/premise
    # lived in the removed unit narrative or the rejected second claim.
    assert raw["claim_decisions"][0]["outcome"] == "supported_within_scope"
    if rejecting_role == "reviewer":
        raw["scope_review"]["standalone_claim_ids"] = []
    else:
        coverage["scope_audit"]["elements"][0]["standalone"] = False
        coverage["scope_audit"]["elements"][0]["reason"] = "Depends on a removed premise."
    review = ScopedReviewOutput.model_validate(raw)
    coverage["scope_audit"]["reviewer_output_hash"] = fingerprint(review.model_dump(mode="json"))
    partition = derive_scope(subject, review, ScopedCoverageOutput.model_validate(coverage))
    assert partition["status"] == "blocked"
    assert not partition["retained_claim_ids"]
    assert {c["claim_id"] for c in partition["quarantined_claims"]} == {"good", "bad"}
    assert subject["elements"][0]["text"] == "Therefore this behavior guarantees isolation."


@pytest.mark.asyncio
async def test_coherently_rehashed_derived_subset_cannot_override_genuine_verdicts(
    env, monkeypatch
):
    """Privileged-storage corruption control, not an ordinary client capability.

    Rewrite every subset copy/hash and its fact edge coherently, but leave the
    original admission, candidate, actual model receipts and verdicts unchanged.
    Publication consumption must recompute authority, not trust self-hashes.
    """
    from careerforge.services.dependencies import depend

    app, actor = env["app"], env["actor"]
    _, pid, root = await partial_fixture(env, monkeypatch)
    with app.db.transaction() as db:
        pub = db.get(Publication, pid)
        version = db.get(UnitVersion, pub.unit_version_id)
        review = db.get(ReviewDecision, pub.review_id)
        receipts_before = {
            receipt.id: deepcopy(receipt.response)
            for receipt in db.scalars(
                select(ModelReceipt).where(ModelReceipt.run_id == review.reviewer_run_id)
            )
        }
        original_review = deepcopy(review.report["scoped_original_review"])
        original_coverage = deepcopy(review.report["fact_coverage"])
        forged = deepcopy(review.report["scope_derivation"])
        excluded = forged["quarantined_claims"].pop()
        claim_id = excluded["claim_id"]
        fact = db.scalar(select(FactAtom).where(FactAtom.claim_id == claim_id))
        forged["retained_claim_ids"].append(claim_id)
        forged["retained_fact_ids"].append(fact.id)
        forged["retained_element_ids"].append(excluded["element_id"])
        forged["derivation_hash"] = fingerprint(
            {key: value for key, value in forged.items() if key != "derivation_hash"}
        )
        report = {**review.report, "scope_derivation": forged}
        review.report = report
        review_object = db.get(ObjectRow, review.id)
        review_object.payload, review_object.content_hash = report, fingerprint(report)
        pub_object = db.get(ObjectRow, pid)
        pub_payload = {**pub_object.payload, "scope_derivation": forged}
        pub_object.payload, pub_object.content_hash = pub_payload, fingerprint(pub_payload)
        depend(db, actor, pid, fact.id, "requires_fact")
        assert fingerprint(version.render_ast) == version.render_hash
        assert review.report["scoped_original_review"] == original_review
        assert review.report["fact_coverage"] == original_coverage
    with app.db.read() as db, pytest.raises(DomainError) as error:
        app.knowledge.publication_pack(db, actor, pid)
    assert error.value.code == "SCOPED_REVIEW_BINDING"
    with pytest.raises(DomainError):
        app.build._published_evidence(actor, [pid])
    with app.db.read() as db, pytest.raises(DomainError):
        app.build.checkpoint_in(db, actor, db.get(AgentRun, root))
    with app.db.read() as db:
        assert {
            receipt.id: receipt.response
            for receipt in db.scalars(
                select(ModelReceipt).where(ModelReceipt.run_id == review.reviewer_run_id)
            )
        } == receipts_before
