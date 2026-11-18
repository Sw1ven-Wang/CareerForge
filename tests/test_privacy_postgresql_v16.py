"""Real PostgreSQL fences and lock order, opt-in disposable database only."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_runtime_postgresql import pg_runtime as pg_runtime

from careerforge.errors import DomainError
from careerforge.models import FundingAgreement
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.privacy import assert_personal_access
from careerforge.services.security import create_principal

pytestmark = pytest.mark.production


def test_cross_owner_shared_privacy_reads_do_not_deadlock(pg_runtime):
    app, actor, _, _ = pg_runtime
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "privacy-cross-" + actor)
    barrier = threading.Barrier(2)

    def read(order):
        with app.db.transaction() as db:
            assert_personal_access(db, order[0])
            barrier.wait(timeout=5)
            assert_personal_access(db, order[1])
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(read, [(actor, other.id), (other.id, actor)]))


@pytest.mark.asyncio
async def test_erasure_state_only_fence_does_not_invert_funding_lock(pg_runtime):
    app, actor, namespace, funding = pg_runtime
    app.runtime.create(actor, namespace, funding, "research", {}, 4, "privacy-lock-order")
    proposal = app.privacy.propose(actor)
    funding_locked, continue_worker = threading.Event(), threading.Event()

    def worker():
        with app.db.transaction() as db:
            db.scalar(
                select(FundingAgreement).where(FundingAgreement.id == funding).with_for_update()
            )
            funding_locked.set()
            assert continue_worker.wait(timeout=5)
            with pytest.raises(DomainError) as exc:
                assert_personal_access(db, actor)
            assert exc.value.code == "PRIVACY_ERASURE_PENDING"

    pending_worker = asyncio.create_task(asyncio.to_thread(worker))
    await asyncio.to_thread(funding_locked.wait, 5)
    erasure = asyncio.create_task(
        app.privacy.confirm(
            actor,
            proposal["proposal_id"],
            proposal["confirmation_nonce"],
            proposal["plan_hash"],
            True,
        )
    )
    try:
        async with asyncio.timeout(5):
            while True:
                with app.db.read() as db:
                    if db.get(PrivacyState, actor).blocked:
                        break
                await asyncio.sleep(0.02)
        continue_worker.set()
        await pending_worker
        result = await asyncio.wait_for(erasure, 8)
        assert result["state"] == "completed"
        with app.db.read() as db:
            assert db.get(FundingAgreement, funding).calls_held == 0
    finally:
        continue_worker.set()
        await asyncio.gather(pending_worker, erasure, return_exceptions=True)


@pytest.mark.asyncio
async def test_cache_publish_shared_fence_is_drained_before_erasure(pg_runtime, monkeypatch):
    from contextlib import contextmanager

    app, actor, _, _ = pg_runtime
    proposal = app.privacy.propose(actor)
    entered, release = asyncio.Event(), asyncio.Event()
    original_put = app.cache.put

    async def delayed_put(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_put(*args, **kwargs)

    @contextmanager
    def guard():
        with app.db.transaction() as db:
            assert_personal_access(db, actor, expected_generation=0)
            yield

    async def loader():
        return {"privacy_owner_id": actor, "secret": "must_be_removed"}

    monkeypatch.setattr(app.cache, "put", delayed_put)
    publisher = asyncio.create_task(
        app.cache.singleflight("base", "privacy-pg-race", loader, publish_guard=guard)
    )
    await entered.wait()
    erasure = asyncio.create_task(
        app.privacy.confirm(
            actor,
            proposal["proposal_id"],
            proposal["confirmation_nonce"],
            proposal["plan_hash"],
            True,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not erasure.done()
        with app.db.read() as db:
            assert not db.get(PrivacyState, actor).blocked
        release.set()
        await publisher
        assert (await asyncio.wait_for(erasure, 8))["state"] == "completed"
        assert await app.cache.get("base", "privacy-pg-race") is None
    finally:
        release.set()
        await asyncio.gather(publisher, erasure, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["cross_copy", "late_callback", "governance"])
async def test_private_content_erasure_on_real_postgres_and_redis(pg_runtime, tmp_path, scenario):
    import os
    from uuid import uuid4

    from support import FixtureModel, FixtureRepositories
    from test_privacy_v16 import (
        test_cross_owner_generated_copies_are_redacted_but_other_work_survives,
        test_explicit_transitive_dependency_fences_late_other_owner_response,
        test_private_taxonomy_projection_and_maintenance_are_erased,
    )

    from careerforge.adapters.cache import RedisCache
    from careerforge.application import Application
    from careerforge.config import Settings

    cache = RedisCache(os.environ["CAREERFORGE_REDIS_CACHE_URL"])
    settings = Settings(
        mode="test",
        database_url=pg_runtime[0].settings.database_url,
        allow_fixture_publication=True,
        embedding_dimensions=8,
        state_dir=tmp_path,
    )
    model = FixtureModel()
    app = Application(settings, model, FixtureRepositories(), cache=cache)
    try:
        from careerforge.models import Namespace
        from careerforge.services.taxonomy import seed

        with app.db.transaction() as db:
            user, private, token = create_principal(
                db, "privacy-pg-content-" + uuid4().hex, admin=True
            )
            public = Namespace(kind="shared_public", owner_id=user.id, name="privacy-test-public")
            db.add(public)
            db.flush()
            owner = {
                "principal_id": user.id,
                "namespace_id": private.id,
                "public_namespace_id": public.id,
                "token": token,
            }
        seed(app.db, user.id, public.id)
        actor, namespace = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, namespace, 1000)
        env = {
            "app": app,
            "actor": actor,
            "ns": namespace,
            "owner": owner,
            "funding": funding,
            "model": model,
        }
        await {
            "cross_copy": test_cross_owner_generated_copies_are_redacted_but_other_work_survives,
            "late_callback": test_explicit_transitive_dependency_fences_late_other_owner_response,
            "governance": test_private_taxonomy_projection_and_maintenance_are_erased,
        }[scenario](env)
    finally:
        await cache.redis.aclose()
        app.db.engine.dispose()
