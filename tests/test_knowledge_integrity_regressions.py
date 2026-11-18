"""Deterministic security regressions; no external model or service calls."""

from datetime import timedelta

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import RetrievalTaskSpec, TaskType
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Evidence,
    Publication,
    ReviewDecision,
    SelectionHead,
    SourceSet,
    TopicLink,
    UnitVersion,
)
from careerforge.services.dependencies import invalidate
from careerforge.util import digest, now


def retrieval_spec(**kwargs):
    return RetrievalTaskSpec(
        turn_id="integrity",
        task_type=TaskType.IMPLEMENTATION,
        user_question="Tool Calling",
        focus_revision=1,
        topic_keys=["agent.tool_calling"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_independent_review_evidence_revocation_blocks_publication(env):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        review = db.get(ReviewDecision, db.get(Publication, pub_id).review_id)
        evidence_id = review.report["claim_decisions"][0]["independent_evidence_ids"][0]
        invalidate(db, actor, evidence_id, "revoked", "independent support withdrawn")
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["quote", "position", "read"])
async def test_evidence_relational_projection_cannot_override_frozen_binding(env, field):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        evidence = db.scalar(select(Evidence))
        if field == "quote":
            evidence.exact_quote = "forged safety guarantee"
            evidence.quote_hash = digest(evidence.exact_quote)
        elif field == "position":
            evidence.start_line = evidence.end_line = 1
        else:
            other = db.scalar(select(Evidence).where(Evidence.read_id != evidence.read_id))
            evidence.read_id = other.read_id
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["decision", "report", "bundle"])
async def test_publication_requires_unchanged_review_projection(env, field):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        review = db.get(ReviewDecision, db.get(Publication, pub_id).review_id)
        if field == "decision":
            review.decision = "reject"
        elif field == "report":
            review.report = {**review.report, "blocking_issues": ["unresolved"]}
        else:
            review.independent_bundle_hash = "0" * 64
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "limitations", "synthetic"])
async def test_unit_metadata_cannot_drift_after_review(env, field):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        version = db.get(UnitVersion, db.get(Publication, pub_id).unit_version_id)
        setattr(
            version,
            field,
            {"title": "Unreviewed guarantee", "limitations": [], "synthetic": False}[field],
        )
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["commit_sha", "repository_key", "scope_hash", "license_id"])
async def test_source_identity_is_bound_to_frozen_payload(env, field):
    app, actor = env["app"], env["actor"]
    source_id, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        setattr(
            db.get(SourceSet, source_id),
            field,
            {
                "commit_sha": "f" * 40,
                "repository_key": "forged/repository",
                "scope_hash": "0" * 64,
                "license_id": "Unreviewed-license",
            }[field],
        )
    with app.db.read() as db, pytest.raises(DomainError):
        app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
async def test_pinned_publication_cannot_widen_explicit_source_scope(env):
    app, actor = env["app"], env["actor"]
    source_id, _, _ = await publish_fixture(env, key="a", repository="fixtures/a")
    _, wrong_pub, _ = await publish_fixture(env, key="b", repository="fixtures/b")
    with pytest.raises(DomainError) as error:
        await app.retrieval.retrieve(
            actor,
            retrieval_spec(project_source_ids=[source_id], pinned_publication_ids=[wrong_pub]),
        )
    assert error.value.code == "PUBLICATION_SCOPE"


@pytest.mark.asyncio
async def test_revoked_selection_head_is_not_a_default_but_pin_still_valid(env):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        head = db.scalar(select(SelectionHead).where(SelectionHead.publication_id == pub_id))
        invalidate(db, actor, head.id, "revoked", "retired selection")
    assert not (await app.retrieval.retrieve(actor, retrieval_spec()))["knowledge_units"]
    with app.db.read() as db:
        assert app.knowledge.publication_pack(db, actor, pub_id)


@pytest.mark.asyncio
async def test_changed_topic_projection_is_not_treated_as_approved(env):
    app, actor = env["app"], env["actor"]
    _, pub_id, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        link = db.scalar(select(TopicLink))
        link.relation = "implements"
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, pub_id)
    assert pack["topic_links"] == []


@pytest.mark.asyncio
async def test_cache_cannot_inject_different_project_or_navigation(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source_id, pub_id, _ = await publish_fixture(env, key="a", repository="fixtures/a")
    wrong_source, wrong_pub, _ = await publish_fixture(env, key="b", repository="fixtures/b")

    async def poisoned(layer, key, loader, ttl=30, *, publish_guard=None):
        return {
            "ids": [pub_id, wrong_pub],
            "candidate_count": 999,
            "total_groups": 999,
            "sampled_groups": [source_id, wrong_source],
            "trace": {
                "ast_symbol": [
                    {
                        "source_id": wrong_source,
                        "path": "fake.py",
                        "symbol": "forged",
                        "start_line": 1,
                        "end_line": 1,
                    }
                ]
            },
        }

    monkeypatch.setattr(app.cache, "singleflight", poisoned)
    result = await app.retrieval.retrieve(actor, retrieval_spec(project_source_ids=[source_id]))
    assert [u["publication_id"] for u in result["knowledge_units"]] == [pub_id]
    assert result["sampled_source_ids"] == [source_id]
    assert result["symbols"] == []
    assert result["candidate_count"] == result["total_project_groups"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed", "running"])
@pytest.mark.parametrize("child_role", ["analysis", "review"])
async def test_publisher_requires_successful_children_even_on_replay(env, status, child_role):
    app, actor = env["app"], env["actor"]
    _, pub_id, root_id = await publish_fixture(env)
    with app.db.transaction() as db:
        publication = db.get(Publication, pub_id)
        review = db.get(ReviewDecision, publication.review_id)
        version = db.get(UnitVersion, publication.unit_version_id)
        child_id = review.reviewer_run_id if child_role == "review" else version.analysis_run_id
        db.get(AgentRun, child_id).status = status
        root = db.get(AgentRun, root_id)
        root.status, root.worker_id = "running", "publication-regression"
        root.lease_until = now() + timedelta(minutes=1)
        epoch, review_id = root.epoch, review.id
    with pytest.raises(DomainError) as error:
        app.knowledge.publish(actor, root_id, epoch, "publication-regression", review_id)
    assert error.value.code == "PUBLICATION_AUTHORITY"


@pytest.mark.asyncio
async def test_completed_analysis_and_review_replay_still_requires_current_lease(env):
    app, actor = env["app"], env["actor"]
    source_id, pub_id, _ = await publish_fixture(env)
    with app.db.read() as db:
        publication = db.get(Publication, pub_id)
        review = db.get(ReviewDecision, publication.review_id)
        version = db.get(UnitVersion, publication.unit_version_id)
        analysis_id, review_run_id, version_id = (
            version.analysis_run_id,
            review.reviewer_run_id,
            version.id,
        )
    # Already persisted stage must not leak its IDs to a stale worker before lease checks.
    with pytest.raises(DomainError):
        app.knowledge.save_analysis(actor, analysis_id, 0, "stale", source_id, "unused", None, True)
    with pytest.raises(DomainError):
        app.knowledge.save_review(
            actor, review_run_id, 0, "stale", version_id, "unused", {}, None, None
        )


@pytest.mark.asyncio
async def test_review_stage_retains_aggregate_decision_string(env):
    from conftest import publish_fixture

    from careerforge.models import Publication, ReviewDecision

    _, publication_id, _ = await publish_fixture(env, key="review-stage-shape")
    with env["app"].db.read() as db:
        publication = db.get(Publication, publication_id)
        review = db.get(ReviewDecision, publication.review_id)
        stage = env["app"].runtime.stage(review.reviewer_run_id, "review:" + review.unit_version_id)
        assert stage["decision"] == review.decision == "accept_scoped"
        assert isinstance(stage["decision"], str)
