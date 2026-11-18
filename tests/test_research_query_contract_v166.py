"""Explicit query grammar regressions; no model quality or network claims."""

import pytest
from test_research_goal_context_v166 import prepared_run, run_research

from careerforge.contracts import ModelOutput
from careerforge.errors import DomainError
from careerforge.services.sources import SourceService

OBSERVED_INVALID_QUERIES = [
    "(Python OR Java) REST (async OR 异步) tests in:readme fork:false",
    '("tool calling" OR "工具调用") (validation OR schema) in:readme fork:false',
    "Git CONTRIBUTING (Python OR Java) (tag OR release) in:readme fork:false",
]


@pytest.mark.asyncio
async def test_native_observed_or_query_is_rejected_before_local_first(env, monkeypatch):
    prepared = await prepared_run(env)
    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [
                {"query": OBSERVED_INVALID_QUERIES[0], "question": "Find a lead", "topic_keys": []}
            ],
            "stop_reason": None,
        },
    )

    def forbidden_local(*args, **kwargs):
        raise AssertionError(
            "Invalid generated query reached local-first before grammar validation"
        )

    monkeypatch.setattr(SourceService, "search_snapshots", forbidden_local)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "QUERY_SCOPE"
    assert env["repos"].calls == []


@pytest.mark.parametrize(
    "query,channel,expected",
    [
        *[(query, "repository", "QUERY_SCOPE") for query in OBSERVED_INVALID_QUERIES],
        ("storage or queue", "repository", "QUERY_SCOPE"),
        ("NOT storage", "repository", "QUERY_SCOPE"),
        ("storage IS:PRIVATE", "repository", "QUERY_SCOPE"),
        ("storage visibility:INTERNAL", "repository", "QUERY_SCOPE"),
        ("x", "repository", "QUERY"),
        ("x" * 181, "repository", "QUERY"),
        ("storage", "unknown", "QUERY"),
        ("orchestration notebook", "repository", None),
        ('"bounded storage" in:readme', "repository", None),
        ("repo:fixtures/storage release", "releases", None),
        ("bounded storage", "issues", None),
        ("bounded storage", "pull_requests", None),
        ("bounded storage", "code", "CODE_SEARCH_AUTH_REQUIRED"),
        ("bounded storage", "documentation", "CODE_SEARCH_AUTH_REQUIRED"),
    ],
)
@pytest.mark.asyncio
async def test_shared_grammar_preserves_actual_adapter_rejection_and_auth(
    env, monkeypatch, query, channel, expected
):
    from careerforge.adapters.github import GitHubProvider
    from careerforge.services.research_query import validate_public_search_query

    provider = GitHubProvider(env["app"].db, env["app"].settings)
    external = []

    async def no_io(url, run_id, request_cap, operation, params=None, **kwargs):
        external.append({"url": url, "operation": operation, "params": params})
        if operation == "release:metadata":
            return b'{"id": "fixture", "full_name": "fixtures/storage", "private": false}'
        if operation == "release:list":
            return b"[]"
        return b'{"items": [], "incomplete_results": false}'

    monkeypatch.setattr(provider, "_get", no_io)
    if expected in {"QUERY", "QUERY_SCOPE"}:
        with pytest.raises(DomainError) as pure:
            validate_public_search_query(query, channel)
        assert pure.value.code == expected
    else:
        validate_public_search_query(query, channel)
    if expected:
        with pytest.raises(DomainError) as adapter:
            await provider.search(query, "synthetic-preflight", 4, channel)
        assert adapter.value.code == expected
        assert external == []
    else:
        await provider.search(query, "synthetic-preflight", 4, channel)
        assert external
        if channel != "releases":
            assert external[0]["params"]["q"].startswith(query + " ")
            assert "is:public" in external[0]["params"]["q"]


@pytest.mark.asyncio
async def test_new_planner_and_reflector_see_grammar_without_losing_goal_logic(env):
    from test_research_goal_context_v166 import install_discovery_doubles

    from careerforge.services.research_planning import RESEARCH_PLANNING_CONTRACT, planning_identity
    from careerforge.services.research_query import (
        PUBLIC_QUERY_DENY_PATTERN,
        RESEARCH_QUERY_CONTRACT,
    )

    prepared = await prepared_run(env)
    install_discovery_doubles(env)
    result = await run_research(env, prepared)
    task = prepared[5]
    calls = [call for call in env["model"].calls if call["role"] in {"planner", "reflector"}]
    assert {call["role"] for call in calls} == {"planner", "reflector"}
    for call in calls:
        context = call["context"]
        assert context["research_task"] == task
        assert context["research_planning_contract"] == RESEARCH_PLANNING_CONTRACT
        assert context["research_query_contract"] == RESEARCH_QUERY_CONTRACT
        grammar = context["query_contract"]
        assert grammar["version"] == RESEARCH_QUERY_CONTRACT
        assert grammar["min_length"] == 2 and grammar["max_length"] == 180
        assert grammar["allowed_channels"] == [
            "repository",
            "code",
            "documentation",
            "issues",
            "pull_requests",
            "releases",
        ]
        assert grammar["disallowed_boolean_operators"] == ["OR", "NOT"]
        assert grammar["case_insensitive_rejection_pattern"] == PUBLIC_QUERY_DENY_PATTERN
        assert grammar["scope"] == "public_only"
        assert grammar["requirement_logic_is_not_query_syntax"] is True
        assert grammar["invalid_queries_are_not_rewritten_or_retried"] is True
        assert "不是 query 字符串的语法" in call["messages"][0]["content"]
        assert "private_jd" in call["data_classes"]
        assert task["goal_version_id"] in call["access_dependencies"]
        assert {row["modality"] for row in task["requirements"]} == {
            "required",
            "preferred",
            "bonus",
            "not_required",
            "unspecified",
        }
        assert {
            group["operator"] for row in task["requirements"] for group in row["group_path"]
        } == {"OR", "NOT"}
    assert result["research_query_contract"] == RESEARCH_QUERY_CONTRACT
    # Existing planning identity never relabels previously frozen V1 tasks.
    assert planning_identity(task)["research_planning_contract"] == RESEARCH_PLANNING_CONTRACT
    assert "research_query_contract" not in planning_identity(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", OBSERVED_INVALID_QUERIES)
async def test_invalid_paid_plan_is_preserved_without_rewrite_retry_or_branch(
    env, monkeypatch, query
):
    from sqlalchemy import func, select

    from careerforge.models import (
        AgentRun,
        ExternalRequest,
        ModelReceipt,
        ResearchNode,
        SearchObservation,
    )

    prepared = await prepared_run(env)
    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [{"query": query, "question": "Find a lead", "topic_keys": []}],
            "stop_reason": None,
        },
    )
    local_calls = []

    def forbidden_local(*args, **kwargs):
        local_calls.append(args)
        raise AssertionError("Query preflight must happen before local search")

    monkeypatch.setattr(SourceService, "search_snapshots", forbidden_local)
    before = len(env["model"].calls)
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            await run_research(env, prepared)
        assert error.value.code == "QUERY_SCOPE"
    assert len(env["model"].calls) == before + 1
    assert local_calls == [] and env["repos"].calls == []
    with env["app"].db.read() as db:
        run = db.get(AgentRun, prepared[2].id)
        assert run.calls_consumed == 1
        receipts = list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))
        assert len(receipts) == 1 and receipts[0].status == "completed"
        assert receipts[0].response["structured"]["queries"][0]["query"] == query
        assert (
            db.scalar(
                select(func.count()).select_from(ResearchNode).where(ResearchNode.run_id == run.id)
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(SearchObservation)) == 0
        assert db.scalar(select(func.count()).select_from(ExternalRequest)) == 0


@pytest.mark.asyncio
async def test_legal_query_uses_actual_local_snapshot_without_external_fallback(env, monkeypatch):
    from support import FILES
    from test_research_goal_context_v166 import install_discovery_doubles

    from careerforge.contracts import ImportSourceInput

    prepared = await prepared_run(env)
    app, actor = env["app"], env["actor"]
    source_id = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/bounded-storage",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    install_discovery_doubles(env)
    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [
                {"query": "echo return", "question": "Find a source lead", "topic_keys": []}
            ],
            "stop_reason": None,
        },
    )
    queries = []
    original = SourceService.search_snapshots

    def local(service, actor, query, **kwargs):
        queries.append(query)
        return original(service, actor, query, **kwargs)

    monkeypatch.setattr(SourceService, "search_snapshots", local)
    result = await run_research(env, prepared)
    assert queries == ["echo return"] and env["repos"].calls == []
    assert result["candidates"][0]["source_id"] == source_id
    assert result["candidates"][0]["verified_implementation"] is False


@pytest.mark.asyncio
async def test_invalid_reflector_query_stops_before_new_local_or_external_dispatch(
    env, monkeypatch
):
    from sqlalchemy import select
    from test_research_goal_context_v166 import install_discovery_doubles

    from careerforge.models import AgentRun, ModelReceipt, ResearchNode, SearchObservation

    prepared = await prepared_run(env)
    install_discovery_doubles(env)
    env["model"].overrides["reflector"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [{"query": "storage OR queue", "question": "A new lead?", "topic_keys": []}],
            "stop_reason": None,
        },
    )
    queries = []
    original = SourceService.search_snapshots

    def local(service, actor, query, **kwargs):
        queries.append(query)
        return original(service, actor, query, **kwargs)

    monkeypatch.setattr(SourceService, "search_snapshots", local)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "QUERY_SCOPE"
    assert queries == ["bounded storage implementation"]
    assert env["repos"].calls == ["bounded storage implementation"]
    with env["app"].db.read() as db:
        assert db.get(AgentRun, prepared[2].id).calls_consumed == 2
        receipts = list(
            db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == prepared[2].id))
        )
        assert len(receipts) == 2 and all(row.status == "completed" for row in receipts)
        reflection = next(row for row in receipts if row.role == "reflector")
        assert reflection.response["structured"]["queries"][0]["query"] == "storage OR queue"
        assert (
            len(list(db.scalars(select(ResearchNode).where(ResearchNode.run_id == prepared[2].id))))
            == 2
        )
        assert len(list(db.scalars(select(SearchObservation)))) == 1


async def versioned_run(env, version):
    from test_research_goal_context_v166 import build_input, extracted_goal

    from careerforge.models import AgentRun
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    document, goal_id = await extracted_goal(env)
    body = build_input(env, goal_id)
    run_id = app.build.start(actor, body)
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        value = dict(run.input_manifest)
        if version != "v2":
            value.pop("research_query_contract")
        if version == "legacy":
            value.pop("research_planning_contract")
        run.input_manifest = value
        run.input_hash = fingerprint(value)
    worker = "query-compatibility"
    epoch = app.runtime.claim(run_id, worker)
    assert epoch is not None
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    task = app.build._freeze_research_context(run, epoch, worker)["research_task"]
    return document, goal_id, run, epoch, worker, task, body


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "v1", "v2"])
async def test_exact_paid_requests_and_idempotent_admissions_keep_their_version(env, version):
    from copy import deepcopy

    from sqlalchemy import select
    from test_research_goal_context_v166 import install_discovery_doubles

    from careerforge.models import AgentRun, ModelReceipt, StageArtifact
    from careerforge.services.research_planning import PLANNING_TASK_INSTRUCTIONS, planning_identity
    from careerforge.services.research_query import (
        QUERY_CONTRACT_INSTRUCTIONS,
        query_contract_context,
    )
    from careerforge.util import fingerprint

    prepared = await versioned_run(env, version)
    install_discovery_doubles(env)
    first = await run_research(env, prepared, legacy=version == "legacy")
    calls = [
        deepcopy(call) for call in env["model"].calls if call["role"] in {"planner", "reflector"}
    ]
    run, task = prepared[2], prepared[5]
    planner_base = (
        "将问题分解为至多3个针对GitHub项目的不同检索问题。不要编造仓库。"
        "查询关注实现线索，不把README宣传当作源码事实。channel可为repository/code/documentation/issues/pull_requests/releases；代码类搜索需鉴权。空queries表示没有合理问题。"
    )
    reflector_base = (
        "基于实际检索观察寻找尚未回答的具体问题。只提出有新线索的检索，最多3个。"
        "不要重复原问题；无需扩展返回空queries及停止理由。不生成项目技术结论。"
    )
    for call in calls:
        expected_system = planner_base if call["role"] == "planner" else reflector_base
        if version != "legacy":
            expected_system += PLANNING_TASK_INSTRUCTIONS
        if version == "v2":
            expected_system += QUERY_CONTRACT_INSTRUCTIONS
        assert call["messages"][0]["content"] == expected_system
        if version != "v2":
            assert "query_contract" not in call["context"]
            assert "research_query_contract" not in call["context"]
        if version == "legacy":
            assert "research_task" not in call["context"]
            assert call["data_classes"] == ["learning_state", "private_source"]
        else:
            assert call["context"]["research_task"] == task
            assert all(
                call["context"][key] == value for key, value in planning_identity(task).items()
            )
            assert (
                "private_jd" in call["data_classes"] and prepared[1] in call["access_dependencies"]
            )
    planner_expected = {
        "question": prepared[6].question,
        "topic_keys": [],
        "remaining_nodes": 3,
        "research_heuristic": run.input_manifest["policies"]["research_heuristic"]["content"],
    }
    if version != "legacy":
        planner_expected.update(**planning_identity(task), research_task=task)
    if version == "v2":
        grammar = query_contract_context(run.input_manifest)
        planner_expected.update(
            **grammar, research_query_contract=grammar["query_contract"]["version"]
        )
    assert calls[0]["context"] == planner_expected
    before = len(env["model"].calls)
    with env["app"].db.transaction() as db:
        receipts = list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))
        assert {row.request_hash for row in receipts} == {fingerprint(call) for call in calls}
        db.delete(
            db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == "research.result"
                )
            )
        )
    assert await run_research(env, prepared, legacy=version == "legacy") == first
    assert len(env["model"].calls) == before
    assert env["app"].build.start(env["actor"], prepared[6]) == run.id
    with env["app"].db.read() as db:
        stored = db.get(AgentRun, run.id)
        assert ("research_query_contract" in stored.input_manifest) == (version == "v2")
        assert ("research_planning_contract" in stored.input_manifest) == (version != "legacy")


@pytest.mark.asyncio
async def test_v1_existing_generated_query_is_never_silently_rewritten_or_rejected_locally(env):
    from test_research_goal_context_v166 import install_discovery_doubles

    prepared = await versioned_run(env, "v1")
    install_discovery_doubles(env)
    query = OBSERVED_INVALID_QUERIES[0]
    env["model"].overrides["planner"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "queries": [{"query": query, "question": "Original V1 lead", "topic_keys": []}],
            "stop_reason": None,
        },
    )
    result = await run_research(env, prepared)
    assert env["repos"].calls == [query]
    assert "research_query_contract" not in result
    assert next(node for node in result["tree"] if node["parent_id"])["payload"]["query"] == query
    # The external adapter's historical guard still rejects it. No rule relaxed.
    from careerforge.services.research_query import validate_public_search_query

    with pytest.raises(DomainError) as error:
        validate_public_search_query(query, "repository")
    assert error.value.code == "QUERY_SCOPE"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unknown", "missing_planning", "forged_without_hash"])
async def test_unknown_or_tampered_query_contract_cannot_dispatch(env, change):
    from sqlalchemy import select

    from careerforge.models import AgentRun, StageArtifact
    from careerforge.util import fingerprint

    prepared = await prepared_run(env)
    with env["app"].db.transaction() as db:
        run = db.get(AgentRun, prepared[2].id)
        value = dict(run.input_manifest)
        if change == "missing_planning":
            value.pop("research_planning_contract")
        else:
            value["research_query_contract"] = "unknown-version"
        run.input_manifest = value
        if change != "forged_without_hash":
            run.input_hash = fingerprint(value)
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == "research_context"
                )
            )
            stage.input_hash = run.input_hash
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        await run_research(env, prepared)
    assert len(env["model"].calls) == before and env["repos"].calls == []


@pytest.mark.asyncio
async def test_persisted_node_is_checked_before_local_first_on_resume(env, monkeypatch):
    from sqlalchemy import select
    from test_research_goal_context_v166 import install_discovery_doubles

    from careerforge.models import ResearchNode, StageArtifact

    prepared = await prepared_run(env)
    install_discovery_doubles(env)
    await run_research(env, prepared)
    before = len(env["model"].calls)
    with env["app"].db.transaction() as db:
        db.delete(
            db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == prepared[2].id,
                    StageArtifact.stage_key == "research.result",
                )
            )
        )
        node = db.scalar(
            select(ResearchNode).where(
                ResearchNode.run_id == prepared[2].id, ResearchNode.parent_id.is_not(None)
            )
        )
        node.status = "observed"
        node.payload = {**node.payload, "query": "storage OR private"}
    local = []

    def forbidden(*args, **kwargs):
        local.append(args)
        raise AssertionError("Tampered invalid query must not reach local search")

    monkeypatch.setattr(SourceService, "search_snapshots", forbidden)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "QUERY_SCOPE"
    assert local == [] and len(env["model"].calls) == before
