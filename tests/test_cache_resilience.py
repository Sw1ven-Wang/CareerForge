import asyncio
import json
import os
from uuid import uuid4

import pytest

from careerforge.adapters.cache import MemoryCache, RedisCache
from careerforge.errors import DomainError


@pytest.mark.asyncio
async def test_memory_cache_is_bounded_and_frees_lock_records():
    cache = MemoryCache(max_entries=3)
    for number in range(20):

        async def load(number=number):
            return {"number": number}

        await cache.singleflight("base", str(number), load)
    assert len(cache.values) == 3
    assert not cache.locks and not cache.lock_users
    assert await cache.get("base", "0") is None
    assert await cache.get("base", "19") == {"number": 19}


@pytest.mark.asyncio
async def test_memory_cancelled_waiter_does_not_remove_active_lock():
    cache = MemoryCache()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return {"ok": True}

    owner = asyncio.create_task(cache.singleflight("base", "one", loader))
    await started.wait()
    waiter = asyncio.create_task(cache.singleflight("base", "one", loader))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    other = asyncio.create_task(cache.singleflight("base", "one", loader))
    await asyncio.sleep(0)
    assert calls == 1
    finish.set()
    assert await owner == await other == {"ok": True}
    assert not cache.locks and not cache.lock_users


class RaceRedis:
    """Deterministic interleaving double, explicitly not real Redis acceptance."""

    def __init__(self, populate_at_acquire=False, expire_before_publish=False):
        self.acquired = False
        self.populate = populate_at_acquire
        self.expire = expire_before_publish
        self.cache_writes = 0
        self.token = None

    async def get(self, key):
        if key.endswith(":lock"):
            return self.token
        if self.acquired and self.populate:
            return json.dumps({"winner": "previous-owner"})
        return None

    async def set(self, key, value, **options):
        if key.endswith(":lock"):
            self.acquired = True
            self.token = value
        else:
            self.cache_writes += 1
        return True

    async def hincrby(self, *args):
        return 1

    async def eval(self, script, count, *args):
        if count == 2:
            if self.expire:
                return 0
            self.cache_writes += 1
            return 1
        return 1


@pytest.mark.asyncio
async def test_redis_rechecks_after_lock_acquisition():
    cache = object.__new__(RedisCache)
    cache.redis = RaceRedis(populate_at_acquire=True)

    async def forbidden_loader():
        raise AssertionError("costly duplicate request")

    assert await cache.singleflight("base", "race", forbidden_loader) == {
        "winner": "previous-owner"
    }
    assert cache.redis.cache_writes == 0


@pytest.mark.asyncio
async def test_redis_atomic_fence_rejects_expired_owner():
    cache = object.__new__(RedisCache)
    cache.redis = RaceRedis(expire_before_publish=True)

    async def loader():
        return {"stale": True}

    with pytest.raises(DomainError, match="租约失效") as raised:
        await cache.singleflight("base", "race", loader)
    assert raised.value.code == "CACHE_LEASE"
    assert cache.redis.cache_writes == 0


@pytest.mark.production
@pytest.mark.asyncio
async def test_real_redis_singleflight_and_atomic_stale_owner_fence():
    url = os.getenv("CAREERFORGE_REDIS_CACHE_URL")
    if not url:
        pytest.skip("requires dedicated local Redis via integration harness")
    first, second = RedisCache(url), RedisCache(url)
    key = "validation-" + uuid4().hex
    stale = key + "-stale"
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"value": 1}

    try:
        result = await asyncio.gather(
            *[(first if i % 2 else second).singleflight("base", key, loader) for i in range(12)]
        )
        assert calls == 1 and result == [{"value": 1}] * 12

        async def stale_loader():
            await second.redis.set(second.key("base", stale) + ":lock", "successor", px=30000)
            return {"stale": True}

        with pytest.raises(DomainError) as raised:
            await first.singleflight("base", stale, stale_loader)
        assert raised.value.code == "CACHE_LEASE"
        assert await second.get("base", stale) is None
        assert await second.redis.get(second.key("base", stale) + ":lock") == "successor"
    finally:
        await second.redis.delete(
            second.key("base", key), second.key("base", stale), second.key("base", stale) + ":lock"
        )
        await first.redis.aclose()
        await second.redis.aclose()
