"""Opt-in real PostgreSQL/pgvector + Redis; synthetic model vectors, never live AI.

Run only against an explicitly provisioned disposable database via
validation/current/native-infrastructure/native_harness.py (or equivalent).
Normal offline pytest skips this test. It does not drop any database/schema.
"""

import asyncio
import os

import pytest
from conftest import publish_fixture
from support import FixtureModel, FixtureRepositories

from careerforge.adapters.cache import RedisCache
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import RetrievalTaskSpec, TaskType
from careerforge.models import Publication, ReviewDecision
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal, set_grant


@pytest.mark.production
@pytest.mark.skipif(
    os.environ.get("CAREERFORGE_NATIVE_KNOWLEDGE_TEST") != "1",
    reason="requires explicitly authorized disposable PostgreSQL/Redis",
)
@pytest.mark.asyncio
async def test_real_postgres_vector_fts_redis_and_authority_barriers(tmp_path):
    url = os.environ["CAREERFORGE_TEST_DATABASE_URL"]
    assert url.startswith("postgresql")
    model = FixtureModel()
    model.supports_embeddings = True
    cache = RedisCache(os.environ["CAREERFORGE_REDIS_CACHE_URL"])
    settings = Settings(
        mode="test",
        database_url=url,
        allow_fixture_publication=True,
        embedding_dimensions=8,
        state_dir=tmp_path,
    )
    app = Application(settings, model, FixtureRepositories(), cache=cache)
    try:
        owner = app.bootstrap("knowledge-postgres-fixture")
        actor, namespace = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, namespace, 1000)
        env = {"app": app, "actor": actor, "ns": namespace, "funding": funding, "owner": owner}
        private_source, private_pub, _ = await publish_fixture(
            env, key="private", repository="fixtures/private"
        )
        _, public_pub, _ = await publish_fixture(
            env, key="public", repository="fixtures/public", namespace=owner["public_namespace_id"]
        )
        with app.db.transaction() as db:
            reader, _, _ = create_principal(db, "knowledge-postgres-reader")
            reader_id = reader.id

        def spec(turn, sources=None):
            return RetrievalTaskSpec(
                turn_id=turn,
                task_type=TaskType.IMPLEMENTATION,
                user_question="Tool Calling return value",
                focus_revision=1,
                topic_keys=["agent.tool_calling"],
                project_source_ids=sources or [],
            )

        run_id = app.runtime.create(
            actor,
            namespace,
            funding,
            "research",
            {"model_profile": model.profile_version},
            6,
            "vector-retrieve",
        )
        epoch = app.runtime.claim(run_id, "native-knowledge-worker")
        run = (run_id, epoch, "native-knowledge-worker")
        before = sum(call["role"] == "embedding" for call in model.calls)
        first, second = await asyncio.gather(
            app.retrieval.retrieve(actor, spec("a", [private_source]), run=run),
            app.retrieval.retrieve(actor, spec("b", [private_source]), run=run),
        )
        assert sum(call["role"] == "embedding" for call in model.calls) == before + 1
        for result in (first, second):
            assert "pgvector" in result["plan"]["allowed_channels"]
            assert [unit["publication_id"] for unit in result["knowledge_units"]] == [private_pub]
        app.runtime.finish(*run, "succeeded")
        metrics = await cache.redis.hgetall("cf15:cache_metrics")
        assert int(metrics.get("base.hit", "0")) >= 1

        visible = await app.retrieval.retrieve(reader_id, spec("reader-public"))
        assert {unit["publication_id"] for unit in visible["knowledge_units"]} == {public_pub}
        with app.db.transaction() as db:
            set_grant(db, actor, namespace, reader_id, "reader", True)
        granted = await app.retrieval.retrieve(reader_id, spec("reader-granted"))
        assert {unit["publication_id"] for unit in granted["knowledge_units"]} == {
            private_pub,
            public_pub,
        }
        # Warm a real Redis entry before the grant and evidence are withdrawn.
        await app.retrieval.retrieve(reader_id, spec("reader-hot"))
        with app.db.transaction() as db:
            set_grant(db, actor, namespace, reader_id, "reader", False)
        revoked = await app.retrieval.retrieve(reader_id, spec("reader-revoked"))
        assert {unit["publication_id"] for unit in revoked["knowledge_units"]} == {public_pub}
        with app.db.transaction() as db:
            review = db.get(ReviewDecision, db.get(Publication, public_pub).review_id)
            evidence = review.report["claim_decisions"][0]["independent_evidence_ids"][0]
            invalidate(db, actor, evidence, "revoked", "native reviewer-evidence regression")
        final = await app.retrieval.retrieve(reader_id, spec("reader-evidence-withdrawn"))
        assert final["knowledge_units"] == []
        # Real PostgreSQL FTS was exercised as well as the vector SQL path.
        with app.db.read() as db:
            assert app.retrieval._lexical(db, [private_pub], ["return", "value"]) == [private_pub]
    finally:
        await cache.redis.aclose()
        app.db.engine.dispose()
