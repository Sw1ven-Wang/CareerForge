"""Generic planner input/integrity regressions, not model-quality evaluations."""

from copy import deepcopy

import pytest

from careerforge.contracts import KnowledgeBuildInput, ModelOutput
from careerforge.contracts_jd_v166 import JDExtractionInput, JDImportInput


def generic_extraction(document):
    requirements, groups = [], []
    specs = [
        (0, "pebble", "Pebble", "熟悉", "required", "OR", "storage"),
        (0, "lmdb", "LMDB", "熟悉", "required", "OR", "storage"),
        (1, "observability", "可观测性", "掌握", "preferred", "SINGLE", None),
        (2, "compression", "压缩算法", "了解", "bonus", "SINGLE", None),
        (3, "graphics", "图形渲染", "熟悉", "not_required", "NOT", "excluded"),
        (4, "checksum", "文件校验", "了解", "required", "SINGLE", None),
        (5, "queue", "队列", None, "unspecified", "SINGLE", None),
    ]
    for index, key, technology, depth, modality, operator, group in specs:
        paragraph = document["paragraphs"][index]
        span = {
            "quote": paragraph["text"],
            **{field: paragraph[field] for field in ("paragraph_id", "span_start", "span_end")},
        }
        requirements.append(
            {
                **span,
                "key": key,
                "technology": technology,
                "keywords": [technology],
                "depth": depth,
                "depth_basis": "explicit" if depth else "unspecified",
                "modality": modality,
                "group_operator": operator,
                "group_key": group,
                "topic_key": None,
            }
        )
        if key in {"pebble", "graphics"}:
            groups.append(
                {
                    **span,
                    "key": group,
                    "operator": operator,
                    "member_keys": ["pebble", "lmdb"] if key == "pebble" else [key],
                }
            )
    return {"requirements": requirements, "groups": groups, "limitations": []}


async def extracted_goal(env):
    app, actor = env["app"], env["actor"]
    env["model"].overrides["extract"] = lambda request: ModelOutput(
        synthetic=True, structured=generic_extraction(request["context"]["jd_document"])
    )
    document = app.jd.import_jd(
        actor,
        JDImportInput(
            namespace_id=env["ns"],
            jd_id="generic-storage-role",
            raw_text=(
                "熟悉 Pebble 或 LMDB。\n优先：掌握可观测性。\n加分：了解压缩算法。\n"
                "不要求熟悉图形渲染。\n了解文件校验。\n涉及队列，程度未说明。"
            ),
        ),
    )
    started = app.jd.start_extraction(
        actor,
        JDExtractionInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            jd_revision_id=document["jd_revision_id"],
            idempotency_key="generic-goal",
        ),
    )
    await app.drain()
    result = app.runtime.stage(started["run_id"], "result")
    assert result and result.get("goal_version_id")
    return document, result["goal_version_id"]


def build_input(env, goal_id, **changes):
    return KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        goal_version_id=goal_id,
        question="Find public implementations relevant to the frozen role requirements.",
        topic_keys=[],
        max_projects=1,
        max_units_per_project=1,
        call_cap=20,
        duration_minutes=60,
        planning_call_seconds=30,
        max_nodes=4,
        max_depth=2,
        idempotency_key="discover-generic-goal",
        **changes,
    )


def install_discovery_doubles(env):
    def plan(request):
        context = request["context"]
        if "candidate_catalog" in context:
            return ModelOutput(
                synthetic=True,
                structured={
                    "selected_candidate_ids": [],
                    "decisions": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "decision": "defer",
                            "reason_code": "insufficient_observation",
                            "observation_ids": [],
                            "possible_related_requirements": [],
                            "uncertainties": ["synthetic fixture does not establish relevance"],
                        }
                        for candidate in context["candidate_catalog"]
                    ],
                },
            )
        return ModelOutput(
            synthetic=True,
            structured={
                "queries": [
                    {
                        "query": "bounded storage implementation",
                        "question": "Which public implementation can be reviewed?",
                        "topic_keys": [],
                        "channel": "repository",
                    }
                ],
                "stop_reason": None,
            },
        )

    env["model"].overrides["planner"] = plan
    env["model"].overrides["reflector"] = lambda request: ModelOutput(
        synthetic=True, structured={"queries": [], "stop_reason": "bounded_fixture_complete"}
    )


@pytest.mark.asyncio
async def test_build_passes_exact_goal_qualifiers_to_planner_and_reflection(env):
    app, actor = env["app"], env["actor"]
    document, goal_id = await extracted_goal(env)
    install_discovery_doubles(env)
    profile = deepcopy(app.learning.profile(actor))
    run_id = app.build.start(actor, build_input(env, goal_id))
    await app.drain()
    task = app.runtime.stage(run_id, "research_context")["research_task"]
    calls = [
        call
        for call in env["model"].calls
        if call["role"] in {"planner", "reflector"} and "candidate_catalog" not in call["context"]
    ]
    assert {call["role"] for call in calls} == {"planner", "reflector"}
    for call in calls:
        assert call["context"].get("research_task") == task
        assert call["context"]["topic_keys"] == ["ops.observability"]
        assert "private_jd" in call["data_classes"]
        assert goal_id in call["access_dependencies"]
        requirements = call["context"]["research_task"]["requirements"]
        assert len(requirements) == 7
        assert {row["modality"] for row in requirements} == {
            "required",
            "preferred",
            "bonus",
            "not_required",
            "unspecified",
        }
        assert all(row["jd_revision_id"] == document["jd_revision_id"] for row in requirements)
        assert all(row["raw_sha256"] == document["raw_sha256"] for row in requirements)
        by_technology = {row["technology"]: row for row in requirements}
        assert by_technology["Pebble"]["group_path"] == by_technology["LMDB"]["group_path"]
        assert by_technology["Pebble"]["group_operator"] == "OR"
        assert by_technology["图形渲染"]["group_operator"] == "NOT"
        assert by_technology["队列"]["depth"] is None
    assert app.learning.profile(actor) == profile


async def prepared_run(env, *, legacy=False):
    from careerforge.models import AgentRun
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    document, goal_id = await extracted_goal(env)
    body = build_input(env, goal_id)
    run_id = app.build.start(actor, body)
    if legacy:
        # Simulate an already-admitted pre-contract run, before any stage exists.
        with app.db.transaction() as db:
            run = db.get(AgentRun, run_id)
            run.input_manifest = {
                key: value
                for key, value in run.input_manifest.items()
                if key not in {"research_planning_contract", "research_query_contract"}
            }
            run.input_hash = fingerprint(run.input_manifest)
    worker = "goal-context-test"
    epoch = app.runtime.claim(run_id, worker)
    assert epoch is not None
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    context = app.build._freeze_research_context(run, epoch, worker)
    return document, goal_id, run, epoch, worker, context["research_task"], body


async def run_research(env, prepared, *, task_override=None, legacy=False):
    _, _, run, epoch, worker, task, _ = prepared
    return await env["app"].research.run(
        env["actor"],
        run.id,
        epoch,
        worker,
        run.input_manifest["question"],
        [],
        4,
        2,
        3,
        10,
        policy=run.input_manifest["policies"]["research_heuristic"]["content"],
        **({} if legacy else {"research_task": task if task_override is None else task_override}),
    )


@pytest.mark.asyncio
async def test_task_identity_is_bound_to_requests_tree_results_and_paid_replay(env):
    from sqlalchemy import select

    from careerforge.models import ModelReceipt, ResearchNode, StageArtifact
    from careerforge.services.research_planning import planning_identity

    prepared = await prepared_run(env)
    _, goal_id, run, _, _, task, _ = prepared
    install_discovery_doubles(env)
    first = await run_research(env, prepared)
    identity = planning_identity(task)
    assert all(first[key] == value for key, value in identity.items())
    assert goal_id in first["access_dependencies"]
    before = len(env["model"].calls)
    assert await run_research(env, prepared) == first
    assert len(env["model"].calls) == before
    with env["app"].db.transaction() as db:
        root = db.scalar(
            select(ResearchNode).where(
                ResearchNode.run_id == run.id, ResearchNode.parent_id.is_(None)
            )
        )
        assert all(root.payload[key] == value for key, value in identity.items())
        # A resumed unfinished result reconstructs the exact already-paid request.
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == "research.result"
            )
        )
        db.delete(stage)
        hashes = list(
            db.scalars(select(ModelReceipt.request_hash).where(ModelReceipt.run_id == run.id))
        )
    assert await run_research(env, prepared) == first
    assert len(env["model"].calls) == before
    with env["app"].db.read() as db:
        assert (
            list(db.scalars(select(ModelReceipt.request_hash).where(ModelReceipt.run_id == run.id)))
            == hashes
        )
    for call in env["model"].calls:
        if call["role"] in {"planner", "reflector"}:
            assert all(call["context"][key] == value for key, value in identity.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["question", "goal", "modality", "or_group", "missing"])
async def test_caller_cannot_replace_or_omit_the_frozen_task(env, change):
    from careerforge.errors import DomainError

    prepared = await prepared_run(env)
    task = deepcopy(prepared[5])
    if change == "question":
        task["original_question"] = "Different question"
    elif change == "goal":
        task["goal_version_id"] = "foreign-goal"
    elif change == "modality":
        next(row for row in task["requirements"] if row["modality"] == "bonus")["modality"] = (
            "required"
        )
    elif change == "or_group":
        next(row for row in task["requirements"] if row["group_operator"] == "OR")["group_path"][0][
            "operator"
        ] = "AND"
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared, task_override=task, legacy=change == "missing")
    assert error.value.code == "RESEARCH_PLANNING_CONTRACT"
    assert len(env["model"].calls) == before
    assert env["repos"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["task", "goal_row", "goal_revoke", "jd_revoke", "privacy", "version"]
)
async def test_persisted_identity_privacy_and_revocation_are_rechecked_before_dispatch(env, change):
    from sqlalchemy import select

    from careerforge.errors import DomainError
    from careerforge.models import AgentRun, GoalVersion, ObjectRow, StageArtifact
    from careerforge.models_privacy_ext import PrivacyState
    from careerforge.util import fingerprint

    prepared = await prepared_run(env)
    document, goal_id, run, _, _, _, _ = prepared
    with env["app"].db.transaction() as db:
        if change == "task":
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == "research_context"
                )
            )
            value = deepcopy(stage.payload)
            value["research_task"]["requirements"] = []
            stage.payload = value
        elif change == "goal_row":
            db.get(GoalVersion, goal_id).content_hash = "0" * 64
        elif change == "goal_revoke":
            db.get(ObjectRow, goal_id).state = "revoked"
        elif change == "jd_revoke":
            db.get(ObjectRow, document["jd_revision_id"]).state = "revoked"
        elif change == "privacy":
            db.get(PrivacyState, env["actor"]).blocked = True
        else:
            stored = db.get(AgentRun, run.id)
            stored.input_manifest = {
                **stored.input_manifest,
                "research_planning_contract": "unknown-version",
            }
            stored.input_hash = fingerprint(stored.input_manifest)
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        await run_research(env, prepared)
    assert len(env["model"].calls) == before
    assert env["repos"].calls == []


@pytest.mark.asyncio
async def test_private_jd_class_cannot_be_omitted_from_funding(env):
    from careerforge.errors import DomainError
    from careerforge.models import AgentRun, FundingAgreement

    prepared = await prepared_run(env)
    with env["app"].db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        funding.allowed_data_classes = [
            value for value in funding.allowed_data_classes if value != "private_jd"
        ]
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "EGRESS_OR_ROLE_DENIED"
    assert len(env["model"].calls) == before
    with env["app"].db.read() as db:
        assert db.get(AgentRun, prepared[2].id).calls_consumed == 0


@pytest.mark.asyncio
async def test_revoked_jd_after_planner_prevents_search_and_reflection(env):
    from careerforge.errors import DomainError
    from careerforge.models import ObjectRow

    prepared = await prepared_run(env)
    install_discovery_doubles(env)
    planner = env["model"].overrides["planner"]

    def revoke_after_plan(request):
        output = planner(request)
        with env["app"].db.transaction() as db:
            db.get(ObjectRow, prepared[0]["jd_revision_id"]).state = "revoked"
        return output

    env["model"].overrides["planner"] = revoke_after_plan
    with pytest.raises(DomainError):
        await run_research(env, prepared)
    assert env["repos"].calls == []
    assert not any(call["role"] == "reflector" for call in env["model"].calls)


@pytest.mark.asyncio
async def test_legacy_queued_plan_reflection_and_exact_paid_replay_are_unchanged(env):
    from sqlalchemy import select

    from careerforge.contracts import QueryPlan
    from careerforge.models import AgentRun, ModelReceipt, StageArtifact
    from careerforge.services.research import ResearchService
    from careerforge.util import fingerprint

    prepared = await prepared_run(env, legacy=True)
    _, goal_id, run, _, _, task, body = prepared
    install_discovery_doubles(env)
    result = await run_research(env, prepared, legacy=True)
    calls = [call for call in env["model"].calls if call["role"] in {"planner", "reflector"}]
    assert {call["role"] for call in calls} == {"planner", "reflector"}
    assert "research_task_hash" not in result and goal_id not in result["access_dependencies"]
    assert calls[0]["context"] == {
        "question": body.question,
        "topic_keys": [],
        "remaining_nodes": 3,
        "research_heuristic": run.input_manifest["policies"]["research_heuristic"]["content"],
    }
    assert set(calls[1]["context"]) == {
        "root_question",
        "branch_question",
        "observations",
        "known_repository_count",
        "topic_keys",
        "remaining_nodes",
        "research_heuristic",
    }
    assert calls[0]["messages"][0]["content"] == (
        "将问题分解为至多3个针对GitHub项目的不同检索问题。不要编造仓库。"
        "查询关注实现线索，不把README宣传当作源码事实。channel可为repository/code/documentation/issues/pull_requests/releases；代码类搜索需鉴权。空queries表示没有合理问题。"
    )
    assert calls[1]["messages"][0]["content"] == (
        "基于实际检索观察寻找尚未回答的具体问题。只提出有新线索的检索，最多3个。"
        "不要重复原问题；无需扩展返回空queries及停止理由。不生成项目技术结论。"
    )
    from careerforge.adapters.model import strict_schema

    for call in calls:
        assert call["schema"] == strict_schema(QueryPlan.model_json_schema())
        assert call["data_classes"] == ["learning_state", "private_source"]
        assert goal_id not in call["access_dependencies"]
    before = len(env["model"].calls)
    with env["app"].db.transaction() as db:
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id, ModelReceipt.stage_key == "research.initial_plan"
            )
        )
        assert receipt.request_hash == fingerprint(calls[0])
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == "research.result"
            )
        )
        db.delete(stage)
    assert await run_research(env, prepared, legacy=True) == result
    assert len(env["model"].calls) == before
    assert env["app"].build.start(env["actor"], body) == run.id
    with env["app"].db.read() as db:
        assert "research_planning_contract" not in db.get(AgentRun, run.id).input_manifest
    # Passing a new task to a legacy run is an explicit error, not a silent upgrade.
    from careerforge.errors import DomainError

    with pytest.raises(DomainError, match="固定研究任务"):
        await run_research(env, prepared)
    assert ResearchService._data_classes(env["app"].research, run.id) == [
        "learning_state",
        "private_source",
    ]


@pytest.mark.asyncio
async def test_context_bound_fails_closed_without_truncation_or_spending(env, monkeypatch):
    from careerforge.errors import DomainError
    from careerforge.services import research_planning
    from careerforge.util import canonical

    prepared = await prepared_run(env)
    size = len(canonical(prepared[5]).encode("utf-8"))
    monkeypatch.setattr(research_planning, "MAX_PLANNING_TASK_BYTES", size - 1)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "RESEARCH_PLANNING_CONTEXT_LIMIT"
    assert len(env["model"].calls) == before
    monkeypatch.setattr(research_planning, "MAX_PLANNING_TASK_BYTES", size)
    install_discovery_doubles(env)
    await run_research(env, prepared)
    assert (
        next(call for call in env["model"].calls if call["role"] == "planner")["context"][
            "research_task"
        ]
        == prepared[5]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["root", "result"])
async def test_cached_tree_and_result_cannot_be_substituted_between_tasks(env, target):
    from sqlalchemy import select

    from careerforge.errors import DomainError
    from careerforge.models import ResearchNode, StageArtifact

    prepared = await prepared_run(env)
    install_discovery_doubles(env)
    await run_research(env, prepared)
    before = len(env["model"].calls)
    with env["app"].db.transaction() as db:
        saved = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == prepared[2].id, StageArtifact.stage_key == "research.result"
            )
        )
        if target == "result":
            saved.payload = {**saved.payload, "research_task_hash": "0" * 64}
        else:
            db.delete(saved)
            root = db.scalar(
                select(ResearchNode).where(
                    ResearchNode.run_id == prepared[2].id, ResearchNode.parent_id.is_(None)
                )
            )
            root.payload = {**root.payload, "research_task_hash": "0" * 64}
    with pytest.raises(DomainError) as error:
        await run_research(env, prepared)
    assert error.value.code == "RESEARCH_PLANNING_CONTRACT"
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["repository", "releases"])
@pytest.mark.parametrize("change", ["missing_dependency", "private_jd_removed", "revoked_jd"])
async def test_external_goal_derived_search_fails_before_http(env, channel, change):
    import httpx
    from sqlalchemy import func, select

    from careerforge.adapters.github import GitHubProvider
    from careerforge.errors import DomainError
    from careerforge.models import ExternalRequest, FundingAgreement, ObjectRow

    prepared = await prepared_run(env)
    document, goal_id, run, _, _, _, _ = prepared
    dependencies = [goal_id]
    with env["app"].db.transaction() as db:
        if change == "missing_dependency":
            dependencies = []
        elif change == "private_jd_removed":
            funding = db.get(FundingAgreement, env["funding"])
            funding.allowed_data_classes = [
                value for value in funding.allowed_data_classes if value != "private_jd"
            ]
        else:
            db.get(ObjectRow, document["jd_revision_id"]).state = "revoked"
    http_requests = []

    def respond(request):
        http_requests.append(request)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GitHubProvider(env["app"].db, env["app"].settings, client)
        with pytest.raises(DomainError):
            await provider.search(
                "repo:fixtures/storage", run.id, 10, channel, access_dependencies=dependencies
            )
    assert http_requests == []
    with env["app"].db.read() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ExternalRequest)
                .where(ExternalRequest.run_id == run.id)
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "private_jd_removed", "revoked_jd", "privacy"])
async def test_external_goal_search_never_caches_private_results_and_rechecks_after_http(
    env, change
):
    import httpx
    from sqlalchemy import func, select

    from careerforge.adapters.github import GitHubProvider
    from careerforge.errors import DomainError
    from careerforge.models import FundingAgreement, ObjectRow
    from careerforge.models_privacy_ext import PrivacyState
    from careerforge.models_retrieval_ext import GitHubHttpCache

    prepared = await prepared_run(env)
    document, goal_id, run, _, _, _, _ = prepared
    http_requests = []

    def respond(request):
        http_requests.append(request)
        if change != "none":
            with env["app"].db.transaction() as db:
                if change == "private_jd_removed":
                    funding = db.get(FundingAgreement, env["funding"])
                    funding.allowed_data_classes = [
                        value for value in funding.allowed_data_classes if value != "private_jd"
                    ]
                elif change == "revoked_jd":
                    db.get(ObjectRow, document["jd_revision_id"]).state = "revoked"
                else:
                    db.get(PrivacyState, env["actor"]).blocked = True
        return httpx.Response(
            200, json={"items": [], "incomplete_results": False}, headers={"etag": '"fixture"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GitHubProvider(env["app"].db, env["app"].settings, client)
        if change == "none":
            result = await provider.search(
                "bounded storage", run.id, 10, access_dependencies=[goal_id]
            )
            assert result["candidates"] == []
        else:
            with pytest.raises(DomainError) as error:
                await provider.search("bounded storage", run.id, 10, access_dependencies=[goal_id])
            assert error.value.code == "GITHUB_AUTHORITY"
    assert len(http_requests) == 1
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(GitHubHttpCache)) == 0


@pytest.mark.asyncio
async def test_direct_imported_revision_discovery_includes_its_newly_frozen_goal(env):
    from careerforge.models import AgentRun
    from careerforge.services.research_planning import RESEARCH_PLANNING_CONTRACT

    app, actor = env["app"], env["actor"]
    document, _ = await extracted_goal(env)
    # This build extracts its own goal rather than reusing the earlier version.
    body = build_input(env, None, jd_revision_id=document["jd_revision_id"])
    install_discovery_doubles(env)
    run_id = app.build.start(actor, body)
    await app.drain()
    task = app.runtime.stage(run_id, "research_context")["research_task"]
    assert task["goal_version_id"]
    for call in env["model"].calls:
        if call["role"] in {"planner", "reflector"} and "candidate_catalog" not in call["context"]:
            assert call["context"]["research_task"] == task
            assert task["goal_version_id"] in call["access_dependencies"]
    with app.db.read() as db:
        assert (
            db.get(AgentRun, run_id).input_manifest["research_planning_contract"]
            == RESEARCH_PLANNING_CONTRACT
        )


@pytest.mark.asyncio
async def test_new_public_discovery_contains_no_private_goal_or_private_egress(env):
    app, actor = env["app"], env["actor"]
    namespace = env["owner"]["public_namespace_id"]
    funding = app.runtime.create_funding(actor, namespace, 30)
    body = build_input(env, None).model_copy(
        update={"namespace_id": namespace, "funding_id": funding}
    )
    install_discovery_doubles(env)
    run_id = app.build.start(actor, body)
    await app.drain()
    task = app.runtime.stage(run_id, "research_context")["research_task"]
    assert task["goal_version_id"] is None and task["requirements"] == []
    for call in env["model"].calls:
        if call["role"] in {"planner", "reflector"}:
            assert call["context"]["research_task"] == task
            assert call["data_classes"] == ["public_source"]
