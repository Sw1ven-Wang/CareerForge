"""Actual build orchestration with explicit synthetic models and local sources.

These exercise contracts, persistence, metering and API delivery. They are not
real-model quality measurements and never invoke an external provider.
"""

from copy import deepcopy

import httpx
import pytest
from sqlalchemy import select
from support import FILES

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, ModelOutput
from careerforge.models import AgentRun, GoalVersion, ModelReceipt, ObjectRow, RunEvent
from careerforge.services.research_coverage import RESEARCH_CONTRACT_VERSION
from careerforge.util import canonical

QUESTION = "说明这个固定项目的模块职责、调用顺序，以及岗位要求对应的证据缺口。"
JD = "熟悉 Tool Calling 的调用边界和返回行为。"
STATIC_SCOPE = ["仅依据固定快照，未运行代码。"]


def import_fixture_source(env):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/ingestion-v163",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )


def build_input(env, source_id, key, **changes):
    return KnowledgeBuildInput.model_validate(
        {
            "namespace_id": env["ns"],
            "funding_id": env["funding"],
            "question": QUESTION,
            "source_id": source_id,
            "topic_keys": ["agent.tool_calling"],
            "call_cap": 14,
            "max_units_per_project": 1,
            "idempotency_key": key,
            **changes,
        }
    )


def run_state(env, run_id):
    with env["app"].db.read() as db:
        row = db.get(AgentRun, run_id)
        return row.status, row.calls_consumed, row.call_cap


def evidence_assessment(anchors, *, status="known"):
    grouped = {}
    for anchor in anchors:
        grouped.setdefault(anchor["subject_key"], []).append(anchor["claim_key"])
    return {
        "status": status,
        "explanation": "Explicit synthetic fixture assessment of this bounded question.",
        "anchors": [
            {"subject_key": subject, "claim_keys": keys} for subject, keys in grouped.items()
        ],
        "unresolved_questions": [],
    }


def install_synthetic_coverage_model(
    env, monkeypatch, *, two_units=False, unanswered=None, empty=False
):
    """Wrap existing FixtureModel. No fake global pass is installed in support.py."""
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        assert output.synthetic is True
        role, context = request["role"], request["context"]
        if role == "analysis" and request.get("schema"):
            if empty:
                return ModelOutput(
                    synthetic=True,
                    structured={
                        "units": [],
                        "unanswered_questions": ["没有足够证据形成知识单元。"],
                    },
                )
            value = deepcopy(output.structured)
            for unit in value["units"]:
                unit["claims"][0]["conditions"] = list(STATIC_SCOPE)
            if two_units:
                original_unit = value["units"][0]
                signature = deepcopy(original_unit)
                signature.update(subject_key="echo.signature", title="函数参数")
                signature["claims"][0].update(
                    key="argument", statement="echo 函数接收一个 value 参数。"
                )
                signature["claims"][0]["support"][0]["quote"] = "def echo(value):"
                result = deepcopy(original_unit)
                result.update(subject_key="echo.result", title="函数返回值")
                value["units"] = [signature, result]
            anchors = [
                {"subject_key": unit["subject_key"], "claim_key": claim["key"]}
                for unit in value["units"]
                for claim in unit["claims"]
            ]
            value["task_coverage"] = {"question": evidence_assessment(anchors)}
            value["unanswered_questions"] = list(unanswered or [])
            output.structured = value
        elif role == "reviewer" and request.get("schema"):
            value = deepcopy(output.structured)
            if two_units:
                statements = {
                    item["claim_id"]: item["statement"] for item in context["subject"]["claims"]
                }
                for decision in value["claim_decisions"]:
                    if "参数" in statements[decision["claim_id"]]:
                        decision["independent_reads"][0]["quote"] = "def echo(value):"
            # The individual unit can support facts without answering the whole
            # two-part question. Only the separately metered root audit composes.
            value["task_coverage"] = {
                "question": evidence_assessment(
                    context["coverage_anchors"], status="partial" if two_units else "known"
                )
            }
            output.structured = value
        elif (
            role == "coverage" and context.get("assessment_kind") == "independent_task_sufficiency"
        ):
            grouped = {}
            for anchor in context["coverage_anchors"]:
                grouped.setdefault(anchor["source_id"], []).append(anchor)
            output.structured = {
                "source_coverages": [
                    {"source_id": source_id, "coverage": {"question": evidence_assessment(anchors)}}
                    for source_id, anchors in grouped.items()
                ]
            }
        return output

    monkeypatch.setattr(env["model"], "complete", complete)


@pytest.mark.asyncio
async def test_new_goal_question_requirements_depth_and_private_dependencies_reach_both_roles(env):
    app, actor = env["app"], env["actor"]
    source_id = import_fixture_source(env)
    run_id = app.build.start(actor, build_input(env, source_id, "new-goal", jd_text=JD))
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and result["publication_ids"], result
    goal_id = result["goal_version_id"]
    task = result["research_task"]
    assert task["contract_version"] == RESEARCH_CONTRACT_VERSION
    assert task["original_question"] == QUESTION and task["goal_version_id"] == goal_id
    requirement = task["requirements"][0]
    assert requirement["requirement_id"] == requirement["key"] == "tool"
    assert requirement["quote"] == JD and requirement["depth"] == "熟悉"
    assert requirement["depth_basis"] == "explicit"
    assert requirement["required_dimensions"] == ["purpose", "implementation", "boundaries"]
    requests = [call for call in env["model"].calls if call["role"] in {"analysis", "reviewer"}]
    assert {call["role"] for call in requests} == {"analysis", "reviewer"}
    for request in requests:
        assert request["context"]["research_task"] == task
        assert "private_jd" in request["data_classes"]
        assert goal_id in request["access_dependencies"]
    independent = [call for call in requests if call["role"] == "reviewer" and not call["schema"]]
    assert independent and all(
        "analysis_evidence" not in call["context"]["review_subject"] for call in independent
    )
    assert result["coverage_visibility"] == "owner_private"
    assert not result["task_coverage"]["complete"]
    with app.db.read() as db:
        goal = db.get(GoalVersion, goal_id)
        assert (
            isinstance(goal.requirements, dict)
            and goal.requirements["requirements"][0]["quote"] == JD
        )
        registered = db.get(ObjectRow, goal_id)
        assert registered.kind == "goal_version" and registered.namespace_id == env["ns"]
        related = list(
            db.scalars(
                select(AgentRun.id).where((AgentRun.id == run_id) | (AgentRun.parent_id == run_id))
            )
        )
        events = list(db.scalars(select(RunEvent.payload).where(RunEvent.run_id.in_(related))))
        assert JD not in canonical(events)
        for identifier in [*result["unit_version_ids"], *result["review_ids"]]:
            assert "task_coverage" not in db.get(ObjectRow, identifier).payload


@pytest.mark.asyncio
async def test_reusing_owned_goal_keeps_identity_without_extracting_again(env):
    app, actor = env["app"], env["actor"]
    source_id = import_fixture_source(env)
    first = app.build.start(actor, build_input(env, source_id, "first-goal", jd_text=JD))
    await app.drain()
    first_result = app.runtime.stage(first, "result")
    goal_id = first_result["goal_version_id"]
    before = len(env["model"].calls)
    second = app.build.start(
        actor, build_input(env, source_id, "reuse-goal", goal_version_id=goal_id)
    )
    await app.drain()
    second_result = app.runtime.stage(second, "result")
    assert second_result and second_result["publication_ids"], second_result
    assert second_result["goal_version_id"] == goal_id
    assert (
        second_result["research_task"]["requirements"]
        == first_result["research_task"]["requirements"]
    )
    calls = env["model"].calls[before:]
    assert calls and not any(call["role"] == "extract" for call in calls)
    for request in calls:
        if request["context"].get("research_task"):
            assert request["context"]["research_task"]["goal_version_id"] == goal_id
            assert (
                goal_id in request["access_dependencies"]
                and "private_jd" in request["data_classes"]
            )
    with app.db.read() as db:
        assert len(list(db.scalars(select(GoalVersion.id)))) == 1
    status, consumed, cap = run_state(env, second)
    assert status == "partially_succeeded" and consumed == len(calls) <= cap


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing_coverage", "unanswered"])
async def test_accepted_facts_remain_partial_when_task_coverage_is_missing_or_unanswered(
    env, monkeypatch, reason
):
    if reason == "unanswered":
        install_synthetic_coverage_model(env, monkeypatch, unanswered=["错误传播条件尚未核实。"])
    source_id = import_fixture_source(env)
    run_id = env["app"].build.start(
        env["actor"],
        build_input(
            env,
            source_id,
            "partial-" + reason,
            question="echo 返回什么？",
            research_scope="focused",
        ),
    )
    await env["app"].drain()
    result = env["app"].runtime.stage(run_id, "result")
    assert result["publication_ids"] and result["fact_publication_status"] == "published"
    assert (
        result["task_coverage"]["status"] == "partial" and not result["task_coverage"]["complete"]
    )
    assert "task_coverage_incomplete" in result["gaps"]
    assert run_state(env, run_id)[0] == "partially_succeeded"
    if reason == "unanswered":
        assert result["unanswered_questions"] == ["错误传播条件尚未核实。"]
        assert result["task_coverage"]["unresolved_questions"] == ["错误传播条件尚未核实。"]
        assert "analysis_unanswered_questions" in result["gaps"]


@pytest.mark.asyncio
async def test_synthetic_focused_cross_unit_sufficiency_completes_only_after_published_composition(
    env, monkeypatch
):
    install_synthetic_coverage_model(env, monkeypatch, two_units=True)
    app, actor = env["app"], env["actor"]
    source_id = import_fixture_source(env)
    run_id = app.build.start(
        actor,
        build_input(
            env,
            source_id,
            "focused-composition",
            question="echo 接收哪个参数，返回什么？",
            research_scope="focused",
            max_units_per_project=2,
            call_cap=20,
        ),
    )
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and len(result["publication_ids"]) == 2, result
    assert result["task_coverage"]["status"] == "complete" and result["task_coverage"]["complete"]
    assert result["gaps"] == [] and result["synthetic"] is True
    status, consumed, cap = run_state(env, run_id)
    assert status == "succeeded" and consumed <= cap
    final_calls = [
        call
        for call in env["model"].calls
        if call["context"].get("assessment_kind") == "independent_task_sufficiency"
    ]
    assert len(final_calls) == 1
    final = final_calls[0]
    assert final["role"] == "coverage"
    assert set(result["publication_ids"]) <= set(final["access_dependencies"])
    assert {unit["publication_id"] for unit in final["context"]["verified_units"]} == set(
        result["publication_ids"]
    )
    assert len(final["context"]["coverage_anchors"]) == 2
    evidence = result["task_coverage"]["question"]["evidence"]
    assert len({row["unit_version_id"] for row in evidence}) == 2
    assert all(
        row["conditions"] == STATIC_SCOPE and row["kind"] == "source_fact" for row in evidence
    )
    review_child = app.runtime.stage(run_id, "review_child")["run_id"]
    individual = app.runtime.stage(review_child, "result")["task_coverages"]
    assert len(individual) == 2 and all(
        item["coverage"]["question"]["status"] == "partial" for item in individual
    )
    with app.db.read() as db:
        receipts = list(
            db.scalars(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run_id, ModelReceipt.stage_key == "task.sufficiency"
                )
            )
        )
        assert len(receipts) == 1 and receipts[0].status == "completed"
    calls_before_read = len(env["model"].calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        first = await client.get("/api/v1/runs/" + run_id)
        again = await client.get("/api/v1/runs/" + run_id)
    assert first.status_code == again.status_code == 200, first.text
    assert first.json()["result"]["task_coverage"]["question"]["evidence"] == evidence
    assert again.json() == first.json() and len(env["model"].calls) == calls_before_read


@pytest.mark.asyncio
async def test_empty_analysis_is_partial_without_fabricated_overview_or_review(env, monkeypatch):
    install_synthetic_coverage_model(env, monkeypatch, empty=True)
    app, actor = env["app"], env["actor"]
    source_id = import_fixture_source(env)
    run_id = app.build.start(actor, build_input(env, source_id, "empty-analysis"))
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and result["publication_ids"] == [], result
    assert result["fact_publication_status"] == "none" and not result["task_coverage"]["complete"]
    assert "analysis_no_units" in result["gaps"] and "analysis_failed" not in result["gaps"]
    assert run_state(env, run_id)[0] == "partially_succeeded"
    analysis_child = app.runtime.stage(run_id, "analysis_child")["run_id"]
    assert app.runtime.stage(analysis_child, "result")["unit_version_ids"] == []
    assert app.runtime.stage(run_id, "review_child") is None
    assert not any(call["role"] in {"reviewer", "coverage"} for call in env["model"].calls)
