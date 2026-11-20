from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import func, select

from ..config import Settings
from ..db import Database, aware, clock
from ..errors import DomainError
from ..models import ExternalRequest
from ..models_retrieval_ext import ExternalBudget, GitHubHttpCache, GitHubRateBucket
from ..services.dependencies import check_closure
from ..services.privacy import assert_personal_access
from ..services.research_query import validate_public_search_query
from ..services.run_pause import ensure_unpaused
from ..services.runtime import TERMINAL, active_funding, is_public_system_run, lock_run
from ..services.security import namespace_access
from ..util import (
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_TOTAL_BYTES,
    digest,
    fingerprint,
    safe_path,
    uid,
    validate_files,
)


class RepositoryProvider(Protocol):
    synthetic: bool

    async def search(
        self, query: str, run_id: str, request_cap: int, channel: str = "repository"
    ) -> dict[str, Any]: ...
    async def snapshot(self, url: str, run_id: str, request_cap: int) -> dict[str, Any]: ...


class GitHubReservation(NamedTuple):
    identifier: str
    resource: str
    cache_id: str
    scope: str
    actor: str
    namespace: str
    request_hash: str
    run_id: str
    privacy_generation: int | None
    dependency_ids: tuple[str, ...]
    dependency_hash: str
    cacheable: bool
    cached: tuple[str, bytes] | None


class GitHubProvider:
    synthetic = False

    def __init__(self, db: Database, settings: Settings, client: httpx.AsyncClient | None = None):
        self.db, self.settings, self.client = db, settings, client

    @staticmethod
    def name(url: str) -> str:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise DomainError("GITHUB_URL", "仅支持 https://github.com/owner/repo。", 422)
        name = parsed.path.strip("/").removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or any(
            p in {".", ".."} for p in name.split("/")
        ):
            raise DomainError("GITHUB_URL", "仓库路径无效。", 422)
        return name

    def _resource(self, url: str) -> str:
        path = urlparse(url).path
        if urlparse(url).hostname == "codeload.github.com":
            return "download"
        return (
            "code_search"
            if path == "/search/code"
            else "search"
            if path.startswith("/search/")
            else "core"
        )

    def _bucket(self, db, resource: str) -> GitHubRateBucket:
        # This scope never contains a recoverable token; all workers share its hash.
        identity = fingerprint(
            {"credential": self.settings.github_token or "anonymous", "resource": resource}
        )
        if db.get_bind().dialect.name == "postgresql":
            db.execute(select(func.pg_advisory_xact_lock(int(identity[:16], 16) - 2**63)))
        row = db.scalar(
            select(GitHubRateBucket).where(GitHubRateBucket.id == identity).with_for_update()
        )
        if row is None:
            capacity, _ = self._rate(resource)
            row = GitHubRateBucket(
                id=identity, resource=resource, tokens=capacity, updated_at=clock(db)
            )
            db.add(row)
            db.flush()
        return row

    def _rate(self, resource: str) -> tuple[float, float]:
        # Conservative local ceilings, NOT a claim about every GitHub account quota.
        if resource == "code_search":
            return 10.0, 10.0 / 60
        if resource == "search":
            capacity = 30.0 if self.settings.github_token else 10.0
            return capacity, capacity / 60
        if resource == "download":
            return 6.0, 6.0 / 60
        if resource == "serial":
            return 1.0, 1.0
        return (60.0, 1.0) if self.settings.github_token else (60.0, 60.0 / 3600)

    @staticmethod
    def _egress_classes(db, run, operation: str, access_dependencies: list[str]) -> set[str]:
        classes = (
            {"public_source"}
            if namespace_access(db, run.principal_id, run.namespace_id).kind == "shared_public"
            else {"learning_state", "private_source"}
        )
        if run.workflow == "project_discovery" and operation.startswith(("search:", "release:")):
            from ..services.research_coverage import validate_goal_snapshot

            goal = validate_goal_snapshot(db, run.principal_id, run.input_manifest["goal_snapshot"])
            if goal["goal_version_id"] not in access_dependencies:
                raise DomainError("GITHUB_EGRESS", "项目检索缺少固定目标依赖。", 403)
            classes.add("private_jd")
        if run.input_manifest.get(
            "research_planning_contract"
        ) is not None and operation.startswith(("search:", "release:")):
            from ..services.research_planning import planning_task_in

            task = planning_task_in(db, run.principal_id, run)
            if task is not None and task.get("goal_version_id"):
                # Queries generated from a private goal retain that goal's egress
                # and revocation scope even if the query itself uses public terms.
                if task["goal_version_id"] not in access_dependencies:
                    raise DomainError("GITHUB_EGRESS", "岗位检索缺少固定目标依赖。", 403)
                classes.add("private_jd")
        return classes

    def _reserve(
        self,
        run_id: str,
        cap: int,
        url: str,
        params,
        operation: str,
        access_dependencies: list[str] | None = None,
    ):
        if type(cap) is not int or not 1 <= cap <= 1000:
            raise DomainError("GITHUB_BUDGET", "GitHub请求预算无效。", 422)
        with self.db.transaction() as db:
            run = lock_run(db, run_id)
            if run is None or run.status in TERMINAL or aware(run.deadline_at) <= clock(db):
                raise DomainError("GITHUB_AUTHORITY", "外部请求任务已结束或不存在。", 409)
            ensure_unpaused(db, run)
            funding = active_funding(db, run)
            data_classes = self._egress_classes(db, run, operation, access_dependencies or [])
            if not data_classes <= set(funding.allowed_data_classes):
                raise DomainError("GITHUB_EGRESS", "该授权不允许外发研究查询。", 403)
            generation = (
                None
                if is_public_system_run(db, run)
                else assert_personal_access(db, run.principal_id)
            )
            dependency_ids = tuple(sorted(set(access_dependencies or [])))
            closure = check_closure(db, run.principal_id, list(dependency_ids))
            if generation is None and not closure.public_only:
                raise DomainError("GITHUB_EGRESS", "公共系统请求不得引用私有依赖。", 403)
            declared = run.input_manifest.get("github_request_cap", cap)
            if type(declared) is not int or not 1 <= declared <= 1000:
                raise DomainError("GITHUB_BUDGET", "任务外部预算无效。", 422)
            budget = db.get(ExternalBudget, run_id)
            if budget is None:
                budget = ExternalBudget(run_id=run_id, request_cap=min(cap, declared))
                db.add(budget)
            ceiling = min(cap, declared, budget.request_cap)
            count = (
                db.scalar(
                    select(func.count())
                    .select_from(ExternalRequest)
                    .where(ExternalRequest.run_id == run_id)
                )
                or 0
            )
            if count >= ceiling:
                raise DomainError("GITHUB_BUDGET", "GitHub请求额度已用尽，不能扩大初次授权。")
            serial = self._bucket(db, "serial")
            resource = self._resource(url)
            bucket = self._bucket(db, resource)
            now = clock(db)
            if serial.blocked_until and aware(serial.blocked_until) > now:
                raise DomainError("GITHUB_RATE_LIMIT", "凭证级退避仍有效；未发送请求。", 429)
            if serial.lease_until and aware(serial.lease_until) > now:
                raise DomainError(
                    "GITHUB_BUSY", "该凭证有在途请求；跨Worker并发已阻止，请稍后重试。", 429
                )
            capacity, rate = self._rate(resource)
            available = min(
                capacity,
                bucket.tokens + max(0.0, (now - aware(bucket.updated_at)).total_seconds()) * rate,
            )
            if (bucket.blocked_until and aware(bucket.blocked_until) > now) or available < 1:
                raise DomainError("GITHUB_RATE_LIMIT", "资源桶仍在限流窗口内；未发送请求。", 429)
            bucket.tokens, bucket.updated_at = available - 1, now
            identifier = uid()
            serial.lease_owner, serial.lease_until = identifier, now + timedelta(seconds=40)
            request_hash = fingerprint({"url": url, "params": params})
            scope = fingerprint(
                {
                    "credential": self.settings.github_token or "anonymous",
                    "actor": run.principal_id,
                    "namespace": run.namespace_id,
                }
            )
            cache_id = fingerprint({"scope": scope, "request": request_hash})
            cached = db.get(GitHubHttpCache, cache_id)
            cached_value = None
            if (
                cached
                and aware(cached.expires_at) > now
                and digest(cached.body) == cached.body_hash
                and closure.public_only
            ):
                cached_value = (cached.etag, cached.body)
            db.add(
                ExternalRequest(
                    id=identifier,
                    run_id=run_id,
                    operation=operation,
                    request_hash=request_hash,
                    response_metadata={"resource": resource, "conditional": bool(cached_value)},
                )
            )
            return GitHubReservation(
                identifier,
                resource,
                cache_id,
                scope,
                run.principal_id,
                run.namespace_id,
                request_hash,
                run_id,
                generation,
                dependency_ids,
                closure.closure_hash,
                closure.public_only,
                cached_value,
            )

    def _record(
        self,
        reservation: GitHubReservation,
        status: str,
        metadata: dict[str, Any],
        data: bytes | None = None,
    ) -> bool:
        (
            identifier,
            resource,
            cache_id,
            scope,
            actor,
            namespace,
            request_hash,
            run_id,
            generation,
            dependency_ids,
            dependency_hash,
            cacheable,
            cached,
        ) = reservation
        with self.db.transaction() as db:
            # Never resurrect personal cache bytes after deletion/revocation while HTTP was in flight.
            row = db.get(ExternalRequest, identifier)
            if row is None:
                raise DomainError("GITHUB_RECEIPT", "外部请求审计记录缺失。", 409)
            allowed = True
            try:
                active = lock_run(db, run_id)
                if (
                    active is None
                    or active.status in TERMINAL
                    or aware(active.deadline_at) <= clock(db)
                ):
                    allowed = False
                else:
                    funding = active_funding(db, active)
                    if active.input_manifest.get("research_planning_contract") is not None and not (
                        self._egress_classes(db, active, row.operation, list(dependency_ids))
                        <= set(funding.allowed_data_classes)
                    ):
                        allowed = False
                    if (
                        check_closure(db, actor, list(dependency_ids)).closure_hash
                        != dependency_hash
                    ):
                        allowed = False
                    if generation is not None:
                        assert_personal_access(db, actor, expected_generation=generation)
                    elif not is_public_system_run(db, active):
                        allowed = False
            except DomainError:
                allowed = False
            # Run -> serial -> resource matches reservation's lock order.
            serial = self._bucket(db, "serial")
            bucket = self._bucket(db, resource)
            now = clock(db)
            row.status, row.response_metadata = status, {**row.response_metadata, **metadata}
            if serial.lease_owner == identifier:
                serial.lease_owner = serial.lease_until = None
            remaining, reset, retry = (
                metadata.get("x-ratelimit-remaining"),
                metadata.get("x-ratelimit-reset"),
                metadata.get("retry-after"),
            )
            blocked = None
            try:
                if remaining is not None:
                    bucket.tokens = min(bucket.tokens, max(0, int(remaining)))
                if remaining == "0" and reset:
                    blocked = datetime.fromtimestamp(int(reset), UTC)
                if retry:
                    until = (
                        now + timedelta(seconds=max(0, int(retry)))
                        if str(retry).isdigit()
                        else parsedate_to_datetime(retry)
                    )
                    blocked = max(blocked or now, aware(until))
            except (ValueError, TypeError, OverflowError):
                pass  # Malformed advisory headers cannot increase local authority.
            if metadata.get("http_status") in {403, 429}:
                blocked = max(blocked or now, now + timedelta(seconds=60))
                serial.blocked_until = max(
                    aware(serial.blocked_until) if serial.blocked_until else now, blocked
                )
            if blocked:
                bucket.blocked_until = max(
                    aware(bucket.blocked_until) if bucket.blocked_until else now, blocked
                )
            etag = metadata.get("etag") or (cached[0] if cached else None)
            if (
                allowed
                and cacheable
                and status == "completed"
                and data is not None
                and len(data) <= 1_000_000
                and isinstance(etag, str)
                and 0 < len(etag) <= 500
            ):
                cache = db.get(GitHubHttpCache, cache_id)
                if cache is None:
                    cache = GitHubHttpCache(
                        id=cache_id,
                        identity_hash=scope,
                        principal_id=actor,
                        namespace_id=namespace,
                        request_hash=request_hash,
                    )
                    db.add(cache)
                cache.etag, cache.body, cache.body_hash = etag, data, digest(data)
                cache.observed_at, cache.expires_at = now, now + timedelta(hours=1)
                db.flush()
                # Bound persistent cache per credential+principal namespace, without exposing identities.
                stale = db.scalars(
                    select(GitHubHttpCache)
                    .where(GitHubHttpCache.identity_hash == scope)
                    .order_by(GitHubHttpCache.observed_at.desc(), GitHubHttpCache.id)
                    .offset(64)
                ).all()
                for entry in stale:
                    db.delete(entry)
            return allowed

    async def _get(
        self,
        url: str,
        run_id: str,
        cap: int,
        operation: str,
        params: dict[str, Any] | None = None,
        max_bytes: int = 1_000_000,
        access_dependencies: list[str] | None = None,
    ) -> bytes:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"api.github.com", "codeload.github.com"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
        ):
            raise DomainError("NETWORK_SCOPE", "请求不在允许的HTTPS GitHub范围。", 403)
        reservation = self._reserve(run_id, cap, url, params, operation, access_dependencies)
        cached = reservation[-1]
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "CareerForge/1.6",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token and parsed.netloc == "api.github.com":
            headers["Authorization"] = "Bearer " + self.settings.github_token
        if cached:
            headers["If-None-Match"] = cached[0]
        metadata: dict[str, Any] = {}
        # Source archives stream much longer than metadata. Retain the byte cap
        # and per-read timeout, but do not cut a healthy download off at 30s.
        request_timeout = 180 if parsed.hostname == "codeload.github.com" else 30

        async def send(client: httpx.AsyncClient) -> bytes:
            async with client.stream(
                "GET", url, params=params, headers=headers, timeout=25, follow_redirects=False
            ) as response:
                metadata.update(
                    {
                        k: response.headers[k][:500]
                        for k in (
                            "etag",
                            "x-ratelimit-resource",
                            "x-ratelimit-remaining",
                            "x-ratelimit-reset",
                            "retry-after",
                        )
                        if k in response.headers
                    }
                )
                metadata["http_status"] = response.status_code
                if response.status_code == 304:
                    if not cached or len(cached[1]) > max_bytes:
                        raise DomainError(
                            "GITHUB_CACHE", "304缺少相同身份的完整缓存，不伪造成功。", 502
                        )
                    metadata["cache_revalidated"] = True
                    return cached[1]
                if response.status_code in {403, 429}:
                    raise DomainError(
                        "GITHUB_RATE_LIMIT", "GitHub限流或拒绝访问；保存观察并停止。", 429
                    )
                if response.status_code != 200:
                    raise DomainError(
                        "GITHUB_UNAVAILABLE", "GitHub未返回可用资料；未跟随不受控重定向。", 502
                    )
                data = bytearray()
                async for block in response.aiter_bytes(65536):
                    data.extend(block)
                    if len(data) > max_bytes:
                        raise DomainError("GITHUB_SIZE", "GitHub响应超过范围限制。", 413)
                return bytes(data)

        try:
            with self.db.transaction() as db:
                current = lock_run(db, run_id)
                if current is None:
                    raise DomainError("GITHUB_AUTHORITY", "任务不可访问。", 409)
                ensure_unpaused(db, current)
            if self.client:
                data = await asyncio.wait_for(send(self.client), timeout=request_timeout)
            else:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                    data = await asyncio.wait_for(send(client), timeout=request_timeout)
            if not self._record(reservation, "completed", metadata, data):
                raise DomainError(
                    "GITHUB_AUTHORITY", "响应返回时授权已失效；不返回或缓存内容。", 409
                )
            return data
        except BaseException as exc:
            self._record(
                reservation,
                "unknown"
                if isinstance(exc, (httpx.HTTPError, TimeoutError, asyncio.CancelledError))
                else "failed",
                metadata,
            )
            if isinstance(exc, (httpx.HTTPError, TimeoutError)):
                raise DomainError(
                    "GITHUB_NETWORK", "GitHub请求未完成；未自动重复请求。", 502
                ) from exc
            raise

    @staticmethod
    def _json(data: bytes) -> Any:
        try:
            return json.loads(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DomainError("GITHUB_SCHEMA", "GitHub响应不是受限JSON。", 502) from exc

    @classmethod
    def _object(cls, data: bytes) -> dict[str, Any]:
        value = cls._json(data)
        if not isinstance(value, dict):
            raise DomainError("GITHUB_SCHEMA", "GitHub响应应为JSON对象。", 502)
        return value

    async def search(
        self,
        query: str,
        run_id: str,
        request_cap: int,
        channel: str = "repository",
        *,
        access_dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        validate_public_search_query(query, channel)
        if channel == "releases":
            return await self._releases(query, run_id, request_cap, access_dependencies)
        if channel in {"code", "documentation"} and not self.settings.github_token:
            raise DomainError(
                "CODE_SEARCH_AUTH_REQUIRED", "代码/文档检索缺少鉴权；未伪装为搜索成功。", 403
            )
        endpoint = (
            "repositories"
            if channel == "repository"
            else "issues"
            if channel in {"issues", "pull_requests"}
            else "code"
        )
        expression = (
            query + " archived:false fork:false is:public"
            if channel == "repository"
            else query + " in:file extension:md is:public"
            if channel == "documentation"
            else query + " is:public is:pr"
            if channel == "pull_requests"
            else query + " is:public is:issue"
            if channel == "issues"
            else query + " is:public"
        )
        raw = self._object(
            await self._get(
                "https://api.github.com/search/" + endpoint,
                run_id,
                request_cap,
                "search:" + channel,
                {"q": expression, "per_page": 10},
                access_dependencies=access_dependencies,
            )
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise DomainError("GITHUB_SCHEMA", "搜索响应格式无效。", 502)
        candidates = {}
        observations = []
        for item in raw["items"][:10]:
            if not isinstance(item, dict):
                continue
            repo = item if channel == "repository" else item.get("repository") or {}
            if not isinstance(repo, dict):
                continue
            if channel in {"issues", "pull_requests"} and not repo:
                u = urlparse(item.get("repository_url", ""))
                if u.netloc != "api.github.com" or not u.path.startswith("/repos/"):
                    continue
                repo = {"full_name": u.path[len("/repos/") :], "id": None}
            if (
                channel in {"repository", "code", "documentation"}
                and repo.get("private") is not False
            ):
                continue
            name = repo.get("full_name", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or repo.get("private"):
                continue
            identity = str(repo.get("id") or name.lower())
            candidates[identity] = {
                "repository_id": identity,
                "name": name,
                "url": "https://github.com/" + name,
                "description": (repo.get("description") or "")[:1500],
                "license": (repo.get("license") or {}).get("spdx_id"),
                "language": repo.get("language"),
                "stars": repo.get("stargazers_count"),
                "forks": repo.get("forks_count"),
                "fork": repo.get("fork"),
                "archived": repo.get("archived"),
                "verified_implementation": False,
            }
            observations.append(
                {
                    "channel": channel,
                    "repository": name,
                    "path": item.get("path"),
                    "source_url": item.get("html_url"),
                    "title": (item.get("title") or "")[:300],
                    "excerpt": (item.get("body") or "")[:1200],
                    "source_status": "search_lead_not_code_evidence",
                }
            )
        return {
            "query": query,
            "channel": channel,
            "candidates": list(candidates.values()),
            "leads": observations,
            "incomplete_results": bool(raw.get("incomplete_results", True)),
            "total_count": raw.get("total_count"),
            "synthetic": False,
        }

    async def _releases(
        self,
        query: str,
        run_id: str,
        request_cap: int,
        access_dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        names = re.findall(r"(?:^|\s)repo:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=\s|$)", query)
        if len(set(names)) > 2:
            raise DomainError("QUERY", "Release通道每次最多固定两个仓库。", 422)
        if not names:
            discovery = await self.search(
                query, run_id, request_cap, "repository", access_dependencies=access_dependencies
            )
            names = [candidate["name"] for candidate in discovery["candidates"][:2]]
        candidates, leads = [], []
        for name in dict.fromkeys(names):
            name = self.name("https://github.com/" + name)
            meta = self._object(
                await self._get(
                    "https://api.github.com/repos/" + name,
                    run_id,
                    request_cap,
                    "release:metadata",
                    access_dependencies=access_dependencies,
                )
            )
            # Deployment-owned token access is never sufficient authority to expose a private repo.
            if not isinstance(meta, dict) or meta.get("private") is not False:
                raise DomainError("SOURCE_POLICY", "Release研究只处理已确认公开的仓库。", 403)
            rows = self._json(
                await self._get(
                    f"https://api.github.com/repos/{name}/releases",
                    run_id,
                    request_cap,
                    "release:list",
                    {"per_page": 10, "page": 1},
                    access_dependencies=access_dependencies,
                )
            )
            if not isinstance(rows, list):
                raise DomainError("GITHUB_SCHEMA", "Release响应格式无效。", 502)
            candidates.append(
                {
                    "repository_id": str(meta.get("id") or name.lower()),
                    "name": name,
                    "url": "https://github.com/" + name,
                    "verified_implementation": False,
                }
            )
            for release in rows[:10]:
                if not isinstance(release, dict) or release.get("draft") is not False:
                    continue
                leads.append(
                    {
                        "channel": "releases",
                        "repository": name,
                        "source_url": release.get("html_url"),
                        "title": str(release.get("name") or release.get("tag_name") or "")[:300],
                        "excerpt": str(release.get("body") or "")[:1200],
                        "tag_name": str(release.get("tag_name") or "")[:200],
                        "source_status": "release_statement_not_code_evidence",
                    }
                )
        return {
            "query": query,
            "channel": "releases",
            "candidates": candidates,
            "leads": leads,
            "incomplete_results": True,
            "stop_reason": "bounded_first_page_not_exhaustive",
            "synthetic": False,
        }

    async def project_metadata(self, url: str, run_id: str, request_cap: int) -> dict[str, Any]:
        name = self.name(url)
        raw = self._object(
            await self._get("https://api.github.com/repos/" + name, run_id, request_cap, "metadata")
        )
        if raw.get("private") is not False:
            raise DomainError("SOURCE_POLICY", "仅允许公开仓库元数据。", 403)
        return {
            key: raw.get(key)
            for key in (
                "id",
                "full_name",
                "private",
                "fork",
                "default_branch",
                "license",
                "language",
                "stargazers_count",
                "forks_count",
                "archived",
                "pushed_at",
            )
        } | {"observed_at": datetime.now(UTC).isoformat()}

    async def quality_snapshot(
        self,
        url: str,
        run_id: str,
        request_cap: int,
        *,
        metadata=None,
        sampling_mode="quality_evidence",
        source_terms=(),
        pinned_commit=None,
    ) -> dict[str, Any]:
        """Pin bounded quality evidence; never describe this sample as the whole repo."""
        from ..services.sources import LICENSE_ALLOWLIST

        name = self.name(url)
        meta = metadata or self._object(
            await self._get("https://api.github.com/repos/" + name, run_id, request_cap, "metadata")
        )
        license_id = (meta.get("license") or {}).get("spdx_id") or "unknown"
        if (
            license_id not in LICENSE_ALLOWLIST
            or meta.get("private") is not False
            or meta.get("archived")
            or meta.get("fork")
        ):
            raise DomainError("SOURCE_POLICY", "仓库不符合公开来源准入策略。", 403)
        ref = quote(meta.get("default_branch", "main"), safe="")
        commit = (
            {"sha": pinned_commit}
            if pinned_commit
            else self._object(
                await self._get(
                    f"https://api.github.com/repos/{name}/commits/{ref}", run_id, request_cap, "pin"
                )
            )
        )
        sha = commit.get("sha", "")
        if not re.fullmatch("[a-f0-9]{40}", sha):
            raise DomainError("GITHUB_COMMIT", "不能固定仓库版本。", 422)
        tree = self._object(
            await self._get(
                f"https://api.github.com/repos/{name}/git/trees/{sha}",
                run_id,
                request_cap,
                "quality:tree",
                params={"recursive": "1"},
                max_bytes=4_000_000,
            )
        )
        rows = [
            r
            for r in tree.get("tree", [])
            if r.get("type") == "blob"
            and isinstance(r.get("size"), int)
            and 0
            < r["size"]
            <= (MAX_SOURCE_FILE_BYTES if sampling_mode == "architecture_fallback" else 96_000)
            and r.get("mode") in {"100644", "100755"}
            and not any(
                part in {"vendor", "vendored", "third_party", "node_modules", "libs"}
                for part in r.get("path", "").split("/")
            )
        ]
        rows.sort(key=lambda r: (r["path"].count("/"), r["path"]))
        suffixes = {
            "Python": (".py",),
            "Go": (".go",),
            "Java": (".java",),
            "TypeScript": (".ts", ".tsx"),
            "JavaScript": (".js", ".jsx"),
            "C#": (".cs",),
            "Rust": (".rs",),
            "C": (".c", ".h"),
            "C++": (".cpp", ".cc", ".h"),
        }.get(meta.get("language") or "", (".py", ".ts", ".go", ".java", ".cs", ".rs"))
        if sampling_mode == "learning_content" and source_terms:

            def relevance(row):
                path = row["path"].lower()
                name = path.rsplit("/", 1)[-1]
                return sum(3 * (term in name) + (term in path) for term in source_terms)

            rows.sort(key=relevance, reverse=True)
        groups = [
            [
                r
                for r in rows
                if r["path"].lower().split("/")[-1].startswith("readme")
                and r["path"].endswith((".md", ".rst"))
            ][:1],
            [
                r
                for r in rows
                if r["path"].lower().split("/")[-1]
                in {
                    "pyproject.toml",
                    "package.json",
                    "go.mod",
                    "requirements.txt",
                    "pom.xml",
                    "cargo.toml",
                    "setup.py",
                }
            ][:1],
            sorted(
                [r for r in rows if r["path"].startswith(".github/workflows/")],
                key=lambda r: (
                    not bool(re.search(r"test", r["path"], re.I)),
                    str(meta.get("language", "")).casefold() not in r["path"].casefold(),
                    not bool(re.search(r"ci|build", r["path"], re.I)),
                    r["path"],
                ),
            )[:2],
            sorted(
                [
                    r
                    for r in rows
                    if re.search(
                        r"(?:^|/)(?:test_[^/]+|[^/]+(?:_test|\.test|\.spec|Tests))\.",
                        r["path"],
                        re.I,
                    )
                    and r["path"].endswith(suffixes)
                ],
                key=lambda r: (
                    r["path"].startswith(".github/"),
                    "fixture" in r["path"].lower(),
                    r["path"],
                ),
            )[:2],
            [
                r
                for r in rows
                if r["path"].endswith(suffixes)
                and not re.search(
                    r"(?:^|/)(?:tests?|docs|examples|samples|configs|scripts)(?:/|$)|(?:test_|_test\.|\.test\.|\.spec\.)",
                    r["path"],
                    re.I,
                )
                and r["path"].split("/")[-1]
                not in {
                    "__init__.py",
                    "__main__.py",
                    "conftest.py",
                    "setup.py",
                    "constant.py",
                    "constants.py",
                    "version.py",
                    "vite.config.ts",
                    "index.ts",
                }
            ][:2],
            [
                r
                for r in rows
                if r["path"].split("/")[-1]
                in {
                    "uv.lock",
                    "poetry.lock",
                    "pnpm-lock.yaml",
                    "package-lock.json",
                    "go.sum",
                    "Cargo.lock",
                }
            ][:1],
        ]
        if sampling_mode == "learning_content":
            groups = [groups[0], groups[1], groups[4]]
        elif sampling_mode == "architecture_fallback":
            code = [
                r
                for r in rows
                if r["path"].endswith(suffixes)
                and not re.search(
                    r"(?:^|/)(?:tests?|docs|examples|scripts)(?:/|$)|\.test\.|\.spec\.",
                    r["path"],
                    re.I,
                )
            ]
            code.sort(
                key=lambda r: (
                    not bool(
                        re.search(
                            r"(?:^|/)(?:agent|agent-loop|loop|runtime|session|index|main|types)\.",
                            r["path"],
                            re.I,
                        )
                    ),
                    r["path"].count("/"),
                    r["path"],
                )
            )
            # Keep entry points, then follow their local imports at the same
            # pinned commit instead of sampling only shallow index files.
            groups = [groups[0], groups[1], code[:12]]
        elif sampling_mode != "quality_evidence":
            raise DomainError("PROJECT_SAMPLING_MODE", "未知项目采样方式。", 422)
        selected = {r["path"]: r for group in groups for r in group}
        files: dict[str, str] = {}
        gaps: list[str] = []
        pending = list(selected.items())
        inventory = {row["path"]: row for row in rows}
        import_candidates: set[str] = set()
        for path, row in pending:
            blob = self._object(
                await self._get(
                    f"https://api.github.com/repos/{name}/git/blobs/{row['sha']}",
                    run_id,
                    request_cap,
                    "quality:blob",
                    max_bytes=max(180_000, row["size"] * 2 + 4096),
                )
            )
            try:
                if blob.get("encoding") != "base64" or blob.get("sha") != row["sha"]:
                    raise ValueError("Unexpected blob binding")
                data = base64.b64decode(blob["content"])
                if len(data) != row["size"]:
                    raise ValueError("Blob size changed")
                if (
                    sum(len(value.encode("utf-8")) for value in files.values()) + len(data)
                    > MAX_SOURCE_TOTAL_BYTES
                ):
                    gaps.append(path)
                    continue
                if (
                    hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                    != row["sha"]
                ):
                    raise ValueError("Blob hash changed")
                files[path] = data.decode("utf-8", errors="strict")
                if sampling_mode == "architecture_fallback":
                    import posixpath

                    for spec in re.findall(
                        r"(?:from\s+|import\s*)[\"'](\.[^\"']+)[\"']", files[path]
                    ):
                        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
                        choices = [
                            base,
                            re.sub(r"\.js$", ".ts", base),
                            base + ".ts",
                            base + "/index.ts",
                            base + ".py",
                        ]
                        import_candidates.update(
                            candidate
                            for candidate in choices
                            if candidate in inventory and candidate not in selected
                        )
                    # Finish seed entries before choosing dependency leads so one
                    # early package cannot consume the whole bounded snapshot.
                    if path == pending[-1][0]:
                        cap = min(26, max(14, request_cap - 6))
                        leads = sorted(
                            import_candidates - selected.keys(),
                            key=lambda item: (
                                not bool(
                                    re.search(
                                        r"(?:^|/)(?:sdk|[\w-]*session|agent|agent-loop|runtime)\.[\w]+$",
                                        item,
                                        re.I,
                                    )
                                ),
                                item.count("/"),
                                item,
                            ),
                        )[: max(0, cap - len(selected))]
                        for candidate in leads:
                            selected[candidate] = inventory[candidate]
                            pending.append((candidate, inventory[candidate]))
            except (ValueError, UnicodeError, KeyError):
                gaps.append(path)
        if not files:
            raise DomainError("GITHUB_QUALITY_EVIDENCE", "未取得可读取的固定质量证据。", 422)
        return {
            "repository_key": name,
            "commit_sha": sha,
            "license_id": license_id,
            "files": files,
            "source_metadata": {
                "github_id": str(meta["id"]),
                "source_kind": "github",
                "synthetic": False,
                "partial_snapshot": True,
                "snapshot_purpose": "architecture_learning"
                if sampling_mode == "architecture_fallback"
                else "project_quality_evidence",
                "source_scope": "selected_fixed_files_not_full_repository",
                "tree_truncated": bool(tree.get("truncated")),
                "selected_paths": list(selected),
                "unreadable_paths": gaps,
                "repository_language": meta.get("language"),
                "community_metadata": {
                    "stars": meta.get("stargazers_count"),
                    "forks": meta.get("forks_count"),
                    "observed_at": meta.get("observed_at") or datetime.now(UTC).isoformat(),
                },
                "sampling_mode": sampling_mode,
                "pushed_at": meta.get("pushed_at"),
                "archived": meta.get("archived"),
            },
        }

    async def snapshot(self, url: str, run_id: str, request_cap: int) -> dict[str, Any]:
        name = self.name(url)
        meta = self._object(
            await self._get("https://api.github.com/repos/" + name, run_id, request_cap, "metadata")
        )
        ref = quote(meta.get("default_branch", "main"), safe="")
        commit = self._object(
            await self._get(
                f"https://api.github.com/repos/{name}/commits/{ref}", run_id, request_cap, "pin"
            )
        )
        sha = commit.get("sha", "")
        if not re.fullmatch("[a-f0-9]{40}", sha):
            raise DomainError("GITHUB_COMMIT", "不能固定仓库版本。", 422)
        license_id = (meta.get("license") or {}).get("spdx_id") or "unknown"
        from ..services.sources import LICENSE_ALLOWLIST

        if (
            license_id not in LICENSE_ALLOWLIST
            or meta.get("private") is not False
            or meta.get("archived")
            or meta.get("fork")
        ):
            raise DomainError("SOURCE_POLICY", "仓库不符合自动下载与分析准入策略。", 403)
        if isinstance(meta.get("size"), int) and meta["size"] > 50_000:
            return await self.quality_snapshot(
                url,
                run_id,
                request_cap,
                metadata=meta,
                sampling_mode="architecture_fallback",
                pinned_commit=sha,
            )
        try:
            data = await self._get(
                f"https://codeload.github.com/{name}/zip/{sha}",
                run_id,
                request_cap,
                "archive",
                max_bytes=20_000_000,
            )
        except DomainError as exc:
            if exc.code not in {"GITHUB_NETWORK", "GITHUB_SIZE"}:
                raise
            # Do not replay an uncertain archive request or widen the pinned
            # version. Read a bounded, explicitly partial tree/blob sample.
            return await self.quality_snapshot(
                url,
                run_id,
                request_cap,
                metadata=meta,
                sampling_mode="architecture_fallback",
                pinned_commit=sha,
            )
        files, skipped = self.unpack(data)
        return {
            "repository_key": name,
            "commit_sha": sha,
            "license_id": license_id,
            "files": files,
            "source_metadata": {
                "github_id": str(meta["id"]),
                "skipped": skipped,
                "archive_hash": digest(data),
                "source_kind": "github",
                "synthetic": False,
            },
        }

    @staticmethod
    def unpack(data: bytes) -> tuple[dict[str, str], dict[str, int]]:
        files: dict[str, str] = {}
        skipped: dict[str, int] = {}

        def skip(reason: str):
            skipped[reason] = skipped.get(reason, 0) + 1

        allowed = {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".c",
            ".h",
            ".cpp",
            ".md",
            ".txt",
            ".rst",
            ".json",
            ".toml",
            ".yaml",
            ".yml",
            ".sql",
            ".lock",
        }
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 20000 or sum(i.file_size for i in infos) > 100_000_000:
                    raise DomainError("ARCHIVE_LIMIT", "归档膨胀/文件数超过限制。", 413)
                total = 0
                for info in infos:
                    if info.is_dir():
                        continue
                    parts = info.filename.split("/", 1)
                    if len(parts) != 2:
                        skip("no_root")
                        continue
                    path = parts[1]
                    try:
                        safe_path(path)
                    except DomainError:
                        skip("unsafe_path")
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16):
                        skip("symlink")
                        continue
                    from pathlib import PurePosixPath

                    p = PurePosixPath(path)
                    if p.suffix.lower() not in allowed and p.name.lower() not in {
                        "license",
                        "notice",
                        "dockerfile",
                        "makefile",
                    }:
                        skip("unsupported")
                        continue
                    if (
                        info.file_size > MAX_SOURCE_FILE_BYTES
                        or len(files) >= MAX_SOURCE_FILES
                        or total + info.file_size > MAX_SOURCE_TOTAL_BYTES
                    ):
                        skip("size_limit")
                        continue
                    try:
                        body = archive.read(info).decode("utf-8")
                        validate_files({path: body})
                    except (UnicodeDecodeError, DomainError):
                        skip("invalid_text")
                        continue
                    if path in files:
                        raise DomainError("ARCHIVE_DUPLICATE", "归档中有重复文件路径。", 422)
                    files[path] = body
                    total += info.file_size
        except zipfile.BadZipFile as exc:
            raise DomainError("ARCHIVE_INVALID", "GitHub 归档损坏。", 422) from exc
        validate_files(files)
        return files, skipped
