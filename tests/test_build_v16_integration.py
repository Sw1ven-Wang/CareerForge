"""Central v1.6 contracts: fixed taxonomy, local source reuse and late-write fences."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select
from support import FILES
from test_taxonomy_governance_v16 import approve, definition, evidence_fixture, propose

from careerforge.adapters.model import OpenAICompatibleProvider, strict_schema
from careerforge.config import Settings
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt, StageArtifact
from careerforge.services.build import BuildService
from careerforge.services.dependencies import invalidate
from careerforge.services.investigation import Investigation
from careerforge.services.security import create_principal, set_grant


def source_for(env, actor=None, namespace=None):
    return env["app"].sources.import_source(
        actor or env["actor"],
        ImportSourceInput(
            namespace_id=namespace or env["ns"],
            repository_key="fixtures/agent",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )


def test_analysis_wire_schema_has_only_explicit_scalar_object_fields():
    schema = strict_schema(
        BuildService._analysis_schema(
            [
                {
                    "facets": {
                        "settings": {
                            "value_type": "object",
                            "object_fields": {
                                "mode": "string",
                                "count": "number",
                                "enabled": "boolean",
                            },
                        }
                    }
                }
            ]
        )
    )
    alternatives = schema["$defs"]["JsonValue"]["anyOf"]
    shape = next(v for v in alternatives if v["type"] == "object")
    assert shape["additionalProperties"] is False
    assert set(shape["required"]) == {"mode", "count", "enabled"}
    assert shape["properties"]["count"]["type"] == "number"

    def closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
            for value in node.values():
                closed(value)
        elif isinstance(node, list):
            for value in node:
                closed(value)

    closed(schema)
    with pytest.raises(DomainError):
        BuildService._analysis_schema(
            [{"facets": {"bad": {"value_type": "object", "object_fields": {}}}}]
        )


async def test_closed_analysis_schema_uses_strict_wire_mode_without_paid_request():
    seen = []

    def mock(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"units":[]}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
        provider = OpenAICompatibleProvider(
            Settings(
                mode="test",
                model_base_url="https://mock.invalid",
                model_api_key="mock-only-not-a-real-key",
                model_name="mock-model",
                structured_output="auto",
            ),
            client,
        )
        await provider.complete(
            {
                "role": "analysis",
                "messages": [{"role": "user", "content": "test"}],
                "max_output_tokens": 10,
                "schema": strict_schema(BuildService._analysis_schema([])),
            }
        )
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["response_format"]["json_schema"]["strict"] is True


async def test_public_source_does_not_authorize_private_taxonomy_egress(env):
    source, evidence = await evidence_fixture(env, public=True)
    governance, proposal = propose(env, evidence, [definition("sensitive_topic")])
    approve(env, governance, proposal)
    app, actor = env["app"], env["actor"]
    funding = app.runtime.create_funding(actor, env["ns"], 20, data_classes=["public_source"])
    before = len(env["model"].calls)
    run_id = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=funding,
            source_id=source,
            question="Explain code using registered taxonomy",
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="private-taxonomy-egress",
        ),
    )
    await app.drain()
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        root = db.get(AgentRun, run_id)
        assert root.input_manifest["taxonomy_data_classes"] == ["private_source"]
        assert db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all() == []
    # New research contracts retain the denial and separately expose that the
    # original task remains incomplete. The private egress must still not occur.
    assert app.runtime.stage(run_id, "result")["gaps"] == [
        "analysis_failed",
        "task_coverage_incomplete",
    ]


async def test_local_research_reuses_exact_snapshot_without_redownload(env, monkeypatch):
    source = source_for(env)

    async def forbidden(*args, **kwargs):
        raise AssertionError("local discovery must not fetch a moving remote HEAD")

    monkeypatch.setattr(env["repos"], "search", forbidden)
    monkeypatch.setattr(env["repos"], "snapshot", forbidden)
    run = env["app"].build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="tool registry agent dispatch",
            topic_keys=["agent.tool_calling"],
            call_cap=16,
            max_units_per_project=1,
            max_projects=1,
            idempotency_key="reuse-local-source",
        ),
    )
    await env["app"].drain()
    stage = env["app"].runtime.stage(run, "sources")
    assert stage["source_ids"] == [source]
    assert env["app"].runtime.stage(run, "result")["publication_ids"]


def foreign_investigation(env):
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "source-owner")
        set_grant(db, other.id, ns.id, actor, "reader", True)
        owner, namespace = other.id, ns.id
    source = source_for(env, owner, namespace)
    run = app.runtime.create(
        actor, env["ns"], env["funding"], "analysis", {"source_ids": [source]}, 4, "foreign-read"
    )
    epoch = app.runtime.claim(run, "reader")
    investigation = Investigation(
        app.sources,
        app.runtime,
        app.gateway,
        actor,
        run,
        epoch,
        "reader",
        source,
        "analysis",
        "foreign",
    )
    return owner, source, run, investigation


def test_source_revocation_between_read_and_stage_cannot_restore_body(env, monkeypatch):
    owner, source, run, investigation = foreign_investigation(env)
    original = env["app"].sources.read_span

    def read_then_revoke(*args, **kwargs):
        body = original(*args, **kwargs)
        with env["app"].db.transaction() as db:
            invalidate(db, owner, source, "revoked", "revoke-before-stage-commit")
        return body

    monkeypatch.setattr(env["app"].sources, "read_span", read_then_revoke)
    with pytest.raises(DomainError) as exc:
        investigation.execute_tool(
            "span", "read_span", {"path": "src/tools.py", "start_line": 1, "end_line": 3}
        )
    assert exc.value.code in {"EVIDENCE_UNAVAILABLE", "NOT_FOUND"}
    with env["app"].db.read() as db:
        assert db.scalar(select(StageArtifact).where(StageArtifact.run_id == run)) is None


def test_cached_tool_read_is_reauthorized_after_source_revocation(env):
    owner, source, _, investigation = foreign_investigation(env)
    args = {"path": "src/tools.py", "start_line": 1, "end_line": 3}
    assert "return value" in investigation.execute_tool("span", "read_span", args)["content"]
    with env["app"].db.transaction() as db:
        invalidate(db, owner, source, "revoked", "revoke-cached-read")
    with pytest.raises(DomainError):
        investigation.execute_tool("span", "read_span", args)


async def test_unfrozen_legacy_task_requires_new_explicit_build(env):
    source = source_for(env)
    run_id = env["app"].runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "knowledge_build",
        {"source_id": source},
        10,
        "old-unfrozen-task",
    )
    epoch = env["app"].runtime.claim(run_id, "old-worker")
    with env["app"].db.read() as db:
        run = db.get(AgentRun, run_id)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as exc:
        await env["app"].build.root(run, epoch, "old-worker")
    assert exc.value.code == "BUILD_SCHEMA_UPGRADE_REQUIRED"
    assert len(env["model"].calls) == before
