"""Synthetic control-flow/scope tests; never assertions of live model quality."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import ImportSourceInput, ModelOutput, RetrievalTaskSpec, TaskType
from careerforge.contracts_retrieval_ext import IndexJobInput
from careerforge.models import KnowledgeEmbedding, Namespace, SourceSet


def task(question, source):
    return RetrievalTaskSpec(
        turn_id="rewrite-test",
        task_type=TaskType.LOCATE,
        user_question=question,
        project_source_ids=[source],
        focus_revision=1,
    )


def active_run(env, *, cap=4, roles=None, classes=None, key="query-rewrite"):
    app, actor, ns = env["app"], env["actor"], env["ns"]
    funding = app.runtime.create_funding(actor, ns, 20, roles=roles, data_classes=classes)
    identifier = app.runtime.create(actor, ns, funding, "research", {}, cap, key)
    epoch = app.runtime.claim(identifier, "test-worker")
    assert epoch is not None
    return identifier, epoch, "test-worker"


@pytest.mark.asyncio
async def test_chinese_alias_fallback_and_exact_symbol_span(env):
    source, publication, _ = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    before = len(env["model"].calls)
    chinese = task("工具调用", source).model_copy(update={"task_type": TaskType.CONCEPT})
    result = await app.retrieval.retrieve(actor, chinese)
    assert result["knowledge_units"][0]["publication_id"] == publication
    assert "agent.tool_calling" in result["query_normalization"]["selected_topic_keys"]
    assert len(env["model"].calls) == before
    exact = await app.retrieval.retrieve(actor, task("echo", source))
    assert exact["symbols"][0]["symbol"] == "echo"
    assert exact["knowledge_units"][0]["publication_id"] == publication


@pytest.mark.asyncio
async def test_rewriter_actual_gap_path_is_funded_and_scope_limited(env):
    source, publication, _ = await publish_fixture(env)
    env["model"].overrides["query_rewriter"] = lambda request: ModelOutput(
        synthetic=True,
        structured={"variants": ["echo"], "selected_topic_keys": [], "reason": "symbol_hint"},
    )
    run = active_run(env)
    result = await env["app"].retrieval.retrieve(
        env["actor"], task("quux_unknown_locator", source), run
    )
    assert result["knowledge_units"][0]["publication_id"] == publication
    calls = [r for r in env["model"].calls if r["role"] == "query_rewriter"]
    assert len(calls) == 1 and calls[0]["context"]["original_query"] == "quux_unknown_locator"
    assert result["query_normalization"]["facts_approved"] is False
    assert any(stage.startswith("query.rewrite:") for stage in _stage_names(env, run[0]))


def _stage_names(env, run_id):
    from careerforge.models import StageArtifact

    with env["app"].db.read() as db:
        return list(
            db.scalars(select(StageArtifact.stage_key).where(StageArtifact.run_id == run_id))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles,classes,cap", [(["planner"], None, 4), (None, ["learning_state"], 4), (None, None, 1)]
)
async def test_rewriter_cannot_create_authority_or_spend_followup_reserve(env, roles, classes, cap):
    source, _, _ = await publish_fixture(env)
    run = active_run(env, roles=roles, classes=classes, cap=cap)
    before = len(env["model"].calls)
    result = await env["app"].retrieval.rewrite_query(
        env["actor"], task("不存在符号", source), ["不存在符号"], run
    )
    assert result["status"] == "aliases_only" and len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_rewriter_rejects_unapproved_topics_preserves_original(env):
    source, _, _ = await publish_fixture(env)
    env["model"].overrides["query_rewriter"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "variants": ["echo"],
            "selected_topic_keys": ["private.stolen"],
            "reason": "symbol_hint",
        },
    )
    result = await env["app"].retrieval.rewrite_query(
        env["actor"], task("查询", source), ["查询"], active_run(env)
    )
    assert result["status"] == "rewrite_unavailable"
    assert result["variants"] == ["查询"] and result["reason"] == "QUERY_REWRITE_SCOPE"


@pytest.mark.asyncio
async def test_batch_index_real_job_and_rebuild_use_frozen_publications(env):
    _, first, _ = await publish_fixture(env, "one", "fixture/one")
    _, second, _ = await publish_fixture(env, "two", "fixture/two", "b" * 40)
    app = env["app"]
    body = IndexJobInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        publication_ids=[first, second],
        batch_size=8,
        call_cap=1,
        idempotency_key="index",
        confirm="INDEX",
    )
    run = app.retrieval.start_index(env["actor"], body)
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result and set(result["indexed"]) == {first, second}
    embedding_calls = [c for c in env["model"].calls if c["role"] == "embedding"]
    assert len(embedding_calls) == 1 and len(embedding_calls[0]["context"]["texts"]) == 2
    with app.db.read() as db:
        expected_dependencies = set().union(
            *(
                set(
                    app.knowledge.pack_dependencies(
                        app.knowledge.publication_pack(db, env["actor"], pid)
                    )
                )
                for pid in [first, second]
            )
        )
    assert expected_dependencies <= set(embedding_calls[0]["access_dependencies"])
    assert result["semantic_quality_validated"] is False
    run2 = app.retrieval.start_index(
        env["actor"], body.model_copy(update={"idempotency_key": "reuse"})
    )
    await app.drain()
    assert set(app.runtime.stage(run2, "result")["already_current"]) == {first, second}
    run3 = app.retrieval.start_index(
        env["actor"], body.model_copy(update={"idempotency_key": "rebuild", "rebuild": True})
    )
    await app.drain()
    assert set(app.runtime.stage(run3, "result")["indexed"]) == {first, second}
    assert len([c for c in env["model"].calls if c["role"] == "embedding"]) == 2
    with app.db.read() as db:
        assert len(db.scalars(select(KnowledgeEmbedding)).all()) == 2


@pytest.mark.asyncio
async def test_index_scope_changes_before_worker_fail_without_model_call(env):
    _, publication, _ = await publish_fixture(env)
    app = env["app"]
    run = app.retrieval.start_index(
        env["actor"],
        IndexJobInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            publication_ids=[publication],
            call_cap=1,
            idempotency_key="changed",
            confirm="INDEX",
        ),
    )
    with app.db.transaction() as db:
        db.get(Namespace, env["ns"]).revision += 1
    before = len(env["model"].calls)
    await app.drain()
    assert app.runtime.stage(run, "result") is None
    assert len(env["model"].calls) == before


def test_source_import_in_rolls_back_with_parent_transaction(env):
    app = env["app"]
    with pytest.raises(RuntimeError):
        with app.db.transaction() as db:
            identifier = app.sources.import_source_in(
                db,
                env["actor"],
                ImportSourceInput(
                    namespace_id=env["ns"],
                    repository_key="fixture/atomic",
                    commit_sha="a" * 40,
                    license_id="MIT",
                    files={"x.py": "pass\n"},
                ),
            )
            raise RuntimeError("abort enclosing archive")
    with app.db.read() as db:
        assert db.get(SourceSet, identifier) is None


@pytest.mark.asyncio
async def test_local_snapshot_discovery_is_private_scope_and_not_reviewed(env):
    source, _, _ = await publish_fixture(env)
    app = env["app"]
    local = app.sources.search_snapshots(env["actor"], "repo:fixtures/agent echo")
    assert local["candidates"][0]["source_id"] == source
    assert not local["candidates"][0]["verified_implementation"]
    assert app.sources.search_snapshots(env["actor"], "echo", public_only=True)["candidates"] == []
    with app.db.transaction() as db:
        from careerforge.services.security import create_principal

        outsider, _, _ = create_principal(db, "outsider")
    assert app.sources.search_snapshots(outsider.id, "echo")["candidates"] == []


def test_benchmark_metrics_have_explicit_gold_and_do_not_hide_empty_facets():
    path = Path(__file__).parents[1] / "scripts/retrieval_benchmark.py"
    spec = importlib.util.spec_from_file_location("cf_retrieval_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.metrics(["wrong", "hit"], ["hit", "miss"], ["outside", "in"], ["in"], [], [], 2)
    assert result["recall_at_k"] == 0.5 and result["mrr"] == 0.5
    assert result["project_scope_precision"] == 0.5 and result["facet_coverage"] is None
    with pytest.raises(ValueError):
        module.metrics([], [], [], [], [], [], 5)
    assert module.percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)


@pytest.mark.asyncio
async def test_research_local_observation_counts_zero_actual_http_requests(env):
    from careerforge.models import ResearchNode, SearchObservation

    source, _, _ = await publish_fixture(env)
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
    run = active_run(env, cap=1)
    result = await env["app"].research.run(
        env["actor"],
        *run,
        "echo",
        ["agent.tool_calling"],
        max_nodes=2,
        max_depth=1,
        model_cap=1,
        request_cap=5,
    )
    assert result["candidates"][0]["source_id"] == source
    assert not env["repos"].calls
    with env["app"].db.read() as db:
        observation = db.scalar(
            select(SearchObservation).join(ResearchNode).where(ResearchNode.run_id == run[0])
        )
        assert observation.payload["external_request_count"] == 0
        assert observation.payload["external_request_ids"] == []


@pytest.mark.asyncio
async def test_public_system_index_has_no_dependency_on_admin_personal_data(env):
    from careerforge.models_privacy_ext import PrivacyState

    app, actor = env["app"], env["actor"]
    public_ns = env["owner"]["public_namespace_id"]
    _, publication, _ = await publish_fixture(
        env, "public-index", "fixture/public-index", namespace=public_ns
    )
    funding = app.runtime.create_funding(actor, public_ns, 2)
    identifier = app.runtime.create(
        actor, public_ns, funding, "research", {}, 1, "public-index-run"
    )
    epoch = app.runtime.claim(identifier, "system-index")
    with app.db.transaction() as db:
        state = db.get(PrivacyState, actor)
        state.generation += 1
        state.blocked = True
    result = await app.retrieval.index_vectors(
        actor, [publication], (identifier, epoch, "system-index")
    )
    assert result["indexed"] == [publication]


@pytest.mark.asyncio
async def test_optional_rewriter_reserves_vector_rerank_and_answer_calls(env):
    from careerforge.models import AgentRun

    source, _, _ = await publish_fixture(env, "budget-a", "fixture/budget-a")
    other, _, _ = await publish_fixture(env, "budget-b", "fixture/budget-b", "b" * 40)
    model = env["model"]
    model.supports_embeddings = model.supports_reranking = True
    model.overrides["query_rewriter"] = lambda request: ModelOutput(
        synthetic=True,
        structured={"variants": ["echo"], "selected_topic_keys": [], "reason": "symbol_hint"},
    )
    run = active_run(env, cap=3)
    spec = task("quux_unknown_budget_term", source).model_copy(
        update={"task_type": TaskType.CONCEPT, "project_source_ids": [source, other]}
    )
    await env["app"].retrieval.retrieve(env["actor"], spec, run)
    assert not [c for c in model.calls if c["role"] == "query_rewriter"]
    with env["app"].db.read() as db:
        current = db.get(AgentRun, run[0])
        assert current.calls_consumed == 2 and current.call_cap - current.calls_consumed == 1


@pytest.mark.asyncio
async def test_funded_replay_prefix_isolates_both_model_steps_and_cache(env):
    source, _, _ = await publish_fixture(env)
    env["model"].overrides["query_rewriter"] = lambda request: ModelOutput(
        synthetic=True,
        structured={"variants": ["echo"], "selected_topic_keys": [], "reason": "symbol_hint"},
    )
    run = active_run(env, cap=4)
    spec = task("quux_replay_partition", source)
    for prefix in ["base:", "candidate:"]:
        result = await env["app"].retrieval.retrieve(env["actor"], spec, run, step_prefix=prefix)
        assert result["knowledge_units"]
    assert len([c for c in env["model"].calls if c["role"] == "query_rewriter"]) == 2
    names = _stage_names(env, run[0])
    assert any(k.startswith("base:query.rewrite:") for k in names)
    assert any(k.startswith("candidate:query.rewrite:") for k in names)
