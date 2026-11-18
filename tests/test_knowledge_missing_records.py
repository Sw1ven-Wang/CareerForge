"""Typed projection failures must remain closed, not escape as AttributeError."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FILES

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.errors import DomainError
from careerforge.models import Claim, Publication, ReviewDecision, Topic
from careerforge.services.dependencies import register


def test_selection_rejects_a_publication_object_without_its_record(env):
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        missing = register(db, actor, env["ns"], "publication", {}).id
    with pytest.raises(DomainError) as error:
        app.knowledge.select_head(actor, env["ns"], missing, 0)
    assert error.value.code == "ARTIFACT_MISSING"


def test_publishing_rejects_a_review_object_without_its_record(env):
    app, actor = env["app"], env["actor"]
    root = app.runtime.create(
        actor, env["ns"], env["funding"], "knowledge_build", {}, 1, "missing-review"
    )
    epoch = app.runtime.claim(root, "projection-worker")
    with app.db.transaction() as db:
        missing = register(db, actor, env["ns"], "review_decision", {}).id
    with pytest.raises(DomainError) as error:
        app.knowledge.publish(actor, root, epoch, "projection-worker", missing)
    assert error.value.code == "ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_selection_does_not_accept_other_dependency_object_types(env):
    source, _, _ = await publish_fixture(env)
    with pytest.raises(DomainError) as error:
        env["app"].knowledge.select_head(env["actor"], env["ns"], source, 0)
    assert error.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_missing_claim_projection_is_a_fact_coverage_error(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        original_get = db.get

        def missing_claim(model, identifier, **kwargs):
            return None if model is Claim else original_get(model, identifier, **kwargs)

        monkeypatch.setattr(db, "get", missing_claim)
        with pytest.raises(DomainError) as error:
            app.knowledge.publication_pack(db, actor, publication)
        assert error.value.code == "FACT_COVERAGE"


@pytest.mark.asyncio
async def test_missing_optional_topic_projection_is_omitted(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        original_get = db.get

        def missing_topic(model, identifier, **kwargs):
            return None if model is Topic else original_get(model, identifier, **kwargs)

        monkeypatch.setattr(db, "get", missing_topic)
        packet = app.knowledge.publication_pack(db, actor, publication)
        assert packet["facts"] and packet["topic_links"] == []


@pytest.mark.asyncio
async def test_missing_selected_publication_projection_is_not_returned(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    with app.db.read() as db:
        original_get = db.get

        def missing_publication(model, identifier, **kwargs):
            return None if model is Publication else original_get(model, identifier, **kwargs)

        monkeypatch.setattr(db, "get", missing_publication)
        assert app.knowledge.selected_publications(db, actor) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accept_scoped", "reject", "needs_evidence"])
async def test_review_outcome_remains_a_string_after_evidence_dependency_loop(env, outcome):
    app, actor, model = env["app"], env["actor"], env["model"]
    original_complete = model.complete

    async def complete(request):
        response = await original_complete(request)
        if request["role"] == "reviewer" and request.get("schema"):
            payload = deepcopy(response.structured)
            payload["decision"] = outcome
            # This regression checks strict whole-unit decision propagation,
            # not the separately versioned per-claim salvage path. A positive
            # standalone audit would authorize a partial result for needs_evidence.
            payload["scope_review"] = None
            return response.model_copy(update={"structured": payload})
        if (
            request["role"] == "coverage"
            and request.get("schema")
            and "scope_binding" in request["context"]
        ):
            payload = deepcopy(response.structured)
            payload["scope_audit"] = None
            return response.model_copy(update={"structured": payload})
        return response

    model.complete = complete
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixture/review-outcome",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    root = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source,
            question="Tool Calling",
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="review-outcome",
        ),
    )
    await app.drain()
    with app.db.read() as db:
        review = db.scalar(select(ReviewDecision))
        assert review is not None
        assert review.decision == review.report["decision"] == outcome
        saved = app.runtime.stage(review.reviewer_run_id, "review:" + review.unit_version_id)
        assert saved["decision"] == outcome
    result = app.runtime.stage(root, "result")
    assert bool(result["publication_ids"]) is (outcome == "accept_scoped")
