from __future__ import annotations

from contextlib import contextmanager

import pytest
from test_cache_resilience import RaceRedis

from careerforge.adapters.cache import MemoryCache, RedisCache
from careerforge.errors import DomainError
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.privacy import assert_personal_access


@pytest.mark.asyncio
@pytest.mark.parametrize("redis", [False, True])
async def test_generation_change_between_loader_and_publish_never_refills_cache(env, redis):
    cache = object.__new__(RedisCache) if redis else MemoryCache()
    if redis:
        cache.redis = RaceRedis()
    app, actor = env["app"], env["actor"]
    with app.db.read() as db:
        generation = assert_personal_access(db, actor)

    @contextmanager
    def guard():
        with app.db.transaction() as db:
            assert_personal_access(db, actor, expected_generation=generation)
            yield

    async def loader():
        with app.db.transaction() as db:
            db.get(PrivacyState, actor).generation += 1
        return {"privacy_owner_id": actor, "sensitive": "old body"}

    with pytest.raises(DomainError) as failure:
        await cache.singleflight("base", "erasure-race", loader, publish_guard=guard)
    assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
    assert cache.redis.cache_writes == 0 if redis else not cache.values


@pytest.mark.asyncio
@pytest.mark.parametrize("redis", [False, True])
async def test_publish_guard_wraps_actual_cache_write(redis):
    entered = False

    @contextmanager
    def guard():
        nonlocal entered
        assert not entered
        entered = True
        try:
            yield
        finally:
            entered = False

    if redis:

        class GuardedRedis(RaceRedis):
            async def eval(self, script, count, *args):
                if count == 2:
                    assert entered
                return await super().eval(script, count, *args)

        cache = object.__new__(RedisCache)
        cache.redis = GuardedRedis()
    else:

        class GuardedMemory(MemoryCache):
            async def put(self, *args, **kwargs):
                assert entered
                return await super().put(*args, **kwargs)

        cache = GuardedMemory()

    async def loader():
        assert not entered
        return {"ok": True}

    assert await cache.singleflight("base", "wrapped", loader, publish_guard=guard) == {"ok": True}
    assert not entered


@pytest.mark.asyncio
async def test_retrieval_guard_tracks_other_private_source_owner(env, monkeypatch):
    from conftest import publish_fixture

    from careerforge.contracts import RetrievalTaskSpec, TaskType
    from careerforge.services.security import create_principal, set_grant

    app, owner = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "cross-owner-cache-reader")
        set_grant(db, owner, env["ns"], reader.id, "reader", True)
    original = app.cache.singleflight

    async def deleting_loader_wrapper(layer, key, loader, ttl=30, *, publish_guard=None):
        async def load_then_change_owner():
            value = await loader()
            assert owner in value["privacy_owner_ids"] and reader.id in value["privacy_owner_ids"]
            with app.db.transaction() as db:
                db.get(PrivacyState, owner).generation += 1
            return value

        return await original(
            layer, key, load_then_change_owner, ttl=ttl, publish_guard=publish_guard
        )

    monkeypatch.setattr(app.cache, "singleflight", deleting_loader_wrapper)
    with pytest.raises(DomainError) as failure:
        await app.retrieval.retrieve(
            reader.id,
            RetrievalTaskSpec(
                turn_id="cross-owner",
                focus_revision=1,
                task_type=TaskType.CONCEPT,
                user_question="Tool Calling",
                project_source_ids=[source],
            ),
        )
    assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
    assert not app.cache.values


@pytest.mark.asyncio
async def test_local_research_cannot_persist_another_owners_late_observation(env, monkeypatch):
    from conftest import publish_fixture
    from sqlalchemy import select

    from careerforge.contracts import ModelOutput
    from careerforge.models import SearchObservation, StageArtifact
    from careerforge.services.security import create_principal, set_grant
    from careerforge.services.sources import SourceService

    app, owner = env["app"], env["actor"]
    await publish_fixture(env)
    with app.db.transaction() as db:
        reader, namespace, _ = create_principal(db, "cross-owner-research-reader")
        set_grant(db, owner, env["ns"], reader.id, "reader", True)
    funding = app.runtime.create_funding(reader.id, namespace.id, 5)
    identifier = app.runtime.create(
        reader.id, namespace.id, funding, "research", {}, 1, "research-erase-window"
    )
    epoch = app.runtime.claim(identifier, "late-local")
    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [
                {
                    "channel": "code",
                    "query": "echo",
                    "question": "locate echo",
                    "topic_keys": ["agent.tool_calling"],
                }
            ],
            "stop_reason": None,
        },
    )
    original = SourceService.search_snapshots

    def read_then_delete(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        assert result["candidates"]
        with app.db.transaction() as db:
            db.get(PrivacyState, owner).generation += 1
        return result

    monkeypatch.setattr(SourceService, "search_snapshots", read_then_delete)
    with pytest.raises(DomainError) as failure:
        await app.research.run(
            reader.id,
            identifier,
            epoch,
            "late-local",
            "echo",
            ["agent.tool_calling"],
            max_nodes=2,
            max_depth=1,
            model_cap=1,
            request_cap=5,
        )
    assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
    with app.db.read() as db:
        assert not db.scalars(select(SearchObservation)).all()
        assert not db.scalars(
            select(StageArtifact).where(
                StageArtifact.run_id == identifier, StageArtifact.stage_key.like("search:%")
            )
        ).all()
