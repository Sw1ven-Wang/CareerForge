"""Desk integration admission tests; synthetic fixtures do not assert real answers."""

from datetime import timedelta

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_gap_schedule_v166 import gap_fixture, no_calls

from careerforge.desk_gaps import supplement_desk_question
from careerforge.models import AgentRun, Conversation, FundingAgreement, KnowledgeGap, Turn
from careerforge.util import now


@pytest.mark.asyncio
async def test_gap_reanswer_retrieves_new_topic_despite_old_module_tags(env, monkeypatch):
    from careerforge.contracts import TurnInput

    source, publication, build_id = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    cid = app.conversations.create(actor)
    captured = []
    retrieve = app.retrieval.retrieve

    async def observe(actor, spec, *args, **kwargs):
        result = await retrieve(actor, spec, *args, **kwargs)
        captured.append((spec, result))
        return result

    monkeypatch.setattr(app.retrieval, "retrieve", observe)
    # Synthetic service binding tests retrieval plumbing, not publication approval.
    outcome = app.conversations.submit_gap_reanswer(
        actor,
        cid,
        TurnInput(
            question="Tool Calling 返回值示例",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="synthetic-gap-reanswer",
        ),
        {
            "source_id": source,
            "publication_ids": [publication],
            "topic_keys": ["rag.rerank"],
            "build_run_id": build_id,
        },
    )
    await app.execute(outcome["run_id"])
    assert captured
    spec, result = captured[0]
    assert spec.pinned_publication_ids == [publication]
    assert spec.project_source_ids == [source]
    assert spec.topic_keys == []
    assert result["knowledge_units"]


def test_analysis_wire_schema_enforces_frozen_unit_budget():
    from careerforge.contracts_research_v163 import ResearchAnalysisOutput
    from careerforge.services.build import BuildService

    schema = BuildService._analysis_schema(
        [], ResearchAnalysisOutput, max_units=1, required_dimensions=[]
    )
    assert schema["properties"]["units"]["maxItems"] == 1
    assert schema["$defs"]["ResearchCoverageDraft"]["properties"]["dimensions"]["maxItems"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "我想学习 cognee 的 RRF 排名融合是怎么实现的。",
        "请分析 https://github.com/topoteretes/cognee 中 add 如何实现文档导入",
    ],
)
def test_implementation_question_is_not_a_goal_command(text):
    from careerforge.contracts_conversation_actions_v1 import DialogueMessage
    from careerforge.services.dialogue_routing import explicit_actions

    assert (
        explicit_actions(
            DialogueMessage(text=text, expected_focus_revision=1, idempotency_key="route")
        )
        is None
    )


def origin(env):
    gap, tid, source = gap_fixture(env, question="Explain the missing implementation exactly")
    app = env["app"]
    rid = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "chat", {}, 8, "original-desk-question"
    )
    with app.db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        funding.call_cap = 80
        funding.expires_at = now() + timedelta(hours=2)
        turn = db.get(Turn, tid)
        turn.run_id = rid
        conv = db.get(Conversation, turn.conversation_id)
        conv.revision = turn.focus_revision + 1
        cid = conv.id
    no_calls(env)
    return (
        gap,
        tid,
        source,
        {
            "actor": env["actor"],
            "funding_id": env["funding"],
            "conversation_id": cid,
            "status": "completed",
        },
    )


@pytest.mark.asyncio
async def test_desk_schedules_exact_gap_once_and_preserves_source(env, monkeypatch):
    gap, tid, source, job = origin(env)

    async def admission_only(*args, **kwargs):
        pass

    monkeypatch.setattr("careerforge.desk_workflow.execute_desk_job", admission_only)
    monkeypatch.setattr("careerforge.desk_gaps.process_auto_gap", lambda *a: None)
    await supplement_desk_question(env["app"], job)
    await supplement_desk_question(env["app"], job)
    with env["app"].db.read() as db:
        builds = list(db.scalars(select(AgentRun).where(AgentRun.workflow == "knowledge_build")))
        assert len(builds) == 1
        contract = builds[0].input_manifest["gap_schedule"]
        assert contract["origin"]["turn_id"] == tid
        assert contract["selected_source_id"] == source
        assert contract["request"]["auto_reanswer"] is True
        grant = db.get(FundingAgreement, job["funding_id"])
        assert grant.call_cap - grant.calls_consumed - grant.calls_held >= 24
        assert builds[0].input_manifest["question"] == "Explain the missing implementation exactly"
    assert "不代表整个项目没有" in job["reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["ambiguous", "stale", "foreign_grant", "reanswer"])
async def test_desk_does_not_expand_scope_or_loop(env, fence):
    gap, tid, source, job = origin(env)
    with env["app"].db.transaction() as db:
        if fence == "ambiguous":
            db.get(KnowledgeGap, gap).source_ids = [source, "other"]
        elif fence == "stale":
            db.get(Conversation, job["conversation_id"]).revision += 1
        elif fence == "reanswer":
            db.get(Turn, tid).focus = {"gap_reanswer": {"gap_id": gap}}
        else:
            job["funding_id"] = "another-grant"
    await supplement_desk_question(env["app"], job)
    with env["app"].db.read() as db:
        assert db.get(KnowledgeGap, gap).build_run_id is None


@pytest.mark.asyncio
async def test_gap_reanswer_keeps_prior_and_new_same_source_evidence(env, monkeypatch):
    from careerforge.contracts import TurnInput
    source, prior, _ = await publish_fixture(env)
    source2, fresh, build_id = await publish_fixture(env, key="fresh-gap")
    assert source2 == source
    app = env["app"]
    captured = []
    retrieve = app.retrieval.retrieve
    async def observe(actor, spec, *args, **kwargs):
        captured.append(spec)
        return await retrieve(actor, spec, *args, **kwargs)
    monkeypatch.setattr(app.retrieval, "retrieve", observe)
    cid = app.conversations.create(env["actor"])
    outcome = app.conversations.submit_gap_reanswer(env["actor"], cid,
        TurnInput(question="Tool Calling 返回值示例", expected_revision=1,
                  funding_id=env["funding"], idempotency_key="cumulative-gap"),
        {"source_id": source, "publication_ids": [fresh],
         "evidence_publication_ids": sorted({prior, fresh}),
         "topic_keys": [], "build_run_id": build_id})
    await app.execute(outcome["run_id"])
    assert captured[0].pinned_publication_ids == sorted({prior, fresh})
    assert captured[0].project_source_ids == [source]
