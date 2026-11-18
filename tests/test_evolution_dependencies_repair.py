"""Synthetic contract/race regressions; never real model quality or promotion evidence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import FeedbackInput, ModelOutput, PolicyPatch, QueryPlan, TurnInput
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    AnswerBlock,
    EvolutionCandidate,
    Experience,
    FundingAgreement,
    ModelReceipt,
    PolicyHead,
    PolicyVersion,
    Turn,
)
from careerforge.services.dependencies import check_closure, register
from careerforge.services.evolution import REFLECTION_CONTRACT
from careerforge.services.evolution_replay import BudgetedReplayGateway
from careerforge.services.policies import ensure_policies
from careerforge.util import fingerprint


def episode(env, publications=(), text="Please use shorter explanations."):
    """A fixture trace, then the real consent/feedback service; no receipt fabrication."""
    app, actor = env["app"], env["actor"]
    conversation = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="fixture source boundary", expected_revision=1, idempotency_key="feedback"
        ),
    )["turn_id"]
    with app.db.transaction() as db:
        db.get(Turn, turn).status = "completed"
        if publications:
            content = {"publication_ids": list(publications), "fixture_trace": True}
            record = register(db, actor, env["ns"], "answer_block", content)
            db.add(
                AnswerBlock(
                    id=record.id,
                    turn_id=turn,
                    ordinal=0,
                    kind="repository_fact",
                    content=content,
                    content_hash=fingerprint(content),
                )
            )
    result = app.evolution.feedback(
        actor,
        FeedbackInput(
            turn_id=turn,
            signal="explicit_feedback",
            content=text,
            consent=True,
        ),
    )
    return result, conversation


def start(env, feedback, funding=None, key="reflect"):
    return env["app"].evolution.start_candidate(
        env["actor"],
        env["ns"],
        funding or env["funding"],
        [feedback["experience_id"]],
        "teaching_skill",
        key,
    )


def receipts(env, run):
    with env["app"].db.read() as db:
        return db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run)).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["none", "public", "private", "mixed"])
async def test_reflection_declares_exact_source_categories_and_replays_once(env, scope):
    app, actor = env["app"], env["actor"]
    publications = []
    expected = {"learning_state"}
    if scope in {"private", "mixed"}:
        _, pub, _ = await publish_fixture(env, key="private")
        publications.append(pub)
        expected.add("private_source")
    if scope in {"public", "mixed"}:
        _, pub, _ = await publish_fixture(
            env,
            key="public",
            repository="fixtures/public",
            namespace=env["owner"]["public_namespace_id"],
        )
        publications.append(pub)
        expected.add("public_source")
    feedback, _ = episode(env, publications)
    funding = app.runtime.create_funding(
        actor, env["ns"], 2, roles=["reflection"], data_classes=sorted(expected)
    )
    run_id = start(env, feedback, funding)
    assert start(env, feedback, funding) == run_id
    epoch = app.runtime.claim(run_id, "reflect-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.input_manifest["reflection_contract"] == REFLECTION_CONTRACT
        assert set(run.input_manifest["reflection_input"]["data_classes"]) == expected
        closure = check_closure(db, actor, publications)
        assert set(closure.source_data_classes) == expected - {"learning_state"}
    before = len(env["model"].calls)
    await app.evolution.execute(run, epoch, "reflect-worker")
    first = app.runtime.stage(run_id, "result")
    await app.evolution.execute(run, epoch, "reflect-worker")
    assert len(env["model"].calls) == before + 1
    assert app.runtime.stage(run_id, "result") == first
    assert first["published"] is False
    request = env["model"].calls[-1]
    assert set(request["data_classes"]) == expected
    assert request["access_dependencies"] == sorted(publications)
    assert all(
        "policy_versions" not in e and "trace_manifest" not in e
        for e in request["context"]["episodes"]
    )
    saved = receipts(env, run_id)
    assert len(saved) == 1 and saved[0].status == "completed"
    assert saved[0].request_hash == fingerprint(request)
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 1
        assert db.get(EvolutionCandidate, first["candidate_id"]).status == "proposed"
        assert not db.get(
            PolicyVersion,
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id,
        ).candidate_id


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["private", "learning", "role", "source"])
async def test_reflection_missing_authorization_has_zero_egress_and_receipts(env, denial):
    app, actor = env["app"], env["actor"]
    source, pub, _ = await publish_fixture(env)
    other, _, _ = await publish_fixture(env, key="other", repository="fixtures/other")
    feedback, _ = episode(env, [pub])
    funding = app.runtime.create_funding(
        actor,
        env["ns"],
        2,
        roles=[] if denial == "role" else ["reflection"],
        data_classes=[
            c
            for c in ["learning_state", "private_source"]
            if c
            != {
                "private": "private_source",
                "learning": "learning_state",
            }.get(denial)
        ],
        allowed_source_ids=[other] if denial == "source" else [source],
    )
    run_id = start(env, feedback, funding)
    before = len(env["model"].calls)
    await app.execute(run_id)
    assert len(env["model"].calls) == before
    assert receipts(env, run_id) == []
    assert app.runtime.stage(run_id, "result") is None
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "failed"
        assert db.get(AgentRun, run_id).calls_consumed == 0
        assert db.get(FundingAgreement, funding).calls_consumed == 0


@pytest.mark.asyncio
async def test_frozen_episode_change_stops_before_model_even_with_live_consent(env):
    app = env["app"]
    feedback, _ = episode(env)
    run_id = start(env, feedback)
    with app.db.transaction() as db:
        item = db.get(Experience, feedback["experience_id"])
        item.payload = {**item.payload, "feedback": "changed after freezing"}
    await app.execute(run_id)
    assert not receipts(env, run_id)
    assert not env["model"].calls
    assert app.runtime.stage(run_id, "result") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["queued", "before_dispatch", "in_flight"])
async def test_withdrawal_fences_dispatch_and_drops_late_reflection_bodies(
    env, monkeypatch, timing
):
    app, actor = env["app"], env["actor"]
    feedback, conversation = episode(env, text="private feedback marker must be removed")
    run_id = start(env, feedback)
    if timing == "queued":
        app.evolution.revoke_feedback(actor, feedback["feedback_id"])
    elif timing == "before_dispatch":
        original = app.runtime.validate

        def revoke_after_last_runtime_check(*args):
            original(*args)
            app.evolution.revoke_feedback(actor, feedback["feedback_id"])

        monkeypatch.setattr(app.runtime, "validate", revoke_after_last_runtime_check)
    else:

        def revoke_in_flight(request):
            app.evolution.revoke_feedback(actor, feedback["feedback_id"])
            return ModelOutput(
                synthetic=True,
                structured={
                    "target": "teaching_skill",
                    "content": {"explanation_order": "code_first"},
                    "experience_ids": [feedback["experience_id"]],
                    "training_group_ids": ["conversation:" + conversation],
                },
            )

        env["model"].overrides["reflection"] = revoke_in_flight
    await app.execute(run_id)
    assert len(env["model"].calls) == int(timing == "in_flight")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "cancelled"
        assert "private feedback marker" not in str(run.input_manifest)
        assert not db.scalars(select(EvolutionCandidate)).all()
    assert all(r.status == "redacted" and r.response is None for r in receipts(env, run_id))
    assert app.runtime.stage(run_id, "result") is None


@pytest.mark.asyncio
async def test_gateway_does_not_trust_omitted_feedback_source_dependencies(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    feedback, _ = episode(env, [pub])
    run_id = start(env, feedback)
    epoch = app.runtime.claim(run_id, "worker")
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            run_id,
            epoch,
            "worker",
            "reflection",
            "reflection",
            "test",
            {},
            data_classes=["learning_state"],
            access_dependencies=[],
        )
    assert error.value.code == "MODEL_DEPENDENCIES_MISSING"
    assert len(env["model"].calls) == before and not receipts(env, run_id)


@pytest.mark.asyncio
async def test_legacy_reflection_keeps_original_request_identity_and_uncertain_stop(env):
    app = env["app"]
    feedback, _ = episode(env)
    run_id = start(env, feedback)
    # Represent an old persisted manifest, without silently upgrading its schema.
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        run.input_manifest = {
            k: v
            for k, v in run.input_manifest.items()
            if k not in {"reflection_contract", "reflection_input"}
        }
        run.input_hash = fingerprint(run.input_manifest)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    await app.evolution.execute(run, epoch, "worker")
    request = env["model"].calls[-1]
    assert request["data_classes"] == ["learning_state"]
    assert "feedback_witnesses" not in request
    assert "reflection_contract" not in request["context"]
    await app.evolution.execute(run, epoch, "worker")
    assert len(env["model"].calls) == 1
    with app.db.transaction() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
        receipt.status, receipt.response = "unknown", None
    with pytest.raises(DomainError) as error:
        await app.evolution.execute(run, epoch, "worker")
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert len(env["model"].calls) == 1


def test_candidate_idempotency_includes_training_lineage(env):
    app, actor = env["app"], env["actor"]
    feedback1, conv1 = episode(env)
    feedback2, conv2 = episode(env)
    with app.db.transaction() as db:
        base = ensure_policies(db, actor, env["ns"])["teaching_skill"]["policy_id"]

    def patch(feedback, conv):
        return PolicyPatch(
            target="teaching_skill",
            content={"explanation_order": "code_first"},
            experience_ids=[feedback["experience_id"]],
            training_group_ids=["conversation:" + conv],
        )

    first = app.evolution.create_candidate(actor, env["ns"], base, patch(feedback1, conv1))
    second = app.evolution.create_candidate(actor, env["ns"], base, patch(feedback2, conv2))
    assert first != second
    assert app.evolution.create_candidate(actor, env["ns"], base, patch(feedback2, conv2)) == second
    app.evolution.revoke_feedback(actor, feedback1["feedback_id"])
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, first).status == "consent_revoked"
        assert db.get(EvolutionCandidate, second).status == "proposed"


@pytest.mark.asyncio
async def test_legacy_router_rule_score_is_diagnostic_not_promotion_evidence(env):
    app, actor = env["app"], env["actor"]
    feedback, conversation = episode(env)
    with app.db.transaction() as db:
        base = ensure_policies(db, actor, env["ns"])["routing_policy"]["policy_id"]
    identifier = app.evolution.create_candidate(
        actor,
        env["ns"],
        base,
        PolicyPatch(
            target="routing_policy",
            content={
                "keyword_rules": [
                    {"keyword": "bottleneck", "task_type": "explain_project_architecture"}
                ]
            },
            experience_ids=[feedback["experience_id"]],
            training_group_ids=["conversation:" + conversation],
        ),
    )
    dataset = app.evolution.register_dataset(
        actor,
        env["ns"],
        "rule-only",
        "1",
        [
            {
                "case_id": str(i),
                "group_id": "holdout-" + str(i % 2),
                "input": {"question": "bottleneck example " + str(i)},
                "expected": {"task_type": "explain_project_architecture"},
            }
            for i in range(4)
        ],
        "fixture",
        [],
    )
    report = await app.evolution.evaluate(actor, identifier, dataset)
    assert report["decision"] == "inconclusive"
    assert "full_coordinator_replay" in report["reason"]
    assert not env["model"].calls
    with pytest.raises(DomainError):
        app.evolution.promote(actor, identifier, report["evaluation_id"], 1)


def test_competing_replay_stages_cannot_steal_other_arm_budget(env):
    app, actor = env["app"], env["actor"]
    run_id = app.runtime.create(actor, env["ns"], env["funding"], "research", {}, 4, "race")
    epoch = app.runtime.claim(run_id, "shared-worker")
    gateway = BudgetedReplayGateway(
        app.gateway, run_id, "replay:0:baseline:", 1, "research_heuristic"
    )
    barrier = Barrier(4)

    def output(request):
        sleep(0.03)
        return ModelOutput(synthetic=True, structured={"queries": [], "stop_reason": "fixture"})

    env["model"].overrides["planner"] = output

    def attempt(index):
        barrier.wait()

        async def call():
            try:
                await gateway.call(
                    run_id,
                    epoch,
                    "shared-worker",
                    f"replay:0:baseline:{index}",
                    "planner",
                    "fixture",
                    {},
                    QueryPlan,
                )
                return "completed"
            except DomainError as exc:
                return exc.code

        return asyncio.run(call())

    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(attempt, range(4)))
    assert result.count("completed") == 1, result
    assert result.count("REPLAY_ARM_BUDGET") == 3, result
    assert len(receipts(env, run_id)) == 1


@pytest.mark.asyncio
async def test_inherited_policy_sources_and_consent_are_not_laundered(env):
    from test_evolution import dataset

    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    first_feedback, _ = episode(env, [pub])
    first_run = start(env, first_feedback)
    await app.execute(first_run)
    first_candidate = app.runtime.stage(first_run, "result")["candidate_id"]
    report = await app.evolution.evaluate(actor, first_candidate, dataset(env))
    app.evolution.promote(actor, first_candidate, report["evaluation_id"], 1)
    second_feedback, conversation = episode(env)
    restricted = app.runtime.create_funding(
        actor, env["ns"], 2, roles=["reflection"], data_classes=["learning_state"]
    )
    second_run = start(env, second_feedback, restricted, "inherited")
    with app.db.read() as db:
        frozen = db.get(AgentRun, second_run).input_manifest
        assert frozen["reflection_input"]["access_dependencies"] == [pub]
        assert set(frozen["reflection_input"]["data_classes"]) == {
            "learning_state",
            "private_source",
        }
        base_id = frozen["base_policy_id"]
    before = len(env["model"].calls)
    await app.execute(second_run)
    assert len(env["model"].calls) == before
    assert not receipts(env, second_run)
    descendant = app.evolution.create_candidate(
        actor,
        env["ns"],
        base_id,
        PolicyPatch(
            target="teaching_skill",
            content={"explanation_order": "comparison_first"},
            experience_ids=[second_feedback["experience_id"]],
            training_group_ids=["conversation:" + conversation],
        ),
    )
    queued = start(env, second_feedback, key="inherited-queued")
    original_hash = receipts(env, first_run)[0].request_hash
    app.evolution.revoke_feedback(actor, first_feedback["feedback_id"])
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, first_candidate).status == "consent_revoked"
        assert db.get(EvolutionCandidate, descendant).status == "consent_revoked"
        assert db.get(AgentRun, queued).status == "cancelled"
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert db.get(PolicyVersion, head.policy_id).candidate_id is None
    assert receipts(env, first_run)[0].request_hash == original_hash
    assert receipts(env, first_run)[0].response is None
    assert receipts(env, first_run)[0].status == "redacted"


@pytest.mark.asyncio
async def test_competing_reflection_replay_does_not_claim_unfinished_receipt(env, monkeypatch):
    app = env["app"]
    feedback, _ = episode(env)
    run_id = start(env, feedback)
    epoch = app.runtime.claim(run_id, "same-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    started, release = asyncio.Event(), asyncio.Event()
    original = env["model"].complete

    async def blocked(request):
        result = await original(request)
        started.set()
        await release.wait()
        return result

    monkeypatch.setattr(env["model"], "complete", blocked)
    executing = asyncio.create_task(app.evolution.execute(run, epoch, "same-worker"))
    await started.wait()
    try:
        with pytest.raises(DomainError) as error:
            await app.evolution.execute(run, epoch, "same-worker")
        assert error.value.code == "MODEL_RESULT_UNCERTAIN"
        assert app.runtime.stage(run_id, "result") is None
        assert receipts(env, run_id)[0].status == "attempted"
    finally:
        release.set()
        await executing
    assert len(env["model"].calls) == 1
    assert len(receipts(env, run_id)) == 1
    assert app.runtime.stage(run_id, "result")["published"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before_dispatch", "in_flight"])
async def test_changed_data_class_grant_cannot_dispatch_or_keep_late_body(env, monkeypatch, timing):
    app, actor = env["app"], env["actor"]
    feedback, conversation = episode(env)
    funding = app.runtime.create_funding(
        actor, env["ns"], 2, roles=["reflection"], data_classes=["learning_state"]
    )
    run_id = start(env, feedback, funding)

    def revoke_classes():
        with app.db.transaction() as db:
            db.get(FundingAgreement, funding).allowed_data_classes = []

    if timing == "before_dispatch":
        original = app.runtime.validate

        def validate_then_revoke(*args):
            original(*args)
            revoke_classes()

        monkeypatch.setattr(app.runtime, "validate", validate_then_revoke)
    else:

        def response(request):
            revoke_classes()
            return ModelOutput(
                synthetic=True,
                structured={
                    "target": "teaching_skill",
                    "content": {"explanation_order": "code_first"},
                    "experience_ids": [feedback["experience_id"]],
                    "training_group_ids": ["conversation:" + conversation],
                },
            )

        env["model"].overrides["reflection"] = response
    await app.execute(run_id)
    assert len(env["model"].calls) == int(timing == "in_flight")
    assert all(r.response is None and r.status != "completed" for r in receipts(env, run_id))
    assert app.runtime.stage(run_id, "result") is None


@pytest.mark.parametrize("category", ["budget", "authorization"])
def test_operational_stops_do_not_become_false_retrieval_feedback(env, category):
    from careerforge.contracts_learning_ext import TraceCaptureInput

    app, actor = env["app"], env["actor"]
    feedback, _ = episode(env)
    with app.db.transaction() as db:
        turn_id = db.get(Experience, feedback["experience_id"]).payload["turn_id"]
        content = {"gap_category": category, "text": "operational boundary"}
        record = register(db, actor, env["ns"], "answer_block", content)
        db.add(
            AnswerBlock(
                id=record.id,
                turn_id=turn_id,
                ordinal=0,
                kind="evidence_gap",
                content=content,
                content_hash=fingerprint(content),
            )
        )
    result = app.evolution.capture_trace_feedback(
        actor,
        TraceCaptureInput(
            turn_id=turn_id,
            signals=["retrieval_gap"],
            consent=True,
        ),
    )
    assert result["collected"] == [] and result["not_observed"] == ["retrieval_gap"]


@pytest.mark.asyncio
async def test_legacy_private_reflection_is_not_silently_upgraded(env):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    feedback, _ = episode(env, [publication])
    run_id = start(env, feedback)
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        run.input_manifest = {
            key: value
            for key, value in run.input_manifest.items()
            if key not in {"reflection_contract", "reflection_input"}
        }
        run.input_hash = fingerprint(run.input_manifest)
    before = len(env["model"].calls)
    await app.execute(run_id)
    assert len(env["model"].calls) == before
    assert not receipts(env, run_id)
    assert app.runtime.stage(run_id, "result") is None


def test_source_anchored_gap_has_no_factual_or_layout_credit(env):
    case = {
        "frozen_units": [
            {
                "publication_id": "pub",
                "facts": [{"fact_atom_id": "fact", "conditions": ["boundary"]}],
            }
        ],
        "expected": {
            "required_fact_ids": ["fact"],
            "required_action_keys": [],
            "preferred_order": "code_first",
            "forbidden_text": [],
        },
    }
    gap = {
        "kind": "evidence_gap",
        "fact_atom_ids": ["fact"],
        "publication_ids": ["pub"],
        "conditions": ["boundary"],
        "checked": "source_anchored_gap",
        "style": "code_first",
        "gap_contract": "source-anchored-gap-1.6.1",
    }
    result = env["app"].evolution._score_replay_output(case, [gap], None)
    assert result["score"] == result["fact_recall"] == result["layout_match"] == 0
    assert result["hard_gate_violations"] == 0
    assert (
        env["app"].evolution._score_replay_output(
            case,
            [{**gap, "gap_contract": "invented"}],
            None,
        )["hard_gate_violations"]
        == 1
    )


async def promoted_reflection(env, publications=()):
    from test_evolution import dataset

    app, actor = env["app"], env["actor"]
    feedback, _ = episode(env, publications)
    reflection = start(env, feedback)
    await app.execute(reflection)
    candidate_id = app.runtime.stage(reflection, "result")["candidate_id"]
    report = await app.evolution.evaluate(actor, candidate_id, dataset(env))
    app.evolution.promote(actor, candidate_id, report["evaluation_id"], 1)
    with app.db.transaction() as db:
        refs = ensure_policies(db, actor, env["ns"])
    return feedback, candidate_id, refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["undeclared", "data_class", "source_scope", "legacy", "allowed"]
)
async def test_ordinary_policy_consumer_requires_actual_source_lineage(env, boundary):
    from careerforge.services.policy_inputs import POLICY_INPUT_CONTRACT

    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env, "policy-source")
    other, _, _ = await publish_fixture(env, "other-source", "fixtures/other-policy-source")
    _, _, refs = await promoted_reflection(env, [publication])
    funding = app.runtime.create_funding(
        actor,
        env["ns"],
        2,
        roles=["tutor"],
        data_classes=["learning_state"]
        if boundary == "data_class"
        else ["learning_state", "private_source"],
        allowed_source_ids=[other] if boundary == "source_scope" else [source, other],
    )
    manifest = {"policies": refs}
    if boundary != "legacy":
        manifest["policy_input_contract"] = POLICY_INPUT_CONTRACT
    run_id = app.runtime.create(actor, env["ns"], funding, "chat", manifest, 2, "policy-consumer")
    epoch = app.runtime.claim(run_id, "consumer")
    before = len(env["model"].calls)
    env["model"].overrides["tutor"] = lambda _: ModelOutput(text="synthetic", synthetic=True)

    async def call():
        classes, dependencies = ["learning_state", "private_source"], []
        if boundary != "undeclared":
            classes, dependencies = app.gateway.policy_egress_inputs(
                run_id, "tutor", ["learning_state"], []
            )
            assert classes == ["learning_state", "private_source"]
            assert dependencies == [publication]
        return await app.gateway.call(
            run_id,
            epoch,
            "consumer",
            "ordinary-policy-consumer",
            "tutor",
            "",
            {"teaching_skill": refs["teaching_skill"]["content"]},
            data_classes=classes,
            access_dependencies=dependencies,
        )

    if boundary == "allowed":
        assert (await call()).text == "synthetic"
        request = env["model"].calls[-1]
        assert request["access_dependencies"] == [publication]
        assert request["data_classes"] == ["learning_state", "private_source"]
        assert len(receipts(env, run_id)) == 1
    else:
        with pytest.raises(DomainError) as error:
            await call()
        assert (
            error.value.code
            == {
                "undeclared": "MODEL_DEPENDENCIES_MISSING",
                "data_class": "EGRESS_OR_ROLE_DENIED",
                "source_scope": "FUNDING_SOURCE_SCOPE",
                "legacy": "POLICY_EGRESS_VERSION",
            }[boundary]
        )
        assert len(env["model"].calls) == before
        assert receipts(env, run_id) == []


@pytest.mark.asyncio
async def test_withdrawal_outbox_recovers_interrupted_cleanup_without_policy_egress(
    env, monkeypatch
):
    from careerforge.models import Feedback, Outbox

    app, actor = env["app"], env["actor"]
    feedback, candidate_id, _ = await promoted_reflection(env)
    conversation = app.conversations.create(actor)
    queued = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="Explain Tool Calling",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="withdrawal-recovery",
        ),
    )
    original = app.evolution.finish_feedback_withdrawal

    def interrupted(*_):
        raise RuntimeError("synthetic crash after committed consent fence")

    monkeypatch.setattr(app.evolution, "finish_feedback_withdrawal", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        app.evolution.revoke_feedback(actor, feedback["feedback_id"])
    with app.db.read() as db:
        assert db.get(Feedback, feedback["feedback_id"]).consent_revision == 0
        pending = db.scalar(
            select(Outbox).where(
                Outbox.kind == "feedback.withdrawn",
                Outbox.aggregate_id == feedback["feedback_id"],
            )
        )
        assert pending is not None and not pending.delivered
        with pytest.raises(DomainError) as error:
            ensure_policies(db, actor, env["ns"])
        assert error.value.code == "EXPERIENCE_CONSENT"
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        app.gateway.policy_egress_inputs(queued["run_id"], "tutor", ["learning_state"], [])
    assert error.value.code == "EXPERIENCE_CONSENT"
    assert len(env["model"].calls) == before
    monkeypatch.setattr(app.evolution, "finish_feedback_withdrawal", original)
    app.process_projections()
    app.process_projections()
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, candidate_id).status == "consent_revoked"
        assert db.get(AgentRun, queued["run_id"]).status == "cancelled"
        assert db.get(Outbox, pending.id).delivered
        refs = ensure_policies(db, actor, env["ns"])
        assert db.get(PolicyVersion, refs["teaching_skill"]["policy_id"]).candidate_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["block_commit", "answer_finish"])
async def test_withdrawal_after_model_check_fences_answer_publication(env, monkeypatch, boundary):
    from careerforge.models import StreamEvent

    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env, "answer-commit-source")
    feedback, _, _ = await promoted_reflection(env, [publication])
    conversation = app.conversations.create(actor)
    queued = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="Explain echo Tool Calling",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="answer-commit-race",
        ),
    )
    original_cleanup = app.evolution.finish_feedback_withdrawal
    withdrew = False

    def withdraw_before_commit():
        nonlocal withdrew
        if withdrew:
            return
        withdrew = True

        def crash(*_):
            raise RuntimeError("synthetic cleanup crash")

        monkeypatch.setattr(app.evolution, "finish_feedback_withdrawal", crash)
        try:
            with pytest.raises(RuntimeError, match="synthetic cleanup crash"):
                app.evolution.revoke_feedback(actor, feedback["feedback_id"])
        finally:
            monkeypatch.setattr(app.evolution, "finish_feedback_withdrawal", original_cleanup)

    if boundary == "block_commit":
        original_guard = app.answers._guard_relevant_block

        async def guard(*args, **kwargs):
            block = await original_guard(*args, **kwargs)
            withdraw_before_commit()
            return block

        monkeypatch.setattr(app.answers, "_guard_relevant_block", guard)
    else:
        original_finish = app.answers.finish

        def finish(*args, **kwargs):
            withdraw_before_commit()
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(app.answers, "finish", finish)
    await app.execute(queued["run_id"], "answer-commit-worker")
    assert withdrew
    with app.db.read() as db:
        assert db.get(Turn, queued["turn_id"]).status != "completed"
        assert not db.scalars(
            select(StreamEvent).where(
                StreamEvent.turn_id == queued["turn_id"],
                StreamEvent.event_type == "answer.completed",
            )
        ).all()
        blocks = db.scalars(
            select(AnswerBlock).where(AnswerBlock.turn_id == queued["turn_id"])
        ).all()
        if boundary == "block_commit":
            assert not blocks
        else:
            # Blocks genuinely committed before withdrawal remain historical;
            # the later full-answer completion may not claim current consent.
            assert blocks
