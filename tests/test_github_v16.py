"""Bounded HTTP mocks; no credential or real external request is used."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from careerforge.adapters.github import GitHubProvider
from careerforge.errors import DomainError
from careerforge.models import ExternalRequest
from careerforge.models_retrieval_ext import ExternalBudget, GitHubHttpCache, GitHubRateBucket

TOKEN = "fixture-only-never-a-real-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_archive_fallback_keeps_commit_and_reports_partial_scope(env, monkeypatch, large):
    import base64
    import hashlib
    import json

    provider = GitHubProvider(env["app"].db, env["app"].settings)
    sha = "a" * 40
    content = b"export class Agent {}"
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    calls = []

    async def get(url, run_id, cap, operation, **kwargs):
        calls.append((url, operation))
        if operation == "metadata":
            value = {"id": 1, "private": False, "license": {"spdx_id": "MIT"},
                     "language": "TypeScript", "size": 100_000 if large else 10}
        elif operation == "pin":
            value = {"sha": sha}
        elif operation == "archive":
            raise DomainError("GITHUB_NETWORK", "download interrupted", 502)
        elif operation == "quality:tree":
            assert sha in url
            value = {"tree": [{"path": "packages/agent/src/agent.ts", "sha": blob,
                               "type": "blob", "mode": "100644", "size": len(content)}]}
        else:
            value = {"sha": blob, "encoding": "base64", "content": base64.b64encode(content).decode()}
        return json.dumps(value).encode()

    monkeypatch.setattr(provider, "_get", get)
    result = await provider.snapshot("https://github.com/acme/project", "run", 30)
    assert result["commit_sha"] == sha
    assert result["files"]["packages/agent/src/agent.ts"] == content.decode()
    assert result["source_metadata"]["partial_snapshot"] is True
    assert result["source_metadata"]["snapshot_purpose"] == "architecture_learning"
    assert sum(op == "pin" for _, op in calls) == 1
    assert sum(op == "archive" for _, op in calls) == (0 if large else 1)


def run(env, cap=10, key="github-v16"):
    return env["app"].runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {"github_request_cap": cap}, 1, key
    )


def adapter(env, client):
    return GitHubProvider(env["app"].db, replace(env["app"].settings, github_token=TOKEN), client)


@pytest.mark.asyncio
async def test_etag_revalidated_across_adapter_instances_and_receipts_exclude_token(env):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if len(requests) == 1:
            assert "if-none-match" not in request.headers
            return httpx.Response(200, content=b'{"items":[]}', headers={"ETag": '"frozen"'})
        assert request.headers["if-none-match"] == '"frozen"'
        return httpx.Response(304)

    identifier = run(env)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await adapter(env, client)._get(
            "https://api.github.com/search/repositories", identifier, 10, "test"
        )
        second = await adapter(env, client)._get(
            "https://api.github.com/search/repositories", identifier, 10, "test"
        )
    assert first == second == b'{"items":[]}'
    with env["app"].db.read() as db:
        receipts = db.scalars(
            select(ExternalRequest).where(ExternalRequest.run_id == identifier)
        ).all()
        assert len(receipts) == 2 and receipts[1].response_metadata["cache_revalidated"]
        assert TOKEN not in repr([r.response_metadata for r in receipts])
        assert TOKEN not in repr([r.id for r in db.scalars(select(GitHubRateBucket))])
        assert len(db.scalars(select(GitHubHttpCache)).all()) == 1


@pytest.mark.asyncio
async def test_distributed_inflight_lease_blocks_second_provider(env):
    entered, released = asyncio.Event(), asyncio.Event()
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        entered.set()
        await released.wait()
        return httpx.Response(200, json={})

    first, second = run(env, key="first"), run(env, key="second")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            adapter(env, client)._get("https://api.github.com/repos/acme/x", first, 10, "one")
        )
        await entered.wait()
        with pytest.raises(DomainError, match="跨Worker") as failure:
            await adapter(env, client)._get(
                "https://api.github.com/repos/acme/y", second, 10, "two"
            )
        assert failure.value.code == "GITHUB_BUSY" and count == 1
        released.set()
        await task


@pytest.mark.asyncio
async def test_budget_cannot_be_expanded_on_later_call(env):
    identifier = run(env, cap=10)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        provider = adapter(env, client)
        await provider._get("https://api.github.com/repos/acme/x", identifier, 1, "first")
        with pytest.raises(DomainError) as failure:
            await provider._get("https://api.github.com/repos/acme/x", identifier, 10, "expanded")
        assert failure.value.code == "GITHUB_BUDGET"
    with env["app"].db.read() as db:
        assert db.get(ExternalBudget, identifier).request_cap == 1


@pytest.mark.asyncio
async def test_rate_headers_block_other_worker_without_http(env):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            429,
            headers={
                "Retry-After": "90",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(
                    int((datetime.now(UTC) + timedelta(seconds=120)).timestamp())
                ),
            },
        )

    identifier = run(env)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(DomainError) as failure:
                await adapter(env, client)._get(
                    "https://api.github.com/search/code", identifier, 10, "code"
                )
            assert failure.value.code == "GITHUB_RATE_LIMIT"
    assert count == 1


@pytest.mark.asyncio
async def test_release_and_pr_channels_do_not_approve_prose(env):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/search/issues":
            assert "is:pr" in request.url.params["q"] and "is:public" in request.url.params["q"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "repository_url": "https://api.github.com/repos/acme/tool",
                            "title": "Proposal",
                            "body": "not executed",
                            "html_url": "https://github.com/acme/tool/pull/1",
                        }
                    ],
                    "incomplete_results": False,
                },
            )
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200,
                json=[
                    {"draft": False, "name": "v1", "body": "a claim", "tag_name": "v1"},
                    {"draft": True, "name": "SECRET", "body": "hidden"},
                ],
            )
        return httpx.Response(200, json={"private": False, "id": 7})

    identifier = run(env)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = adapter(env, client)
        pull = await provider.search("tool repo:acme/tool", identifier, 10, "pull_requests")
        release = await provider.search("repo:acme/tool", identifier, 10, "releases")
    assert len(seen) == 3 and len(release["leads"]) == 1 and release["incomplete_results"]
    assert not pull["candidates"][0]["verified_implementation"]
    assert release["leads"][0]["source_status"] == "release_statement_not_code_evidence"


@pytest.mark.asyncio
async def test_private_release_repository_is_not_disclosed(env):
    identifier = run(env)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"private": True}))
    ) as client:
        with pytest.raises(DomainError) as failure:
            await adapter(env, client).search("repo:acme/secret", identifier, 10, "releases")
    assert failure.value.code == "SOURCE_POLICY"


@pytest.mark.asyncio
async def test_external_allowlist_size_and_unbacked_304_fail_closed(env):
    identifier = run(env)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(304))
    ) as client:
        provider = adapter(env, client)
        with pytest.raises(DomainError) as failure:
            await provider._get("https://api.github.com.evil.test/repos/x/y", identifier, 10, "bad")
        assert failure.value.code == "NETWORK_SCOPE"
        with pytest.raises(DomainError) as failure:
            await provider._get("https://api.github.com/repos/x/y", identifier, 10, "304")
        assert failure.value.code == "GITHUB_CACHE"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"12345", headers={"ETag": "five"})
        )
    ) as client:
        with pytest.raises(DomainError) as failure:
            await adapter(env, client)._get(
                "https://api.github.com/repos/x/y", identifier, 10, "large", max_bytes=4
            )
        assert failure.value.code == "GITHUB_SIZE"
    with env["app"].db.read() as db:
        assert not db.scalars(select(GitHubHttpCache)).all()


@pytest.mark.asyncio
async def test_generation_changed_during_http_never_returns_or_refills_cache(env):
    from careerforge.models_privacy_ext import PrivacyState

    identifier = run(env)

    async def handler(request):
        with env["app"].db.transaction() as db:
            db.get(PrivacyState, env["actor"]).generation += 1
        return httpx.Response(200, json={"sensitive": "old body"}, headers={"ETag": "old"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as failure:
            await adapter(env, client)._get(
                "https://api.github.com/repos/x/y", identifier, 10, "late"
            )
        assert failure.value.code == "GITHUB_AUTHORITY"
    with env["app"].db.read() as db:
        assert not db.scalars(select(GitHubHttpCache)).all()
        assert all(
            row.lease_owner is None
            for row in db.scalars(
                select(GitHubRateBucket).where(GitHubRateBucket.resource == "serial")
            )
        )


@pytest.mark.asyncio
async def test_research_freezes_exact_external_receipt_ids(env):
    from careerforge.contracts import ModelOutput
    from careerforge.models import ResearchNode, SearchObservation

    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [
                {
                    "channel": "pull_requests",
                    "query": "echo",
                    "question": "locate echo",
                    "topic_keys": ["agent.tool_calling"],
                }
            ],
            "stop_reason": None,
        },
    )
    identifier = run(env)
    epoch = env["app"].runtime.claim(identifier, "receipt-worker")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": [], "incomplete_results": False})
        )
    ) as client:
        env["app"].research.repositories = adapter(env, client)
        await env["app"].research.run(
            env["actor"],
            identifier,
            epoch,
            "receipt-worker",
            "echo",
            ["agent.tool_calling"],
            max_nodes=2,
            max_depth=1,
            model_cap=1,
            request_cap=10,
        )
    with env["app"].db.read() as db:
        ids = list(
            db.scalars(select(ExternalRequest.id).where(ExternalRequest.run_id == identifier))
        )
        observation = db.scalar(
            select(SearchObservation).join(ResearchNode).where(ResearchNode.run_id == identifier)
        )
        assert len(ids) == 1 and observation.payload["external_request_count"] == 1
        assert observation.payload["external_request_ids"] == ids


@pytest.mark.asyncio
async def test_public_system_github_request_can_continue_after_personal_erasure_fence(env):
    from careerforge.models_privacy_ext import PrivacyState

    app, actor, public_ns = env["app"], env["actor"], env["owner"]["public_namespace_id"]
    funding = app.runtime.create_funding(actor, public_ns, 2)
    identifier = app.runtime.create(
        actor, public_ns, funding, "research", {"github_request_cap": 1}, 1, "public-http"
    )
    with app.db.transaction() as db:
        state = db.get(PrivacyState, actor)
        state.generation += 1
        state.blocked = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"public": True}))
    ) as client:
        assert (
            await adapter(env, client)._get(
                "https://api.github.com/repos/a/b", identifier, 1, "public"
            )
            == b'{"public":true}'
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_during_http", [False, True])
async def test_private_dependency_http_response_cannot_outlive_scope_or_cache(
    env, revoke_during_http
):
    from support import FILES

    from careerforge.contracts import ImportSourceInput
    from careerforge.services.dependencies import invalidate
    from careerforge.services.security import create_principal, set_grant

    app, owner = env["app"], env["actor"]
    source = app.sources.import_source(
        owner,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixture/private-dependency",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
        ),
    )
    with app.db.transaction() as db:
        reader, ns, _ = create_principal(db, "github-private-dependency-reader")
        set_grant(db, owner, env["ns"], reader.id, "reader", True)
    funding = app.runtime.create_funding(reader.id, ns.id, 4)
    identifier = app.runtime.create(
        reader.id, ns.id, funding, "research", {"github_request_cap": 2}, 1, "scope"
    )

    def handler(request):
        if revoke_during_http:
            with app.db.transaction() as db:
                invalidate(db, owner, source, "revoked", "test scope withdrawal")
        return httpx.Response(
            200, json={"query_echo": "private-derived-term"}, headers={"ETag": "scoped"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = adapter(env, client)._get(
            "https://api.github.com/search/issues",
            identifier,
            2,
            "scoped",
            access_dependencies=[source],
        )
        if revoke_during_http:
            with pytest.raises(DomainError) as failure:
                await request
            assert failure.value.code == "GITHUB_AUTHORITY"
        else:
            assert b"private-derived-term" in await request
    with app.db.read() as db:
        assert not db.scalars(select(GitHubHttpCache)).all(), (
            "private cross-owner inputs disable persistent HTTP cache"
        )

@pytest.mark.asyncio
async def test_architecture_snapshot_follows_pinned_local_imports(env, monkeypatch):
    import base64, hashlib, json
    provider = GitHubProvider(env["app"].db, env["app"].settings)
    files = {f"packages/p{i}/index.ts": "export {};" for i in range(12)}
    files["packages/p0/index.ts"] = 'export { AgentSession } from "./core/agent-session.js";'
    files["packages/p0/core/agent-session.ts"] = "export class AgentSession {}\n" + "// large session implementation\n" * 6000
    blobs = {path: hashlib.sha1(b"blob " + str(len(text.encode())).encode() + b"\0" + text.encode()).hexdigest() for path, text in files.items()}
    async def get(url, run_id, cap, operation, **kwargs):
        if operation == "metadata":
            value = {"id": 1, "private": False, "license": {"spdx_id": "MIT"}, "language": "TypeScript", "size": 100_000}
        elif operation == "pin": value = {"sha": "a" * 40}
        elif operation == "quality:tree":
            assert "a" * 40 in url
            value = {"tree": [{"path": p, "sha": blobs[p], "type": "blob", "mode": "100644", "size": len(t.encode())} for p, t in files.items()]}
        else:
            p = next(p for p in files if url.endswith(blobs[p]))
            value = {"sha": blobs[p], "encoding": "base64", "content": base64.b64encode(files[p].encode()).decode()}
        return json.dumps(value).encode()
    monkeypatch.setattr(provider, "_get", get)
    result = await provider.snapshot("https://github.com/acme/project", "run", 30)
    assert "packages/p0/core/agent-session.ts" in result["files"]
    assert result["commit_sha"] == "a" * 40
    assert result["source_metadata"]["partial_snapshot"]
