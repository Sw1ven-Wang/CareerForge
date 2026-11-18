"""Synthetic mechanism probes; not native model quality or source publication evidence."""

from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import ModelOutput, TurnInput
from careerforge.contracts_answer_completion_v166 import (
    ANSWER_COMPLETION_CONTRACT,
    ANSWER_COMPLETION_STAGE,
    AnswerAspectCheck,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    KnowledgeGap,
    StageArtifact,
    StreamEvent,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.answer_completion import classify_answer_aspects
from careerforge.services.dependencies import invalidate
from careerforge.util import fingerprint


@pytest.mark.parametrize(
    ("text", "quote", "satisfied", "safe"),
    [
        (
            "已知规则会返回 auto_deny，但当前证据不足以判断缓存是否会绕过该检查。",
            "规则会返回 auto_deny",
            False,
            True,
        ),
        ("规则先于缓存执行，因此不能绕过。", "不能绕过", True, False),
        ("规则先于缓存执行。", "缓存总能绕过", False, False),
    ],
)
def test_aspect_coverage_requires_a_delivered_quote_and_a_resolved_conclusion(
    text, quote, satisfied, safe
):
    verdict = AnswerAspectCheck(
        status="complete",
        covered_fact_ids=["reviewed-fact"],
        reasons=["synthetic verdict"],
        answered_aspects=[
            {"requirement": "是否绕过", "answer_quote": quote, "fact_atom_id": "reviewed-fact"}
        ],
        unsupported_aspects=[],
        conclusion_status="resolved",
    )
    result = classify_answer_aspects(
        "缓存会绕过规则吗？", [{"fact_atom_id": "reviewed-fact", "text": text}], verdict
    )
    assert result["task_satisfied"] is satisfied
    assert result["safe_abstention"] is safe
    assert bool(result["unsupported_aspects"]) is not satisfied


@pytest.mark.asyncio
async def test_private_knowledge_request_returns_authorized_scope_response_without_model(env):
    turn = submit(env, "我没有权限访问另一个用户的私有知识单元，能给我其内容吗？")
    await env["app"].execute(turn["run_id"])
    packet = env["app"].answers.events(env["actor"], turn["turn_id"])
    block = next(
        event["payload"]["content"]
        for event in packet["events"]
        if event["type"] == "answer.block.validated"
    )
    assert block["gap_category"] == "authorization"
    assert block["publication_ids"] == block["fact_atom_ids"] == block["citations"] == []
    assert "当前授权范围" in block["text"]
    assert not env["model"].calls


def completion_model(env, status="complete", *, before_return=None):
    original = env["model"].complete
    observed = []

    async def complete(request):
        if request["context"].get("assessment_kind") != "rendered_answer_completeness":
            return await original(request)
        observed.append(deepcopy(request))
        env["model"].calls.append(deepcopy(request))
        if before_return:
            before_return(request)
        facts = request["context"]["accepted_blocks"]
        return ModelOutput(
            structured={
                "status": status,
                "covered_fact_ids": [f["fact_atom_id"] for f in facts],
                "missing_aspects": []
                if status == "complete"
                else ["The requested storage location is absent."],
                "reasons": ["Explicit synthetic completeness verdict for mechanism testing."],
                **(
                    {
                        "answered_aspects": [
                            {
                                "requirement": request["context"]["original_question"],
                                "answer_quote": f["text"][:1500],
                                "fact_atom_id": f["fact_atom_id"],
                            }
                            for f in facts
                        ]
                        if status == "complete"
                        else [],
                        "unsupported_aspects": []
                        if status == "complete"
                        else ["The requested storage location is absent."],
                        "conclusion_status": "resolved" if status == "complete" else status,
                    }
                    if request["context"].get("answer_aspect_contract")
                    else {}
                ),
            },
            synthetic=True,
        )

    env["model"].complete = complete
    return observed


def submit(env, question="解释 Tool Calling 返回值", *, funding=True, focus=None):
    app, actor = env["app"], env["actor"]
    conversation = app.conversations.create(actor)
    if focus is not None:
        with app.db.transaction() as db:
            db.get(Conversation, conversation).focus = focus
    return app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question=question,
            expected_revision=1,
            funding_id=env["funding"] if funding else None,
            idempotency_key="completion",
        ),
    )


def blocks(env, turn):
    return [
        event["payload"]["content"]
        for event in env["app"].answers.events(env["actor"], turn["turn_id"])["events"]
        if event["type"] == "answer.block.validated"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("high_risk", [True, False])
async def test_rewritten_answer_cannot_inherit_unrendered_draft_completeness(env, high_risk):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        original_pack = app.knowledge.publication_pack(db, actor, publication)
    observed = completion_model(env, "partial")

    def tutor(request):
        unit = request["context"]["evidence_pack"]["knowledge_units"][0]
        fact = unit["facts"][0]
        return ModelOutput(
            structured={
                "blocks": [
                    {
                        "kind": "repository_fact",
                        "text": "The result is stored in registry[source][name]."
                        + (" It never changes existing output." if high_risk else ""),
                        "fact_atom_ids": [fact["fact_atom_id"]],
                        "publication_ids": [unit["publication_id"]],
                        "conditions": fact["conditions"],
                        "transformation": "paraphrase",
                    }
                ],
                "complete": True,
            },
            synthetic=True,
        )

    env["model"].overrides["tutor"] = tutor
    question = "Explain which container stores the Tool Calling result."
    turn = submit(env, question)
    await app.execute(turn["run_id"], "completion-worker")
    result = app.runtime.stage(turn["run_id"], "result")
    assert result["task_satisfied"] is False
    assert result["uncovered_questions"] == [question]
    assert result["answer_coverage_status"] == "partial"
    actual = blocks(env, turn)
    assert len(actual) == 2
    assert actual[0]["text"] == original_pack["facts"][0]["text"]
    assert actual[0]["checked"] == "exact_fallback"
    assert actual[0]["blocked_reason"] == (
        "ANSWER_HIGH_RISK" if high_risk else "ANSWER_UNSUPPORTED"
    )
    assert actual[1]["kind"] == "evidence_gap" and actual[1]["gap_category"] == "evidence"
    assert len(observed) == 1
    assert observed[0]["context"]["original_question"] == question
    assert "registry[source][name]" not in str(observed[0]["context"])
    assert "citations" not in observed[0]["context"]
    with app.db.read() as db:
        gap = db.scalar(select(KnowledgeGap).where(KnowledgeGap.turn_id == turn["turn_id"]))
        assert gap is not None and "accepted_answer_incomplete" in gap.reason_codes
        assert app.knowledge.publication_pack(db, actor, publication) == original_pack
        assert db.get(Turn, turn["turn_id"]).status == "completed"
    finished = app.answers.events(actor, turn["turn_id"])["events"][-1]
    assert finished["type"] == "answer.completed"
    assert finished["payload"]["task_satisfied"] is False


@pytest.mark.asyncio
async def test_complete_answer_is_bound_to_exact_accepted_blocks_and_no_gap(env):
    await publish_fixture(env)
    observed = completion_model(env)
    turn = submit(env)
    await env["app"].execute(turn["run_id"], "completion-worker")
    assessment = env["app"].runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE)
    assert assessment["status"] == "complete" and assessment["task_satisfied"] is True
    assert assessment["synthetic"] is True
    assert len(observed) == 1 and len(blocks(env, turn)) == 1
    with env["app"].db.read() as db:
        row = db.scalar(select(AnswerBlock).where(AnswerBlock.turn_id == turn["turn_id"]))
        assert assessment["binding"]["blocks"] == [{"id": row.id, "content_hash": row.content_hash}]
        assert db.get(AgentRun, turn["run_id"]).calls_consumed <= 8
        assert (
            db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn["turn_id"]))
            is None
        )
    plan = env["app"].runtime.stage(turn["run_id"], "tutor.plan:atomic-answer-1.6.1")
    assert plan["calls_reserved_for_completion"] == 1
    assert plan["max_steps"] * 2 + 1 <= plan["remaining_calls_at_plan"]


@pytest.mark.asyncio
async def test_multi_block_completeness_observes_actual_combined_answer(env):
    await publish_fixture(env, "source-a", "fixtures/a")
    await publish_fixture(env, "source-b", "fixtures/b")
    observed = completion_model(env)
    turn = submit(env)
    await env["app"].execute(turn["run_id"], "completion-worker")
    assert len(observed) == 1
    assert len(observed[0]["context"]["accepted_blocks"]) == 2
    assert len(blocks(env, turn)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,category",
    [
        ("MODEL_BUDGET", "budget"),
        ("EGRESS_OR_ROLE_DENIED", "authorization"),
        ("MODEL_SCHEMA", "verification"),
    ],
)
async def test_unavailable_completion_is_not_a_knowledge_build_gap(
    env, code, category, monkeypatch
):
    await publish_fixture(env)
    completion_model(env)
    gateway = env["app"].gateway
    original = gateway.call

    async def unavailable(*args, **kwargs):
        if args[3] == ANSWER_COMPLETION_STAGE:
            raise DomainError(code, "Synthetic operational completion failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(gateway, "call", unavailable)
    turn = submit(env)
    await env["app"].execute(turn["run_id"], "completion-worker")
    assessment = env["app"].runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE)
    assert assessment["status"] == "unverified" and assessment["task_satisfied"] is None
    actual = blocks(env, turn)
    assert actual[-1]["gap_category"] == category
    with env["app"].db.read() as db:
        assert (
            db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn["turn_id"]))
            is None
        )


@pytest.mark.asyncio
async def test_replay_after_assessment_before_gap_commit_reuses_check(env, monkeypatch):
    await publish_fixture(env)
    observed = completion_model(env, "partial")
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    epoch = app.runtime.claim(turn["run_id"], "resume-worker")
    run = turn["run_id"], epoch, "resume-worker"
    original = app.answers.commit

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash after paid completion check")

    monkeypatch.setattr(app.answers, "commit", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"], run)
    assert len(observed) == 1
    assert app.runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE)
    monkeypatch.setattr(app.answers, "commit", original)
    await app.conversations.process(turn["turn_id"], run)
    assert len(observed) == 1 and len(blocks(env, turn)) == 2
    assert app.answers.events(actor, turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_revocation_during_completeness_does_not_finish_or_publish_gap(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)

    def revoke(_):
        with app.db.transaction() as db:
            invalidate(db, actor, publication, "revoked", "synthetic completion-race test")

    completion_model(env, before_return=revoke)
    turn = submit(env)
    await app.execute(turn["run_id"], "completion-worker")
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "interrupted"
    assert not any(event["type"] == "answer.completed" for event in packet["events"])
    assert app.runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE) is None


@pytest.mark.asyncio
async def test_new_ordinary_completion_marker_removal_is_not_a_historical_request(env):
    await publish_fixture(env)
    observed = completion_model(env)
    app = env["app"]
    turn = submit(env)
    # This is a new ordinary admission, not historical data. Removing a marker
    # and recomputing the run hash must not defeat its independent authority.
    # Real legacy compatibility is covered by a separate old-code subprocess.
    calls = len(env["model"].calls)
    with app.db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        row.focus = {k: v for k, v in row.focus.items() if k != "answer_completion_contract"}
        active = db.get(AgentRun, turn["run_id"])
        active.input_manifest = {
            k: v for k, v in active.input_manifest.items() if k != "answer_completion_contract"
        }
        active.input_hash = fingerprint(active.input_manifest)
    await app.execute(turn["run_id"], "completion-worker")
    assert observed == [] and len(env["model"].calls) == calls
    packet = app.answers.events(env["actor"], turn["turn_id"])
    assert packet["status"] == "blocked"
    assert [event["type"] for event in packet["events"]] == ["answer.interrupted"]
    assert packet["events"][0]["payload"] == {"code": "ORDINARY_CHECKER_BINDING", "redacted": True}
    assert app.runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE) is None
    assert app.runtime.stage(turn["run_id"], "tutor.plan:atomic-answer-1.6.1") is None
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status != "completed"
        assert (
            db.scalar(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn["turn_id"])) is None
        )
        assert (
            db.scalar(
                select(StreamEvent.id).where(
                    StreamEvent.turn_id == turn["turn_id"],
                    StreamEvent.event_type == "answer.completed",
                )
            )
            is None
        )


@pytest.mark.parametrize("marker", [None, "atomic-answer-1.6.1", "future-contract"])
def test_public_turn_schema_cannot_choose_completeness_marker(marker):
    with pytest.raises(ValidationError):
        TurnInput.model_validate(
            {
                "question": "Explain",
                "expected_revision": 1,
                "idempotency_key": "x",
                "answer_completion_contract": marker,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, "legacy", "future-contract"])
async def test_new_turn_overrides_untrusted_focus_marker_and_http_forbids_override(env, marker):
    app = env["app"]
    turn = submit(env, focus={"answer_completion_contract": marker})
    with app.db.read() as db:
        assert (
            db.get(Turn, turn["turn_id"]).focus["answer_completion_contract"]
            == ANSWER_COMPLETION_CONTRACT
        )
        assert (
            db.get(AgentRun, turn["run_id"]).input_manifest["answer_completion_contract"]
            == ANSWER_COMPLETION_CONTRACT
        )
    api = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        conv = (await client.post("/api/v1/conversations", json={})).json()
        response = await client.post(
            f"/api/v1/conversations/{conv['conversation_id']}/turns",
            json={
                "question": "Explain",
                "expected_revision": 1,
                "idempotency_key": "override",
                "answer_completion_contract": marker,
            },
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_turn_marker_cannot_be_downgraded_after_server_admission(env):
    await publish_fixture(env)
    observed = completion_model(env)
    turn = submit(env)
    with env["app"].db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        row.focus = {**row.focus, "answer_completion_contract": None}
    await env["app"].execute(turn["run_id"], "completion-worker")
    assert not observed and not blocks(env, turn)
    assert env["app"].answers.events(env["actor"], turn["turn_id"])["status"] == "blocked"
    with env["app"].db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status == "interrupted"


@pytest.mark.asyncio
async def test_completed_check_does_not_accept_changed_question_or_final_block(env, monkeypatch):
    await publish_fixture(env)
    completion_model(env)
    app = env["app"]
    turn = submit(env)
    epoch = app.runtime.claim(turn["run_id"], "bind-worker")
    run = turn["run_id"], epoch, "bind-worker"
    original = app.answers.commit

    def crash(*args, **kwargs):
        raise RuntimeError("stop before completion")

    monkeypatch.setattr(app.answers, "commit", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"], run)
    monkeypatch.setattr(app.answers, "commit", original)
    with app.db.transaction() as db:
        db.get(Turn, turn["turn_id"]).question = "A materially different question"
    calls = len(env["model"].calls)
    with pytest.raises(DomainError) as changed:
        await app.conversations.process(turn["turn_id"], run)
    assert changed.value.code == "ORDINARY_CHECKER_BINDING"
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == turn["run_id"],
                StageArtifact.stage_key == ANSWER_COMPLETION_STAGE,
            )
        )
        assert stage is not None


@pytest.mark.asyncio
async def test_narrow_question_needs_no_whole_repository_runtime_or_mastery_proof(env):
    await publish_fixture(env)
    observed = completion_model(env)
    turn = submit(env, "Explain the return value of this function.")
    await env["app"].execute(turn["run_id"], "narrow-worker")
    assessment = env["app"].runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE)
    assert assessment["task_satisfied"] is True
    assert len(observed[0]["context"]["accepted_blocks"]) == 1
    assert observed[0]["context"]["accepted_blocks"][0]["text"] == "该函数返回传入的 value。"
    system = observed[0]["messages"][0]["content"]
    assert "完整性只针对实际所问范围" in system and "未被要求的证明" in system
    assert assessment["uncovered_questions"] == []


@pytest.mark.asyncio
async def test_navigation_does_not_run_factual_completeness_checker(env):
    source, _, _ = await publish_fixture(env)
    observed = completion_model(env)
    turn = submit(env, "Locate src/tools.py", focus={"project_source_ids": [source]})
    await env["app"].execute(turn["run_id"], "navigation-worker")
    assert observed == []
    assert env["app"].runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE) is None


@pytest.mark.asyncio
async def test_resume_after_completion_gap_commit_does_not_duplicate_or_restart_tutor(
    env, monkeypatch
):
    await publish_fixture(env)
    observed = completion_model(env, "partial")
    app = env["app"]
    turn = submit(env)
    epoch = app.runtime.claim(turn["run_id"], "gap-resume-worker")
    run = turn["run_id"], epoch, "gap-resume-worker"
    original = app.answers.finish

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash after gap commit")

    monkeypatch.setattr(app.answers, "finish", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"], run)
    calls = len(env["model"].calls)
    assert len(blocks(env, turn)) == 2
    monkeypatch.setattr(app.answers, "finish", original)
    await app.conversations.process(turn["turn_id"], run)
    assert len(observed) == 1 and len(blocks(env, turn)) == 2
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_paid_completion_without_stage_replays_without_an_extra_charge(env, monkeypatch):
    await publish_fixture(env)
    observed = completion_model(env)
    app = env["app"]
    turn = submit(env)
    epoch = app.runtime.claim(turn["run_id"], "paid-resume-worker")
    run = turn["run_id"], epoch, "paid-resume-worker"
    original = app.runtime.save_stage_in

    def crash(db, active, key, payload):
        if key == ANSWER_COMPLETION_STAGE:
            raise RuntimeError("synthetic crash before final stage")
        return original(db, active, key, payload)

    monkeypatch.setattr(app.runtime, "save_stage_in", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"], run)
    with app.db.read() as db:
        before = db.get(AgentRun, turn["run_id"]).calls_consumed
    monkeypatch.setattr(app.runtime, "save_stage_in", original)
    await app.conversations.process(turn["turn_id"], run)
    with app.db.read() as db:
        assert db.get(AgentRun, turn["run_id"]).calls_consumed == before
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_terminal_delivery_atomically_records_question_outcome_and_schedulable_gap(
    env, monkeypatch
):
    await publish_fixture(env)
    observed = completion_model(env, "partial")
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    epoch = app.runtime.claim(turn["run_id"], "terminal-gap-worker")
    run = turn["run_id"], epoch, "terminal-gap-worker"
    original = app.answers.commit

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash immediately after terminal delivery")

    monkeypatch.setattr(app.answers, "commit", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"], run)
    assert app.answers.events(actor, turn["turn_id"])["status"] == "completed"
    assert app.runtime.stage(turn["run_id"], "result")["task_satisfied"] is False
    with app.db.read() as db:
        assert (
            len(
                list(
                    db.scalars(select(KnowledgeGap).where(KnowledgeGap.turn_id == turn["turn_id"]))
                )
            )
            == 1
        )
    calls = len(env["model"].calls)
    await app.conversations.process(turn["turn_id"], run)
    assert len(observed) == 1 and len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_privacy_generation_change_after_check_cannot_complete_old_answer(env, monkeypatch):
    await publish_fixture(env)
    completion_model(env, "partial")
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    original = app.answers.commit

    def changed_generation(*args, **kwargs):
        with app.db.transaction() as db:
            db.get(PrivacyState, actor).generation += 1
        original(*args, **kwargs)

    monkeypatch.setattr(app.answers, "commit", changed_generation)
    await app.execute(turn["run_id"], "privacy-worker")
    calls = len(env["model"].calls)
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "blocked"
    assert not any(event["type"] == "answer.completed" for event in packet["events"])
    assert all(event["payload"].get("redacted") is True for event in packet["events"])
    assert packet["events"][-1]["payload"]["code"] == "PRIVACY_GENERATION_CHANGED"
    with pytest.raises(DomainError):
        app.answers.committed(actor, turn["turn_id"])
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status == "interrupted"
        assert (
            db.scalar(
                select(StreamEvent.id).where(
                    StreamEvent.turn_id == turn["turn_id"],
                    StreamEvent.event_type == "answer.completed",
                )
            )
            is None
        )
        assert (
            db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn["turn_id"]))
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["partial", "unverified", "complete"])
async def test_existing_reconciliation_gate_rejects_new_partial_and_operational_answers(
    env, monkeypatch, outcome
):
    from careerforge.services import gap_scheduling

    source, publication, build_id = await publish_fixture(env)
    observed = completion_model(env, "complete" if outcome == "complete" else "partial")
    app, actor = env["app"], env["actor"]
    if outcome == "unverified":
        original = app.gateway.call

        async def unavailable(*args, **kwargs):
            if args[3] == ANSWER_COMPLETION_STAGE:
                raise DomainError("MODEL_BUDGET", "Synthetic verification limit")
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.gateway, "call", unavailable)
    question = "Explain the return value of this function."
    turn = submit(env, question, focus={"project_source_ids": [source]})
    await app.execute(turn["run_id"], "reconcile-boundary-worker")
    # Isolate the final-answer gate, not a simulated native source/build success.
    # The normal synthetic-publication rejection remains in the returned reasons.
    monkeypatch.setattr(gap_scheduling, "_verified_question_complete", lambda *args: True)
    with app.db.read() as db:
        build = db.get(AgentRun, build_id)
        pack = app.knowledge.publication_pack(db, actor, publication)
        reasons = gap_scheduling._answer_reasons(
            app.lifecycle,
            db,
            actor,
            {
                "origin": {
                    "turn_id": "synthetic-origin",
                    "question": question,
                    "source_ids": [source],
                },
                "selected_source_id": source,
            },
            build,
            {},
            {publication: pack},
            turn["turn_id"],
        )
        assert db.get(Turn, turn["turn_id"]).status == "completed"
    assert ("answer_still_has_gap" in reasons) is (outcome != "complete")
    assert "answer_fact_not_independently_checked" in reasons  # No synthetic gate bypass.
    assessment = app.runtime.stage(turn["run_id"], ANSWER_COMPLETION_STAGE)
    assert assessment["task_satisfied"] is (
        True if outcome == "complete" else False if outcome == "partial" else None
    )
    assert bool(observed) is (outcome != "unverified")
