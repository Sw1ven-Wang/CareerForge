"""Explicitly gated native PG/pgvector/Redis control-flow tests, not Docker/live AI."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FixtureModel, FixtureRepositories

from careerforge.adapters.cache import RedisCache
from careerforge.adapters.github import GitHubProvider
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_retrieval_ext import IndexJobInput
from careerforge.errors import DomainError
from careerforge.models import KnowledgeEmbedding, Namespace
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.privacy import assert_personal_access

pytestmark = [
    pytest.mark.production,
    pytest.mark.skipif(
        os.environ.get("CAREERFORGE_NATIVE_RETRIEVAL_V16") != "1",
        reason="requires explicit disposable native PostgreSQL/Redis harness",
    ),
]


@pytest.fixture
def native(tmp_path):
    url = os.environ["CAREERFORGE_TEST_DATABASE_URL"]
    assert "_validation_" in url and url.startswith("postgresql")
    model = FixtureModel()
    app = Application(
        Settings(
            mode="test",
            database_url=url,
            allow_fixture_publication=True,
            embedding_dimensions=8,
            state_dir=tmp_path,
        ),
        model,
        FixtureRepositories(),
    )
    app.db.create_schema()
    with app.db.read() as db:
        public = db.scalar(select(Namespace).where(Namespace.kind == "shared_public"))
    if public is None:
        owner = app.bootstrap("native-retrieval-" + uuid4().hex)
    else:
        from careerforge.services.security import create_principal

        with app.db.transaction() as db:
            principal, private, token = create_principal(
                db, "native-retrieval-" + uuid4().hex, admin=True
            )
            owner = {
                "principal_id": principal.id,
                "namespace_id": private.id,
                "public_namespace_id": public.id,
                "token": token,
            }
    actor, namespace = owner["principal_id"], owner["namespace_id"]
    funding = app.runtime.create_funding(actor, namespace, 500)
    value = {
        "app": app,
        "actor": actor,
        "ns": namespace,
        "funding": funding,
        "model": model,
        "owner": owner,
    }
    yield value
    app.db.engine.dispose()


def test_real_pg_cross_worker_atomic_request_budget_and_serial_bucket(native):
    app = native["app"]
    runs = []
    for index in range(8):
        funding = app.runtime.create_funding(native["actor"], native["ns"], 2)
        runs.append(
            app.runtime.create(
                native["actor"],
                native["ns"],
                funding,
                "research",
                {"github_request_cap": 1},
                1,
                "atomic-" + str(index),
            )
        )
    settings = replace(app.settings, github_token="synthetic-credential-" + uuid4().hex)
    barrier = threading.Barrier(len(runs))

    def attempt(identifier):
        barrier.wait(timeout=5)
        try:
            return GitHubProvider(app.db, settings)._reserve(
                identifier, 1, "https://api.github.com/repos/a/b", None, "atomic"
            )
        except DomainError as exc:
            assert exc.code == "GITHUB_BUSY"
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(attempt, runs))
    winners = [row for row in reservations if row]
    assert len(winners) == 1
    provider = GitHubProvider(app.db, settings)
    provider._record(winners[0], "failed", {})
    with pytest.raises(DomainError) as failure:
        provider._reserve(
            winners[0].run_id, 100, "https://api.github.com/repos/a/b", None, "expanded"
        )
    assert failure.value.code == "GITHUB_BUDGET"


@pytest.mark.asyncio
async def test_real_pg_share_guard_serializes_erasure_with_real_redis_publish(native):
    app, actor = native["app"], native["actor"]
    cache = RedisCache(os.environ["CAREERFORGE_REDIS_CACHE_URL"])
    key = "v16-privacy-" + uuid4().hex
    entered, release = asyncio.Event(), asyncio.Event()
    deletion_started, deletion_committed = threading.Event(), threading.Event()
    with app.db.read() as db:
        generation = assert_personal_access(db, actor)
    original_eval = cache.redis.eval

    async def held_eval(script, count, *args):
        if count == 2:
            entered.set()
            await release.wait()
        return await original_eval(script, count, *args)

    cache.redis.eval = held_eval

    @contextmanager
    def guard():
        with app.db.transaction() as db:
            assert_personal_access(db, actor, expected_generation=generation)
            yield

    def erase_fence():
        deletion_started.set()
        with app.db.transaction() as db:
            state = db.scalar(
                select(PrivacyState).where(PrivacyState.principal_id == actor).with_for_update()
            )
            state.generation += 1
        deletion_committed.set()

    async def loader():
        return {"privacy_owner_id": actor, "privacy_generation": generation, "body": "private"}

    task = asyncio.create_task(cache.singleflight("base", key, loader, publish_guard=guard))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        deletion = asyncio.create_task(asyncio.to_thread(erase_fence))
        assert await asyncio.to_thread(deletion_started.wait, 5)
        await asyncio.sleep(0.1)
        assert not deletion_committed.is_set(), (
            "exclusive erasure crossed held shared publish guard"
        )
        release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(deletion, 5)
        await cache.redis.delete(cache.key("base", key))  # owned-key purge after fence commits
        with pytest.raises(DomainError) as failure:
            await cache.singleflight("base", key, loader, publish_guard=guard)
        assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
        assert await cache.get("base", key) is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if deletion is not None:
            await asyncio.gather(deletion, return_exceptions=True)
        await cache.redis.delete(cache.key("base", key), cache.key("base", key) + ":lock")
        await cache.redis.aclose()


@pytest.mark.asyncio
async def test_real_pg_batch_vectors_and_acl_filtered_hnsw_probe(native):
    app = native["app"]
    source, first, _ = await publish_fixture(native, "pg-a", "fixtures/ann-a")
    _, second, _ = await publish_fixture(native, "pg-b", "fixtures/ann-b", "b" * 40)
    job = app.retrieval.start_index(
        native["actor"],
        IndexJobInput(
            namespace_id=native["ns"],
            funding_id=native["funding"],
            publication_ids=[first, second],
            batch_size=8,
            call_cap=1,
            idempotency_key="native-batch",
            confirm="INDEX",
        ),
    )
    await app.drain()
    assert set(app.runtime.stage(job, "result")["indexed"]) == {first, second}
    with app.db.read() as db:
        vector = list(db.get(KnowledgeEmbedding, (first, native["model"].profile_version)).vector)
    from careerforge.services.security import create_principal

    with app.db.transaction() as db:
        outsider, outsider_ns, _ = create_principal(db, "ann-outsider-" + uuid4().hex)
    other = {
        **native,
        "actor": outsider.id,
        "ns": outsider_ns.id,
        "funding": app.runtime.create_funding(outsider.id, outsider_ns.id, 100),
    }
    _, foreign_pub, _ = await publish_fixture(other, "pg-foreign", "fixtures/ann-foreign", "c" * 40)
    foreign_job = app.retrieval.start_index(
        outsider.id,
        IndexJobInput(
            namespace_id=outsider_ns.id,
            funding_id=other["funding"],
            publication_ids=[foreign_pub],
            call_cap=1,
            idempotency_key="foreign-index",
            confirm="INDEX",
        ),
    )
    await app.drain()
    assert app.runtime.stage(foreign_job, "result")["indexed"] == [foreign_pub]
    path = Path(__file__).parents[1] / "scripts/hnsw_acl_probe.py"
    module_spec = importlib.util.spec_from_file_location("cf_native_hnsw", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    result = module.probe(
        app.db,
        native["actor"],
        native["model"].profile_version,
        vector,
        k=1,
        apply_index=True,
        source_ids=[source],
    )
    assert result["exact_ids"] == [first] and result["approximate_ids"] == [first]
    assert second not in result["approximate_ids"] and result["unauthorized_returned"] == 0
    assert result["ann_exercised"], result["plan"]
    assert result["recall_at_k"] == 1 and result["semantic_quality_validated"] is False
    acl_result = module.probe(app.db, native["actor"], native["model"].profile_version, vector, k=2)
    assert set(acl_result["approximate_ids"]) == {first, second}
    assert foreign_pub not in acl_result["approximate_ids"] and acl_result["ann_exercised"]
    result["cross_actor_acl_probe"] = acl_result
    result["excluded_other_actor_fixture_publication"] = foreign_pub
    output = Path(os.environ.get("CAREERFORGE_V16_EVIDENCE", "validation/v16/retrieval"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "native-hnsw-acl.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
