"""Coordinator/Analysis collaboration: frozen goals, bounded followup, no extra agent."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from support import FILES
from test_research_coverage_v163 import coverage, evaluate

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.contracts_research_v163 import ResearchTaskContract
from careerforge.errors import DomainError
from careerforge.models import AgentRun
from careerforge.services.analysis_coordination import (
    BRIEF_STAGE,
    COORDINATOR_ANALYSIS_CONTRACT,
    NAVIGATION_CONTRACT,
    NAVIGATION_FIELD,
    CoordinatorAnalysisBrief,
    coverage_read_candidates,
    fence_call_flow,
    freeze_brief,
    open_objectives,
    require_bound_brief,
    reviewed_mechanism_path,
)


def _source(env, files=None):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/coordination",
            commit_sha="e" * 40,
            license_id="MIT",
            files=files or FILES,
            source_metadata={"synthetic": True},
        ),
    )


def _body(env, source, **extra):
    return KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=source,
        question="Explain tool calling and error boundary",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        auto_evidence_retry=True,
        coverage_guided_analysis=True,
        call_cap=18,
        planning_call_seconds=30,
        idempotency_key="coverage-build",
    ).model_copy(update=extra)


def test_explicit_cross_file_request_reserves_discovery_and_replays_original_request(env):
    app, actor = env["app"], env["actor"]
    body = _body(
        env,
        _source(env),
        question="Trace the complete cross-file tool calling chain",
        call_cap=24,
    )
    root = app.build.start(actor, body)
    assert app.build.start(actor, body) == root
    with app.db.read() as db:
        manifest = db.get(AgentRun, root).input_manifest
    assert manifest[NAVIGATION_FIELD] == NAVIGATION_CONTRACT
    assert manifest["mechanism_discovery_inferred"]
    assert manifest["discover_mechanisms"]
    assert manifest["mechanism_discovery_contract"]
    assert body.discover_mechanisms is False
    with pytest.raises(DomainError) as error:
        app.build.start(actor, body.model_copy(update={"discover_mechanisms": True}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_phase_path_uses_alternative_published_edges_and_rejects_disconnected_witnesses():
    endpoints = [
        {"path": f"code/{name}.py", "symbol": name}
        for name in ("agent", "validate", "dispatch", "result", "boundary")
    ]
    phases = ("entry", "validation", "dispatch", "result", "boundary")
    witnesses = [
        {"phase": phase, **endpoint, "source_id": "src", "commit": "a" * 40}
        for phase, endpoint in zip(phases, endpoints, strict=True)
    ]

    def edge(index, caller, callee):
        target = {"key": f"edge-{index}", "caller": caller, "callee": callee}
        return {
            "edge_id": target["key"],
            "edge_key": target["key"],
            "target": target,
            "source_id": "src",
            "commit": "a" * 40,
        }

    edges = [
        edge(i, caller, callee)
        for i, (caller, callee) in enumerate(zip(endpoints, endpoints[1:], strict=False))
    ]
    false_candidate = edge(9, endpoints[0], endpoints[-1])["target"]
    path = reviewed_mechanism_path(edges, witnesses)
    assert path["complete"] and len(path["path_edge_ids"]) == 4
    baseline = {
        "complete": True,
        "status": "complete",
        "publication_ids": ["pub"],
        "dimensions": [{"dimension": "call_flow", "evidence": ["claim"], "reason_codes": []}],
    }
    assert fence_call_flow(baseline, [false_candidate], edges, autonomous=True, path_evidence=path)[
        "complete"
    ]
    assert not fence_call_flow(
        baseline, [false_candidate], edges, autonomous=False, path_evidence=path
    )["complete"]
    for bad_edges, bad_witnesses in (
        (edges[:2], witnesses),
        ([edges[0], *edges[2:]], witnesses),
        (edges, witnesses[:-1]),
        (edges, [*witnesses[:-1], {**witnesses[-1], "commit": "b" * 40}]),
        (edges, [*witnesses[:-1], {**witnesses[-1], "source_id": "foreign"}]),
    ):
        incomplete = reviewed_mechanism_path(bad_edges, bad_witnesses)
        assert not incomplete["complete"]
        assert not fence_call_flow(
            baseline, [false_candidate], bad_edges, autonomous=True, path_evidence=incomplete
        )["complete"]


def test_frozen_brief_preserves_question_and_negative_requirement():
    task = ResearchTaskContract.model_validate(
        {
            "original_question": "Tool calling",
            "scope": "project",
            "required_dimensions": ["call_flow", "boundaries"],
            "requirements": [],
        }
    )
    source = SimpleNamespace(id="source-1", scope_hash="s" * 64, commit_sha="a" * 40)
    brief = freeze_brief(
        task.model_dump(mode="json"),
        source=source,
        question="Tool calling",
        topic_hints=["agent.tool_calling"],
    )
    assert brief["task_contract_hash"]
    assert brief["open_objective_keys"] == [
        "question",
        "dimension:call_flow",
        "dimension:boundaries",
    ]
    assert brief["completion_rule"] == "independently_reviewed_published_evidence_only"
    assert CoordinatorAnalysisBrief.model_validate(brief).round_index == 0
    bad = deepcopy(brief)
    bad["open_objective_keys"].append("dimension:made_up")
    with pytest.raises(ValueError):
        CoordinatorAnalysisBrief.model_validate(bad)
    with pytest.raises(DomainError) as exc:
        freeze_brief(
            task.model_dump(mode="json"), source=source, question="another", topic_hints=[]
        )
    assert exc.value.code == "ANALYSIS_BRIEF_SCOPE"


def test_review_draft_only_identifies_candidate_gaps_not_completion():
    task = ResearchTaskContract.model_validate(
        {"original_question": "Tool calling", "required_dimensions": ["call_flow", "boundaries"]}
    )
    draft = {
        "question": {
            "status": "known",
            "explanation": "candidate",
            "anchors": [{"subject_key": "x", "claim_keys": ["y"]}],
        },
        "dimensions": [
            {
                "dimension": "call_flow",
                "status": "known",
                "explanation": "candidate",
                "anchors": [{"subject_key": "x", "claim_keys": ["y"]}],
            },
            {"dimension": "boundaries", "status": "unknown", "explanation": "not read"},
        ],
    }
    frozen = task.model_dump(mode="json")
    result = evaluate(task, review=draft, anchors=[])
    assert open_objectives(frozen, result) == [
        "question",
        "dimension:call_flow",
        "dimension:boundaries",
    ]


def test_reviewer_known_but_depth_or_dimension_missing_still_requires_investigation():
    task = ResearchTaskContract.model_validate(
        {
            "original_question": "Explain tool calling",
            "requirements": [
                {
                    "key": "tool_call",
                    "technology": "Tool calling",
                    "keywords": ["tool"],
                    "depth": "掌握",
                    "depth_basis": "explicit",
                    "quote": "掌握工具调用",
                    "requirement_id": "tool_call",
                    "required_dimensions": ["call_flow"],
                }
            ],
        }
    )
    review = coverage(task)
    data = task.model_dump(mode="json")
    review["requirements"][0]["demonstrated_depth"] = "了解"
    assert open_objectives(data, evaluate(task, review=review)) == ["requirement:tool_call"]
    review["requirements"][0]["demonstrated_depth"] = "掌握"
    review["requirements"][0]["dimensions"] = []
    assert open_objectives(data, evaluate(task, review=review)) == ["requirement:tool_call"]
    review = coverage(task)
    assert open_objectives(data, evaluate(task, review=review)) == []


def test_bounded_static_followup_produces_no_authoritative_claims(env):
    files = {
        "src/loop.py": "from src.dispatch import dispatch\n\ndef run(value):\n    return dispatch(value)\n",
        "src/dispatch.py": "def dispatch(value):\n    try:\n        return value\n    except Exception:\n        return None\n",
        "README.md": "# synthetic fixture",
    }
    source_id = _source(env, files)
    with env["app"].db.read() as db:
        source = env["app"].sources.get(db, env["actor"], source_id)
        _, symbols = env["app"].sources.static_index(source)
        targets = coverage_read_candidates(
            source, symbols, "dispatch", ["dimension:call_flow", "dimension:boundaries"], []
        )
        assert len(targets) <= 4
        assert all(
            x["path"] in files and x["end_line"] <= len(files[x["path"]].splitlines())
            for x in targets
        )
        assert (
            coverage_read_candidates(source, symbols, "dispatch", ["question"], [], limit=0) == []
        )
        assert all("statement" not in x and "claim" not in x for x in targets)


def test_coverage_guided_requires_explicit_budgeted_scope(env):
    app = env["app"]
    source = _source(env)
    body = _body(env, source)
    for value in (
        body.model_copy(update={"auto_evidence_retry": False}),
        body.model_copy(update={"source_id": None}),
        body.model_copy(update={"max_units_per_project": 2}),
    ):
        with pytest.raises(DomainError) as exc:
            _prepare(app, env["actor"], value)
        assert exc.value.code == "ANALYSIS_COVERAGE_SCOPE"


def _prepare(app, actor, value):
    with app.db.transaction() as db:
        return app.build.prepare_in(db, actor, value)


@pytest.mark.asyncio
async def test_initial_brief_bound_to_parent_and_never_adds_agent_role(env):
    app, actor = env["app"], env["actor"]
    src = _source(env)
    body = _body(env, src)
    run = app.build.start(actor, body)
    assert app.build.start(actor, body) == run  # idempotent, no budget extension
    await app.execute(run)
    stage = app.runtime.stage(run, BRIEF_STAGE + "0")
    assert stage is not None
    assert stage["round_index"] == 0
    child_id = app.runtime.stage(run, "analysis_child")["run_id"]
    with app.db.read() as db:
        child = db.get(AgentRun, child_id)
        source = app.sources.get(db, actor, src)
        verified = require_bound_brief(db, child, source)
        assert verified == stage
        assert child.workflow == "analysis"
        assert (
            child.input_manifest["coordinator_analysis_contract"] == COORDINATOR_ANALYSIS_CONTRACT
        )
        assert len(list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run)))) == 1
    # An untrusted child-supplied goal digest cannot replace the root stage.
    with app.db.read() as db:
        child = db.get(AgentRun, child_id)
        source = app.sources.get(db, actor, src)
        injected = deepcopy(child)
        injected.input_manifest = {**child.input_manifest, "analysis_brief_hash": "bogus"}
        with pytest.raises(DomainError) as exc:
            require_bound_brief(db, injected, source)
        assert exc.value.code == "ANALYSIS_BRIEF_SCOPE"
        stripped = deepcopy(child)
        stripped.input_manifest = {
            key: value
            for key, value in child.input_manifest.items()
            if key != "coordinator_analysis_contract"
        }
        with pytest.raises(DomainError) as exc:
            require_bound_brief(db, stripped, source)
        assert exc.value.code == "ANALYSIS_BRIEF_SCOPE"


@pytest.mark.asyncio
async def test_bounded_coverage_build_discloses_unfinished_goal(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source = _source(env)
    original = env["model"].complete

    async def complete(request):
        answer = await original(request)
        if not request.get("schema"):
            return answer
        data = deepcopy(answer.structured)
        if request["role"] == "analysis":
            data["mechanism_edges"] = []
        if request["role"] == "reviewer":
            data["evidence_read_requests"] = []
        if request["role"] == "coverage" and "independent_reads" in request["context"]:
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [],
                "topic_links": [],
                "facets": [],
            }
        return answer.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", complete)
    run = app.build.start(actor, _body(env, source))
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result, "The root must persist its partial outcome"
    decision = result["coordinator_coverage_decision"]
    assert decision["contract"] == COORDINATOR_ANALYSIS_CONTRACT
    assert decision["complete"] is False
    assert decision["open_objectives"]
    assert "insufficient_reviewed_knowledge" in decision["reason_codes"]
    assert result["task_coverage"]["complete"] is False
    assert result["evidence_retry"]["status"] in {
        "no_new_read_target",
        "re_reviewed",
        "analysis_incomplete",
        "review_incomplete",
    }
    with app.db.read() as db:
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run)))
        assert all(child.workflow in {"analysis", "review"} for child in children)
        assert len(children) <= 4
        assert db.get(AgentRun, run).calls_consumed <= 18


@pytest.mark.asyncio
async def test_reviewed_coverage_gap_uses_second_analysis_not_a_new_agent(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source = _source(
        env,
        {
            "src/tool_loop.py": (
                "from src.tool_executor import execute_tool\n\n"
                + ("# Unrelated lines before the later dispatch definition.\n" * 260)
                + "def dispatch_tool(value):\n"
                "    try:\n"
                "        return execute_tool(value)\n"
                "    except ValueError:\n"
                "        return None\n"
            ),
            "src/tool_executor.py": "def execute_tool(value):\n    return value\n",
            "README.md": "# synthetic fixture",
        },
    )
    base = env["model"].complete

    async def completed(request):
        answer = await base(request)
        if not request.get("schema"):
            return answer
        data = deepcopy(answer.structured)
        if request["role"] == "analysis":
            data["mechanism_edges"] = []
        if request["role"] == "reviewer":
            data["evidence_read_requests"] = []
        if request["role"] == "coverage" and "independent_reads" in request["context"]:
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [],
                "topic_links": [],
                "facets": [],
            }
        return answer.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", completed)
    run = app.build.start(actor, _body(env, source))
    await app.drain()
    plan = app.runtime.stage(run, "evidence_retry_plan")
    assert plan, "Coverage must produce a durable decision after first independent review"
    assert plan["coverage_open_objectives"]
    assert plan["coverage_candidate_windows"]
    assert plan["retry_targets"]
    assert app.runtime.stage(run, "analysis_retry_child")
    assert plan["coverage_status"] == "unresolved_candidate_leads_only"
    assert all(
        window["path"] in {"src/tool_loop.py", "src/tool_executor.py"}
        for window in plan["coverage_candidate_windows"]
    )
    assert len(plan["retry_targets"]) <= 4
    if plan["retry_targets"]:
        saved_brief = app.runtime.stage(run, BRIEF_STAGE + "1")
        assert saved_brief and saved_brief["round_index"] == 1
        assert saved_brief["prior_review_ids"] == plan["prior_review_ids"]
        assert saved_brief["read_targets"] == plan["read_targets"]
    with app.db.read() as db:
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run)))
        assert len(children) <= 4
        assert all(child.workflow in {"analysis", "review"} for child in children)
        assert db.get(AgentRun, run).calls_consumed <= 18


def _coverage_fixture(env, monkeypatch, *, complete=True):
    base = env["model"].complete

    async def model(request):
        answer = await base(request)
        context = request["context"]
        if not request.get("schema"):
            return answer
        data = deepcopy(answer.structured)
        if request["role"] == "coverage" and "independent_reads" in context:
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [],
                "topic_links": [],
                "facets": [],
            }
            return answer.model_copy(update={"structured": data})
        if "research_task" not in context:
            return answer
        task = ResearchTaskContract.model_validate(context["research_task"])
        report = coverage(task)

        def bind(value):
            if isinstance(value, dict):
                if value.get("subject_key") == "delivery":
                    value.update(subject_key="echo", claim_keys=["return"])
                for item in value.values():
                    bind(item)
            elif isinstance(value, list):
                for item in value:
                    bind(item)

        bind(report)
        if not complete:
            report["question"] = {"status": "unknown", "explanation": "missing evidence"}
        if request["role"] == "analysis":
            data["task_coverage"] = report
            if "mechanism_edges" in request["schema"].get("properties", {}):
                data["mechanism_edges"] = []
        elif request["role"] == "reviewer":
            data["task_coverage"] = report
            data["evidence_read_requests"] = []
        elif context.get("assessment_kind") == "independent_task_sufficiency":
            data["source_coverages"] = [
                {"source_id": context["verified_units"][0]["source_id"], "coverage": report},
            ]
        return answer.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", model)


@pytest.mark.asyncio
async def test_first_published_scope_suffices_without_an_extra_round(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _coverage_fixture(env, monkeypatch)
    source = _source(env)
    run = app.build.start(actor, _body(env, source, research_scope="focused"))
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result["publication_ids"]
    assert result["task_coverage"]["complete"]
    decision = result["coordinator_coverage_decision"]
    assert decision["complete"] and decision["open_objectives"] == []
    assert decision["retry_status"] == "initial_reviewed_coverage_sufficient"
    assert app.runtime.stage(run, "analysis_retry_child") is None


def test_disjoint_published_edges_do_not_complete_the_requested_call_flow():
    task = ResearchTaskContract(
        original_question="Trace the connected flow", required_dimensions=["call_flow"]
    )
    complete = evaluate(task)
    assert complete["complete"]

    def endpoint(name):
        return {"path": name + ".py", "symbol": name}

    wanted = [
        {"key": "entry", "caller": endpoint("entry"), "callee": endpoint("dispatch")},
        {"key": "result", "caller": endpoint("result"), "callee": endpoint("write")},
    ]
    approved = [{"edge_key": row["key"], "target": row} for row in wanted]
    result = fence_call_flow(complete, wanted, approved, autonomous=True)
    assert not result["complete"]
    assert "dimension:call_flow" in open_objectives(task.model_dump(mode="json"), result)
    assert not fence_call_flow(complete, wanted, approved[:1], autonomous=False)["complete"]
    wanted[1]["caller"] = endpoint("dispatch")
    assert fence_call_flow(complete, wanted, approved, autonomous=True)["complete"]


@pytest.mark.asyncio
async def test_cancel_replay_exposes_blockers_and_never_reserves_again(env):
    from fastapi.testclient import TestClient

    from careerforge.api import create_app

    app, actor = env["app"], env["actor"]
    body = _body(env, _source(env))
    run = app.build.start(actor, body)
    await app.execute(run)
    with app.db.read() as db:
        revision = db.get(AgentRun, run).revision
    app.runtime.cancel(actor, run, revision)
    assert app.build.start(actor, body) == run
    await app.drain()
    assert not env["model"].calls
    assert app.runtime.stage(run, "analysis_retry_child") is None
    client = TestClient(create_app(app))
    response = client.get(
        f"/api/v1/runs/{run}", headers={"Authorization": "Bearer " + env["owner"]["token"]}
    )
    assert response.status_code == 200
    decision = response.json()["coordinator_coverage_decision"]
    assert not decision["complete"] and decision["retry_status"] == "interrupted"
    assert decision["open_objectives"]


def test_foreign_fixed_source_cannot_open_a_coverage_build(env):
    from careerforge.services.security import create_principal

    source = _source(env)
    app = env["app"]
    with app.db.transaction() as db:
        outsider, namespace, _ = create_principal(db, "outside-coverage-fixture")
        owner, namespace_id = outsider.id, namespace.id
    funding = app.runtime.create_funding(owner, namespace_id, 18)
    body = _body(env, source, namespace_id=namespace_id, funding_id=funding)
    with pytest.raises(DomainError) as error:
        app.build.start(owner, body)
    assert error.value.code == "NOT_FOUND"
    assert env["model"].calls == []


def test_no_opt_in_keeps_the_baseline_manifest_and_cannot_upgrade_same_key(env):
    app, actor = env["app"], env["actor"]
    body = _body(env, _source(env), coverage_guided_analysis=False)
    run_id = app.build.start(actor, body)
    with app.db.read() as db:
        manifest = db.get(AgentRun, run_id).input_manifest
        assert "coverage_guided_analysis" not in manifest
        assert "coordinator_analysis_contract" not in manifest
    assert app.build.start(actor, body) == run_id
    with pytest.raises(DomainError) as error:
        app.build.start(actor, body.model_copy(update={"coverage_guided_analysis": True}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_insufficient_budget_is_rejected_before_any_work(env):
    app, actor = env["app"], env["actor"]
    body = _body(env, _source(env), call_cap=8)
    with pytest.raises(DomainError) as error:
        app.build.start(actor, body)
    assert "BUDGET" in error.value.code
    assert not env["model"].calls


def test_satisfied_or_branch_is_not_an_open_objective():
    from test_jd_lineage_v166 import document, freeze_requirements, grouped_extraction

    from careerforge.services.research_coverage import freeze_research_task

    raw = document("熟悉 Quartz 或 NATS。")
    task = freeze_research_task(
        "Explain either queue",
        scope="focused",
        requirements=freeze_requirements(raw, grouped_extraction(raw)),
    )
    assessments = coverage(task)
    assessments["requirements"] = assessments["requirements"][:1]
    judged = evaluate(task, analysis=assessments, review=assessments)
    assert judged["complete"]
    assert open_objectives(task.model_dump(mode="json"), judged) == []


def test_negative_optional_and_unknown_depth_preserve_distinct_semantics():
    from test_jd_lineage_v166 import document, extraction, freeze_requirements

    from careerforge.contracts_jd_v166 import RequirementExtractionOutput
    from careerforge.services.analysis_coordination import goal_objectives
    from careerforge.services.research_coverage import freeze_research_task

    variants = [
        ("加分：熟悉 Redis。", {"technology": "Redis", "modality": "bonus"}),
        ("需要队列经验，具体熟练程度不限定。", {"depth": None, "depth_basis": "unspecified"}),
    ]
    for raw, changes in variants:
        doc = document(raw)
        payload = freeze_requirements(
            doc, RequirementExtractionOutput.model_validate(extraction(doc, **changes))
        )
        task = freeze_research_task(
            "Explain a bounded target", scope="focused", requirements=payload
        )
        verified = evaluate(task)
        objective = goal_objectives(task)[-1]
        frozen = freeze_brief(
            task.model_dump(mode="json"),
            source=SimpleNamespace(id="snapshot", scope_hash="s", commit_sha="c"),
            question=task.original_question,
            topic_hints=[],
            navigation_contract=NAVIGATION_CONTRACT,
        )
        assert frozen["goal_requirements"] == [
            row.model_dump(mode="json") for row in task.requirements
        ]
        assert frozen["active_required"] == frozen["open_objective_keys"] == ["question"]
        group = "optional_visible" if changes.get("modality") == "bonus" else "unresolved_scope"
        assert frozen[group] == [objective.key]
        if changes.get("modality") == "bonus":
            assert objective.priority == "optional" and verified["complete"]
        else:
            assert objective.target_depth is None and objective.depth_basis == "unspecified"
            assert not verified["complete"] and open_objectives(
                task.model_dump(mode="json"), verified
            )
        assert open_objectives(task.model_dump(mode="json"), verified, investigable=True) == []

    raw = document("不要求 Kubernetes。")
    draft = extraction(
        raw,
        technology="Kubernetes",
        modality="not_required",
        depth=None,
        depth_basis="unspecified",
        group_operator="NOT",
        group_key="negative",
    )
    draft["groups"] = [
        {
            **{
                k: draft["requirements"][0][k]
                for k in ("quote", "paragraph_id", "span_start", "span_end")
            },
            "key": "negative",
            "operator": "NOT",
            "member_keys": ["queue"],
        }
    ]
    task = freeze_research_task(
        "Explain a bounded target",
        scope="focused",
        requirements=freeze_requirements(raw, RequirementExtractionOutput.model_validate(draft)),
    )
    assert [item.key for item in goal_objectives(task)] == ["question"]
    assert task.requirements[0].modality == "not_required"


@pytest.mark.production
def test_pg_revocation_committed_while_root_waits_prevents_first_publication(tmp_path, monkeypatch):
    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    from sqlalchemy import func
    from sqlalchemy.engine import make_url
    from support import FixtureModel, FixtureRepositories

    from careerforge.application import Application
    from careerforge.config import Settings
    from careerforge.db import locked
    from careerforge.models import FundingAgreement, Publication, ReviewDecision
    from careerforge.services import runtime

    url = os.getenv("CAREERFORGE_REVIEW7_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires an isolated PostgreSQL validation database")
    assert "_validation_" in (make_url(url).database or "")
    model = FixtureModel()
    app = Application(
        Settings(
            mode="test",
            database_url=url,
            state_dir=tmp_path,
            allow_fixture_publication=True,
            embedding_dimensions=8,
        ),
        model,
        FixtureRepositories(),
    )
    try:
        initial = app.bootstrap("coordination-pg-" + uuid4().hex[:8])
        actor, ns = initial["principal_id"], initial["namespace_id"]
        funding = app.runtime.create_funding(actor, ns, 40)
        env = {"app": app, "actor": actor, "ns": ns, "funding": funding, "model": model}
        _coverage_fixture(env, monkeypatch)
        source = _source(env)
        root = app.build.start(actor, _body(env, source, research_scope="focused"))

        async def prepublication():
            await app.execute(root)
            await app.execute(app.runtime.stage(root, "analysis_child")["run_id"])
            await app.execute(root)
            await app.execute(app.runtime.stage(root, "review_child")["run_id"])

        asyncio.run(prepublication())
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(ReviewDecision)) == 1
            assert db.scalar(select(func.count()).select_from(Publication)) == 0
        reached = Event()
        baseline_calls = len(model.calls)

        def observe_lock(db, model_cls, identifier):
            if model_cls is FundingAgreement and identifier == funding:
                reached.set()
            return locked(db, model_cls, identifier)

        monkeypatch.setattr(runtime, "locked", observe_lock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.db.transaction() as db:
                agreement = locked(db, FundingAgreement, funding)
                agreement.status = "revoked"
                db.flush()
                worker = pool.submit(asyncio.run, app.execute(root, "coverage-pg-fence"))
                assert reached.wait(15) and not worker.done()
            worker.result(timeout=30)
        assert len(model.calls) == baseline_calls
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(Publication)) == 0
        assert app.runtime.stage(root, "analysis_retry_child") is None
    finally:
        app.db.engine.dispose()
