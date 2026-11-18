from dataclasses import replace

import httpx
import pytest

from careerforge.adapters.model import OpenAICompatibleProvider, strict_schema
from careerforge.adapters.sandbox import DockerSandbox
from careerforge.config import Settings
from careerforge.contracts import QueryPlan
from careerforge.errors import DomainError


@pytest.mark.asyncio
async def test_http_provider_uses_structured_request_without_secret_persistence():
    seen = []

    def route(request):
        import json

        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-not-real-key"
        return httpx.Response(
            200,
            json={
                "id": "fixture-http",
                "choices": [
                    {
                        "message": {"content": '{"queries":[],"stop_reason":"none"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "untrusted_payload": "dont-persist",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as http:
        provider = OpenAICompatibleProvider(
            Settings(
                mode="test",
                model_base_url="https://provider.example/v1",
                model_api_key="test-not-real-key",
                model_name="configured-model",
            ),
            http,
        )
        r = await provider.complete(
            {
                "role": "planner",
                "messages": [{"role": "user", "content": "test"}],
                "max_output_tokens": 100,
                "schema": strict_schema(QueryPlan.model_json_schema()),
            }
        )
    assert r.structured["queries"] == [] and r.usage == {"prompt_tokens": 5, "completion_tokens": 3}
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    assert "max_completion_tokens" in seen[0]


@pytest.mark.asyncio
async def test_provider_error_is_not_retried():
    count = 0

    def error(request):
        nonlocal count
        count += 1
        return httpx.Response(429, json={"error": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(error)) as http:
        p = OpenAICompatibleProvider(
            Settings(
                mode="test",
                model_base_url="https://provider.example/v1",
                model_api_key="k",
                model_name="m",
            ),
            http,
        )
        with pytest.raises(DomainError):
            await p.complete({"role": "planner", "messages": [], "max_output_tokens": 100})
    assert count == 1


@pytest.mark.asyncio
async def test_embedding_and_rerank_http_contracts():
    def handle(req):
        if req.url.path.endswith("embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.2] * 8},
                        {"index": 0, "embedding": [0.1] * 8},
                    ],
                    "usage": {"prompt_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    s = Settings(
        mode="test",
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="k",
        embedding_model="e",
        embedding_dimensions=8,
        rerank_url="https://rerank.example/rerank",
        rerank_api_key="k",
        rerank_model="r",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        p = OpenAICompatibleProvider(s, c)
        e = await p.complete({"role": "embedding", "context": {"texts": ["a", "b"]}})
        r = await p.complete(
            {"role": "reranker", "context": {"query": "q", "documents": ["a", "b"]}}
        )
    assert e.structured["vectors"][0] == [0.1] * 8 and r.structured["scores"][0]["index"] == 1


def test_sandbox_unavailable_never_executes_host_code(monkeypatch):
    import careerforge.adapters.sandbox as sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    invoked = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **kw: invoked.append(a))
    with pytest.raises(DomainError):
        DockerSandbox().execute(
            {"image_digest": "sha256:" + "1" * 64, "files": {}}, ["python", "-c", "print(1)"]
        )
    assert not invoked


def test_postgresql_schema_compiles_without_claiming_live_db():
    from sqlalchemy import MetaData, create_mock_engine

    from careerforge.models import Base

    statements = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *a, **k: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    # PostgreSQL's ALTER TABLE compilation sets a create rule on deferred FKs.
    # Compile an independent graph, never mutate the metadata later used by
    # SQLite bootstrap (which needs these constraints inline).
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    base_constraints = {
        constraint: constraint._create_rule
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    }
    metadata.create_all(engine)
    assert all(c._create_rule is rule for c, rule in base_constraints.items())
    assert any("knowledge_embeddings" in x and "vector" in x for x in statements)
    assert any("artifact_dependencies" in x for x in statements)
    assert any("ADD CONSTRAINT fk_goal_version_addendum" in x for x in statements)
    assert any("ADD CONSTRAINT fk_workflow_current_execution" in x for x in statements)
    assert len(statements) > 40


@pytest.mark.asyncio
async def test_multichannel_github_observations_not_approved_knowledge(env):
    from careerforge.adapters.github import GitHubProvider

    app = env["app"]
    run = app.runtime.create(env["actor"], env["ns"], env["funding"], "research", {}, 1, "channels")

    async def handler(req):
        assert req.url.host == "api.github.com"
        if req.url.path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "incomplete_results": False,
                    "total_count": 1,
                    "items": [
                        {
                            "repository_url": "https://api.github.com/repos/acme/agent",
                            "html_url": "https://github.com/acme/agent/issues/2",
                            "title": "Tool errors",
                            "body": "An unverified discussion",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "incomplete_results": False,
                "total_count": 1,
                "items": [
                    {
                        "repository": {"id": 12, "full_name": "acme/agent", "private": False},
                        "path": "tools.py",
                        "html_url": "https://github.com/acme/agent/blob/main/tools.py",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubProvider(
            app.db, replace(app.settings, github_token="fixture-only-not-a-real-token"), client
        )
        code = await adapter.search("tool dispatch", run, 5, "code")
        issues = await adapter.search("tool error", run, 5, "issues")
        assert code["channel"] == "code" and code["leads"][0]["path"] == "tools.py"
        assert issues["candidates"][0]["name"] == "acme/agent"
        assert all(
            not c["verified_implementation"] for c in code["candidates"] + issues["candidates"]
        )
        denied = GitHubProvider(app.db, app.settings, client)
        with pytest.raises(DomainError):
            await denied.search("tool", run, 5, "code")


def test_source_input_preserves_exact_crlf_and_outer_whitespace(env):
    from careerforge.contracts import ImportSourceInput
    from careerforge.services.sources import SourceService

    raw = "\r\ndef ident(value):\r\n    return value\r\n\r\n"
    body = ImportSourceInput(
        namespace_id=env["ns"],
        repository_key="fixture/crlf",
        commit_sha="c" * 40,
        license_id="MIT",
        files={"main.py": raw},
    )
    assert body.files["main.py"] == raw
    identifier = env["app"].sources.import_source(env["actor"], body)
    with env["app"].db.read() as db:
        assert SourceService.get(db, env["actor"], identifier).files["main.py"] == raw
