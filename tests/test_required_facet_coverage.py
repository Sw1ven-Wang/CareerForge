"""Blocking facet edges require a surviving, valid projection on the reviewed version."""

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_knowledge_lifecycle_regressions import publish_same_named_facets

from careerforge.errors import DomainError
from careerforge.models import ArtifactDependency, FacetValue, ObjectRow, Publication, Topic
from careerforge.services.dependencies import check_closure


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["deleted", "other_version"])
async def test_missing_required_facet_projection_blocks_publication(env, corruption):
    app, actor = env["app"], env["actor"]
    publication_id, original_pack, topics = await publish_same_named_facets(env, required=True)
    assert len(original_pack["knowledge_units"][0]["facets"]) == 2
    other_publication = None
    if corruption == "other_version":
        # A second genuinely reviewed version has no facets to receive this projection.
        env["model"].overrides.pop("analysis")
        _, other_publication, _ = await publish_fixture(
            env, key="different-version", repository="fixtures/other-version"
        )
    with app.db.transaction() as db:
        publication = db.get(Publication, publication_id)
        assert publication is not None
        facet = db.scalar(
            select(FacetValue)
            .join(Topic, Topic.id == FacetValue.topic_id)
            .where(
                FacetValue.unit_version_id == publication.unit_version_id, Topic.key == topics[0]
            )
        )
        assert facet is not None
        facet_id = facet.id
        if corruption == "deleted":
            db.delete(facet)
        else:
            other = db.get(Publication, other_publication)
            assert other is not None
            facet.unit_version_id = other.unit_version_id
    with app.db.read() as db:
        # The authoritative object and blocking dependency still exist. The missing
        # projection must not silently pass just because closure traversal succeeds.
        check_closure(db, actor, [publication_id])
        assert db.get(ObjectRow, facet_id) is not None
        assert (
            db.scalar(
                select(ArtifactDependency.id).where(
                    ArtifactDependency.dependent_id == publication_id,
                    ArtifactDependency.source_id == facet_id,
                    ArtifactDependency.kind == "uses_facet",
                    ArtifactDependency.enforcement == "blocking",
                )
            )
            is not None
        )
        with pytest.raises(DomainError) as error:
            app.knowledge.publication_pack(db, actor, publication_id)
        assert error.value.code == "FACET_COVERAGE"
        if other_publication:
            assert app.knowledge.publication_pack(db, actor, other_publication)["facets"] == []


@pytest.mark.asyncio
async def test_missing_optional_facet_projection_does_not_block_remaining_facts(env):
    app, actor = env["app"], env["actor"]
    publication_id, _, topics = await publish_same_named_facets(env)
    with app.db.transaction() as db:
        publication = db.get(Publication, publication_id)
        assert publication is not None
        facet = db.scalar(
            select(FacetValue)
            .join(Topic, Topic.id == FacetValue.topic_id)
            .where(
                FacetValue.unit_version_id == publication.unit_version_id, Topic.key == topics[0]
            )
        )
        assert facet is not None
        facet_id = facet.id
        db.delete(facet)
    with app.db.read() as db:
        assert db.get(ObjectRow, facet_id) is not None
        packet = app.knowledge.publication_pack(db, actor, publication_id)
        assert packet["facts"]
        assert [facet["topic_key"] for facet in packet["facets"]] == [topics[1]]
