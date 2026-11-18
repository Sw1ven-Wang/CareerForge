from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from collections import Counter, OrderedDict
from contextlib import AbstractContextManager, nullcontext, suppress
from typing import Any, Awaitable, Callable, Protocol, cast

from ..errors import DomainError
from ..util import canonical

PublishGuard = Callable[[], AbstractContextManager[None]]


class Cache(Protocol):
    async def get(self, layer: str, key: str) -> Any: ...
    async def put(self, layer: str, key: str, value: Any, ttl: int = 60) -> None: ...
    async def singleflight(
        self,
        layer: str,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl: int = 60,
        *,
        publish_guard: PublishGuard | None = None,
    ) -> Any: ...


class MemoryCache:
    """Explicit development/test adapter, never automatically substituted in production."""

    def __init__(self, max_entries: int = 1024):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self.values: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_users: Counter[str] = Counter()
        self.metrics: Counter[str] = Counter()

    def _prune(self) -> None:
        now = time.monotonic()
        for identity, (expires, _) in list(self.values.items()):
            if expires <= now:
                self.values.pop(identity, None)

    @staticmethod
    def key(layer: str, key: str) -> str:
        if layer not in {"exact", "base", "derived"}:
            raise ValueError("unknown cache layer")
        return layer + ":" + key

    async def get(self, layer: str, key: str) -> Any:
        self._prune()
        identity = self.key(layer, key)
        record = self.values.get(identity)
        if record:
            self.values.move_to_end(identity)
            self.metrics[layer + ".hit"] += 1
            # Return a copy so callers cannot mutate a cached answer or scope.
            return json.loads(canonical(record[1]))
        self.metrics[layer + ".miss"] += 1
        return None

    async def put(self, layer: str, key: str, value: Any, ttl: int = 60) -> None:
        self._prune()
        identity = self.key(layer, key)
        self.values[identity] = (time.monotonic() + max(1, ttl), json.loads(canonical(value)))
        self.values.move_to_end(identity)
        while len(self.values) > self.max_entries:
            self.values.popitem(last=False)

    async def singleflight(
        self,
        layer: str,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl: int = 60,
        *,
        publish_guard: PublishGuard | None = None,
    ) -> Any:
        identity = self.key(layer, key)
        lock = self.locks.setdefault(identity, asyncio.Lock())
        self.lock_users[identity] += 1
        try:
            async with lock:
                value = await self.get(layer, key)
                if value is not None:
                    return value
                self.metrics[layer + ".load"] += 1
                value = await loader()
                with publish_guard() if publish_guard else nullcontext():
                    await self.put(layer, key, value, ttl)
                return value
        finally:
            self.lock_users[identity] -= 1
            if not self.lock_users[identity]:
                self.lock_users.pop(identity, None)
                if self.locks.get(identity) is lock:
                    self.locks.pop(identity, None)


class RedisCache:
    """Three separate logical layers. Cached candidates are never authorization decisions."""

    def __init__(self, url: str):
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise DomainError(
                "REDIS_REQUIRED", "请安装 production 依赖，生产模式不使用内存缓存。", 503
            ) from exc
        self.redis = Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=3, socket_timeout=5
        )

    @staticmethod
    def key(layer: str, key: str) -> str:
        if layer not in {"exact", "base", "derived"}:
            raise ValueError("unknown cache layer")
        return "cf15:" + layer + ":" + key

    async def get(self, layer: str, key: str) -> Any:
        value = await self.redis.get(self.key(layer, key))
        await cast(
            Awaitable[int],
            self.redis.hincrby("cf15:cache_metrics", layer + (".hit" if value else ".miss"), 1),
        )
        return json.loads(value) if value else None

    async def put(self, layer: str, key: str, value: Any, ttl: int = 60) -> None:
        expiry = max(1, int(ttl * random.uniform(0.85, 1.15)))
        await self.redis.set(self.key(layer, key), canonical(value), ex=expiry)

    async def singleflight(
        self,
        layer: str,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl: int = 60,
        *,
        publish_guard: PublishGuard | None = None,
    ) -> Any:
        lock_key, token = self.key(layer, key) + ":lock", secrets.token_hex(24)
        started = time.monotonic()
        while True:
            found = await self.get(layer, key)
            if found is not None:
                return found
            if await self.redis.set(lock_key, token, nx=True, px=30000):
                break
            if time.monotonic() - started > 35:
                raise DomainError("CACHE_BUSY", "相同查询正在构建，请重试；未重复请求模型。", 503)
            await asyncio.sleep(0.05)

        async def renew():
            while True:
                await asyncio.sleep(5)
                changed = await self.redis.eval(
                    "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('pexpire',KEYS[1],30000) else return 0 end",
                    1,
                    lock_key,
                    token,
                )
                if not changed:
                    raise DomainError("CACHE_LEASE", "缓存构建租约失效。")

        monitor = asyncio.create_task(renew())
        try:
            # Another owner may have populated the cache between our miss and
            # successful lock acquisition. Recheck before doing any costly work.
            found = await self.get(layer, key)
            if found is not None:
                return found
            await cast(Awaitable[int], self.redis.hincrby("cf15:cache_metrics", layer + ".load", 1))
            value = await loader()
            expiry = max(1, int(ttl * random.uniform(0.85, 1.15)))
            # Token comparison and publication must be one Redis operation: a
            # GET followed by SET lets an expired owner overwrite a successor.
            with publish_guard() if publish_guard else nullcontext():
                published = await cast(
                    Awaitable[int],
                    self.redis.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then "
                        "redis.call('set',KEYS[2],ARGV[2],'EX',ARGV[3]); return 1 else return 0 end",
                        2,
                        lock_key,
                        self.key(layer, key),
                        token,
                        canonical(value),
                        expiry,
                    ),
                )
            if not published:
                raise DomainError("CACHE_LEASE", "缓存构建租约失效，不写过期结果。")
            return value
        finally:
            monitor.cancel()
            from redis.exceptions import RedisError

            # Cleanup must not replace the primary loader/cancellation failure.
            with suppress(asyncio.CancelledError, DomainError, RedisError):
                await monitor
            with suppress(RedisError):
                await cast(
                    Awaitable[int],
                    self.redis.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                        1,
                        lock_key,
                        token,
                    ),
                )
