"""Explicit synthetic user confirmation. Never claims a real user's approval."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select
from test_session_plans_v166 import planner, request
from test_session_tutor_binding_v166 import accepted

from careerforge.api import create_app
from careerforge.contracts import ModelOutput
from careerforge.contracts_session_progress_v166 import SessionModuleAckInput
from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput
from careerforge.errors import DomainError
from careerforge.models import AnswerBlock, ObjectRow
from careerforge.models_learning_ext import LearningActivity
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal
from careerforge.services.session_progress import MODULE_ACK_CONTRACT


async def delivered(env, *, paid=True, modules=1):
    app, actor = env["app"], env["actor"]
    if modules == 1:
        conv, value, plan = await accepted(env)
    else:
        _, first, _ = await publish_fixture(env, "first")
        _, second, _ = await publish_fixture(env, "second", "fixtures/second", "b" * 40)
        planner(env, [5, 5])
        from careerforge.contracts_session_plan_v166 import SessionPreferences

        proposed = app.learning.start_session_plan(
            actor,
            request(
                env,
                [first, second],
                preferences=SessionPreferences(practice=False, check_understanding=False),
            ),
        )
        await app.execute(proposed["run_id"])
        plan = app.runtime.stage(proposed["run_id"], "result")
        app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
        conv = app.conversations.create(actor)
        value = SessionTutorTurnInput(
            question="该函数返回什么 value？",
            expected_revision=1,
            idempotency_key="first-module",
            namespace_id=env["ns"],
            plan_id=plan["plan_id"],
            version_id=plan["version_id"],
            module_id=plan["module_ids"][0],
            expected_plan_revision=2,
            funding_id=env["funding"],
        )
    value = value.model_copy(update={"funding_id": env["funding"] if paid else None})
    turn = app.conversations.submit_session(actor, conv, value)
    if paid:
        await app.execute(turn["run_id"])
    else:
        await app.conversations.process(turn["turn_id"])
    binding = app.conversations.session_binding(actor, turn["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    return conv, value, plan, turn, detail


def ack_input(env, detail, *, key="module-ack"):
    module, delivery = detail["module"], detail["delivery"]
    return SessionModuleAckInput(
        learning_session_id=detail["learning_session_id"],
        turn_id=detail["turn_id"],
        namespace_id=env["ns"],
        plan_id=module["plan_id"],
        version_id=module["version_id"],
        module_id=module["module_id"],
        expected_plan_revision=module["plan_revision"],
        expected_profile_revision=env["app"].learning.profile(env["actor"])["revision"],
        binding_snapshot_hash=detail["session_tutor_binding"]["snapshot_hash"],
        completion_assessment_hash=delivery["completion_assessment_hash"],
        delivery_hash=delivery["delivery_hash"],
        confirm="ACKNOWLEDGE_MODULE_AND_ADVANCE",
        accept_incomplete_material=delivery["requires_incomplete_acceptance"],
        idempotency_key=key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", [True, False])
async def test_explicit_ack_finishes_reading_session_not_mastery_and_history_remains(env, paid):
    app, actor = env["app"], env["actor"]
    conv, tutor_input, _, turn, detail = await delivered(env, paid=paid)
    assert detail["activity_kind"] == "reading_and_tutoring"
    assert detail["module_ack"] is None and detail["delivery"]["delivered_fact_ids"]
    value = ack_input(env, detail)
    before = app.learning.profile(actor)
    result = app.conversations.acknowledge_module(actor, value)
    assert result["contract"] == MODULE_ACK_CONTRACT
    assert result["confirmation_provenance"] == "synthetic_user_confirmation"
    assert result["module_completed"] and result["competence_verified"] is False
    assert result["acceptance_passed"] is None
    assert result["next_active_module_id"] is None
    assert result["delivery"] == detail["delivery"]
    assert app.learning.profile(actor)["revision"] == before["revision"] + 1
    assert app.learning.profile(actor)["active_module_id"] is None
    replay = app.conversations.acknowledge_module(actor, value)
    assert replay["replayed"] and replay["ack_id"] == result["ack_id"]
    assert app.learning.profile(actor)["revision"] == before["revision"] + 1
    assert (
        app.conversations.teaching_session(actor, detail["learning_session_id"])["module_ack"][
            "ack_id"
        ]
        == result["ack_id"]
    )
    assert app.answers.committed(actor, turn["turn_id"])
    assert app.answers.events(actor, turn["turn_id"])["events"]
    assert (
        app.conversations.session_binding(actor, turn["turn_id"])["snapshot_hash"]
        == value.binding_snapshot_hash
    )
    assert app.conversations.submit_session(actor, conv, tutor_input)["turn_id"] == turn["turn_id"]
    if not paid:
        await app.conversations.process(turn["turn_id"])
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(
            actor, value.model_copy(update={"idempotency_key": "second-ack"})
        )
    with pytest.raises(DomainError):
        app.conversations.submit_session(
            actor,
            conv,
            tutor_input.model_copy(
                update={"expected_revision": 2, "idempotency_key": "new-old-module"}
            ),
        )
    with app.db.read() as db:
        assert list(db.scalars(select(LearningActivity))) == []


@pytest.mark.asyncio
async def test_two_modules_advance_in_order_only_after_explicit_ack(env):
    app, actor = env["app"], env["actor"]
    conv, value, plan, first, detail = await delivered(env, modules=2)
    first_ack = app.conversations.acknowledge_module(actor, ack_input(env, detail))
    assert first_ack["next_active_module_id"] == plan["module_ids"][1]
    second_input = value.model_copy(
        update={
            "module_id": plan["module_ids"][1],
            "expected_revision": 2,
            "idempotency_key": "second-module",
        }
    )
    second = app.conversations.submit_session(actor, conv, second_input)
    await app.execute(second["run_id"])
    binding = app.conversations.session_binding(actor, second["turn_id"])
    detail2 = app.conversations.teaching_session(actor, binding["learning_session_id"])
    second_ack = app.conversations.acknowledge_module(
        actor, ack_input(env, detail2, key="ack-second")
    )
    assert second_ack["plan_progress_status"] == "finished_by_user_ack"
    assert second_ack["next_active_module_id"] is None
    assert app.answers.committed(actor, first["turn_id"])


@pytest.mark.asyncio
async def test_hash_scope_revision_and_idempotency_mismatch_never_advance(env):
    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env)
    value, before = ack_input(env, detail), app.learning.profile(actor)
    for changes in [
        {"learning_session_id": "other"},
        {"turn_id": "other"},
        {"namespace_id": "other"},
        {"plan_id": "other"},
        {"version_id": "other"},
        {"module_id": "other"},
        {"expected_plan_revision": 1},
        {"expected_profile_revision": 999},
        {"binding_snapshot_hash": "0" * 64},
        {"completion_assessment_hash": "0" * 64},
        {"delivery_hash": "0" * 64},
    ]:
        with pytest.raises(DomainError):
            app.conversations.acknowledge_module(actor, value.model_copy(update=changes))
        assert app.learning.profile(actor) == before
    one = app.conversations.acknowledge_module(actor, value)
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(
            actor,
            value.model_copy(
                update={"accept_incomplete_material": not value.accept_incomplete_material}
            ),
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert app.learning.profile(actor)["revision"] == one["profile_revision_after"]


@pytest.mark.asyncio
async def test_unknown_requires_explicit_acceptance_and_preserves_unknown(env):
    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env, paid=False)
    assert detail["delivery"]["answer_coverage_status"] == "unverified"
    value = ack_input(env, detail)
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(
            actor, value.model_copy(update={"accept_incomplete_material": False})
        )
    assert error.value.code == "MODULE_ACK_INCOMPLETE"
    result = app.conversations.acknowledge_module(actor, value)
    assert result["delivery"]["answer_coverage_status"] == "unverified"
    assert result["delivery"]["task_satisfied"] is None
    assert result["acceptance_passed"] is None and not result["competence_verified"]


@pytest.mark.asyncio
async def test_zero_facts_cannot_be_acknowledged_even_with_incomplete_override(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    env["model"].overrides["tutor"] = lambda _: ModelOutput(
        structured={"blocks": [], "complete": True}, synthetic=True
    )
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    binding = app.conversations.session_binding(actor, started["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    assert detail["delivery"]["delivered_fact_ids"] == []
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(actor, ack_input(env, detail))
    assert error.value.code == "MODULE_ACK_NO_FACTS"


@pytest.mark.asyncio
async def test_pending_cancelled_wrong_owner_ack_rejected(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    pending = app.conversations.submit_session(actor, conv, value)
    binding = app.conversations.session_binding(actor, pending["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    assert detail["delivery"] is None
    fake = SessionModuleAckInput(
        learning_session_id=detail["learning_session_id"],
        turn_id=pending["turn_id"],
        namespace_id=env["ns"],
        plan_id=value.plan_id,
        version_id=value.version_id,
        module_id=value.module_id,
        expected_plan_revision=2,
        expected_profile_revision=app.learning.profile(actor)["revision"],
        binding_snapshot_hash=binding["snapshot_hash"],
        completion_assessment_hash="0" * 64,
        delivery_hash="0" * 64,
        confirm="ACKNOWLEDGE_MODULE_AND_ADVANCE",
        idempotency_key="pending",
    )
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(actor, fake)
    assert error.value.code == "SESSION_DELIVERY_PENDING"
    app.conversations.mark_failed(pending["turn_id"], "USER_CANCELLED")
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(actor, fake)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "not-the-learning-user")
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(other.id, fake)


@pytest.mark.asyncio
async def test_new_version_keeps_completed_history_but_rejects_new_old_ack_and_navigation_is_not_progress(
    env,
):
    app, actor = env["app"], env["actor"]
    _, _, plan, turn, detail = await delivered(env)
    value = ack_input(env, detail)
    revised = app.learning.revise_plan(actor, plan["plan_id"], 2, "new version", plan["module_ids"])
    app.learning.accept_plan(
        actor, plan["plan_id"], revised["version_id"], revised["expected_revision"]
    )
    assert (
        app.conversations.teaching_session(actor, detail["learning_session_id"])["delivery"]
        == detail["delivery"]
    )
    assert app.answers.events(actor, turn["turn_id"])["events"]
    with app.db.read() as db:
        block = db.scalar(
            select(AnswerBlock).where(
                AnswerBlock.turn_id == turn["turn_id"], AnswerBlock.kind == "repository_fact"
            )
        )
    before = app.learning.profile(actor)
    navigation = app.learning.navigation_status(actor, block.id)
    assert navigation["server_location_valid"]
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(actor, value)
    assert app.learning.profile(actor) == before


@pytest.mark.asyncio
async def test_successful_ack_replay_still_fences_evidence_revocation(env):
    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env)
    value = ack_input(env, detail)
    app.conversations.acknowledge_module(actor, value)
    with app.db.transaction() as db:
        invalidate(db, actor, value.module_id, "revoked", "revoked after ack")
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(actor, value)
    with pytest.raises(DomainError):
        app.conversations.teaching_session(actor, detail["learning_session_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False])
async def test_competing_ack_transactions_never_double_advance_sqlite_only(env, same_key):
    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env)
    value = ack_input(env, detail)
    before = app.learning.profile(actor)["revision"]

    def apply(index):
        try:
            return app.conversations.acknowledge_module(
                actor,
                value
                if same_key
                else value.model_copy(update={"idempotency_key": f"race-{index}"}),
            )
        except DomainError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [1, 2]))
    assert app.learning.profile(actor)["revision"] == before + 1
    if same_key:
        assert len({item["ack_id"] for item in results}) == 1
        assert sorted(item["replayed"] for item in results) == [False, True]
    else:
        assert sum("error" not in item for item in results) == 1
        assert (
            next(item["error"] for item in results if "error" in item)
            == "MODULE_ALREADY_ACKNOWLEDGED"
        )


def test_new_revisions_and_ack_fields_are_strict_without_changing_legacy():
    from careerforge.contracts import TurnInput

    base = dict(
        question="x",
        expected_revision=1,
        idempotency_key="x",
        namespace_id="n",
        plan_id="p",
        version_id="v",
        module_id="m",
        expected_plan_revision=1,
    )
    for key in ["expected_revision", "expected_plan_revision"]:
        for bad in [True, "1", 1.0]:
            with pytest.raises(ValidationError):
                SessionTutorTurnInput(**{**base, key: bad})
    assert (
        TurnInput(question="x", expected_revision="1", idempotency_key="x").expected_revision == 1
    )
    fields = SessionModuleAckInput.model_json_schema()["properties"]
    assert (
        not {"synthetic_user_confirmation", "human", "evaluator", "competence_verified"}
        & fields.keys()
    )


@pytest.mark.asyncio
async def test_ack_api_rejects_client_identity_forgery(env):
    app = env["app"]
    _, _, _, _, detail = await delivered(env)
    value = ack_input(env, detail).model_dump(mode="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer " + env["owner"]["token"]}
        path = "/api/v1/learning/session-module-acks"
        assert (
            await client.post(path, json={**value, "human": True}, headers=headers)
        ).status_code == 422
        assert (await client.post(path, json=value)).status_code == 401
        response = await client.post(path, json=value, headers=headers)
        assert response.status_code == 201, response.text
        assert response.json()["confirmation_provenance"] == "synthetic_user_confirmation"


@pytest.mark.asyncio
async def test_planned_check_needs_real_same_module_submission_not_tutor_complete(env):
    from careerforge.contracts_learning_ext import ActivityInput, AttemptInput, AttemptSubmission
    from careerforge.contracts_session_plan_v166 import SessionPreferences

    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(
        env, prefs=SessionPreferences(practice=False, check_understanding=True)
    )
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    binding = app.conversations.session_binding(actor, started["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    request_value = ack_input(env, detail)
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(actor, request_value)
    assert error.value.code == "MODULE_ATTEMPT_REQUIRED"
    activity = app.learning.create_activity(actor, ActivityInput(module_id=value.module_id))
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="real-attempt")
    )
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(
            actor, request_value.model_copy(update={"attempt_id": attempt["attempt_id"]})
        )
    app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(response="该函数返回传入的value。", external_help="used"),
    )
    ack = app.conversations.acknowledge_module(
        actor, request_value.model_copy(update={"attempt_id": attempt["attempt_id"]})
    )
    assert ack["attempt"]["state"] == "submitted"
    assert ack["attempt"]["assessment_state"] == "submitted_not_automatically_verified"
    assert ack["acceptance_passed"] is None and ack["competence_verified"] is False


def test_general_ack_never_bypasses_mandatory_acceptance():
    from careerforge.services.session_progress import require_ackable_task

    with pytest.raises(DomainError) as error:
        require_ackable_task({"kind": "reading", "acceptance_checks": [{"mandatory": True}]}, None)
    assert error.value.code == "MODULE_ACCEPTANCE_REQUIRED"
    with pytest.raises(DomainError):
        require_ackable_task(
            {"kind": "exercise", "acceptance_checks": [{"mandatory": True}]},
            {"kind": "exercise", "state": "submitted"},
        )


@pytest.mark.asyncio
async def test_ack_and_teaching_session_erasure_cannot_resurrect_receipt(env):
    from test_privacy_v16 import confirm, service

    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env)
    value = ack_input(env, detail)
    ack = app.conversations.acknowledge_module(actor, value)
    privacy = service(env)
    assert (await confirm(privacy, actor, privacy.propose(actor)))["state"] == "completed"
    with app.db.read() as db:
        for identifier in [detail["learning_session_id"], ack["ack_id"]]:
            obj = db.get(ObjectRow, identifier)
            assert obj.payload == {} and obj.state == "revoked"
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(actor, value)


@pytest.mark.asyncio
async def test_completed_ack_replay_survives_new_plan_version_without_advancing_again(env):
    app, actor = env["app"], env["actor"]
    _, _, plan, _, detail = await delivered(env)
    value = ack_input(env, detail)
    first = app.conversations.acknowledge_module(actor, value)
    revision = app.learning.revise_plan(
        actor, plan["plan_id"], 2, "new plan version", plan["module_ids"]
    )
    app.learning.accept_plan(
        actor, plan["plan_id"], revision["version_id"], revision["expected_revision"]
    )
    before = app.learning.profile(actor)
    replay = app.conversations.acknowledge_module(actor, value)
    assert replay["ack_id"] == first["ack_id"] and replay["replayed"]
    assert app.learning.profile(actor) == before


@pytest.mark.asyncio
async def test_persistent_preference_source_is_exact_and_revocation_applies_to_next_binding(env):
    from careerforge.contracts_learning_events_v166 import PreferenceEventInput
    from careerforge.contracts_session_plan_v166 import SessionPreferences

    app, actor = env["app"], env["actor"]
    event = app.learning.preference_event(
        actor,
        PreferenceEventInput(
            expected_revision=app.learning.profile(actor)["revision"],
            key="requested_style",
            value="architecture_first",
            lifetime="persistent",
            durable_consent=True,
            idempotency_key="persistent-style",
        ),
    )
    conv, value, _ = await accepted(
        env, prefs=SessionPreferences(practice=False, check_understanding=False)
    )
    first = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"funding_id": None})
    )
    binding = app.conversations.session_binding(actor, first["turn_id"])
    assert binding["preference_sources"]["requested_style"] == {
        "kind": "persistent_preference_event",
        "event_id": event["event_id"],
    }
    await app.conversations.process(first["turn_id"])
    app.learning.preference_event(
        actor,
        PreferenceEventInput(
            expected_revision=app.learning.profile(actor)["revision"],
            key="requested_style",
            operation="revoke",
            target_event_id=event["event_id"],
            lifetime="persistent",
            idempotency_key="revoke-style",
        ),
    )
    second = app.conversations.submit_session(
        actor,
        conv,
        value.model_copy(
            update={"expected_revision": 2, "funding_id": None, "idempotency_key": "second"}
        ),
    )
    binding = app.conversations.session_binding(actor, second["turn_id"])
    assert binding["effective_preferences"]["requested_style"] == "concept_first"
    assert binding["preference_sources"]["requested_style"]["kind"] == "profile_default"


@pytest.mark.parametrize(
    "module_name",
    [
        "session_progress",
        "learning",
        "learning_ext",
        "session_tutor",
        "contracts_session_plan_v166",
        "contracts_session_progress_v166",
    ],
)
def test_each_new_semantic_module_changes_replay_identity(monkeypatch, module_name):
    from careerforge.services import evolution_replay

    original = evolution_replay.inspect.getsource
    before = evolution_replay.engine_versions()

    def changed(module):
        result = original(module)
        return (
            result + "\n# changed semantic module\n"
            if getattr(module, "__name__", "").split(".")[-1] == module_name
            else result
        )

    monkeypatch.setattr(evolution_replay.inspect, "getsource", changed)
    assert (
        evolution_replay.engine_versions()["session_tutor_implementation"]
        != before["session_tutor_implementation"]
    )


@pytest.mark.asyncio
async def test_foreign_source_owner_erasure_scrubs_derived_session_and_ack_storage(env):
    from test_privacy_v16 import confirm, service

    from careerforge.contracts_session_plan_v166 import SessionPreferences
    from careerforge.services.security import set_grant

    app, learner = env["app"], env["actor"]
    with app.db.transaction() as db:
        owner, ns, _ = create_principal(db, "private-source-owner")
    foreign = {
        **env,
        "actor": owner.id,
        "ns": ns.id,
        "funding": app.runtime.create_funding(owner.id, ns.id, 100),
    }
    _, publication, _ = await publish_fixture(foreign, key="foreign-reading-source")
    with app.db.transaction() as db:
        set_grant(db, owner.id, ns.id, learner, "reader", True)
    planner(env, [10])
    proposed = app.learning.start_session_plan(
        learner,
        request(
            env,
            [publication],
            preferences=SessionPreferences(practice=False, check_understanding=False),
        ),
    )
    await app.execute(proposed["run_id"])
    plan = app.runtime.stage(proposed["run_id"], "result")
    app.learning.accept_plan(learner, plan["plan_id"], plan["version_id"], 1)
    conv = app.conversations.create(learner)
    value = SessionTutorTurnInput(
        question="Explain return value",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="foreign-reading",
        namespace_id=env["ns"],
        plan_id=plan["plan_id"],
        version_id=plan["version_id"],
        module_id=plan["module_ids"][0],
        expected_plan_revision=2,
    )
    started = app.conversations.submit_session(learner, conv, value)
    await app.execute(started["run_id"])
    binding = app.conversations.session_binding(learner, started["turn_id"])
    detail = app.conversations.teaching_session(learner, binding["learning_session_id"])
    ack_value = ack_input(env, detail)
    ack = app.conversations.acknowledge_module(learner, ack_value)
    ids = [binding["manifest_id"], detail["learning_session_id"], ack["ack_id"]]
    privacy = service(env)
    assert (await confirm(privacy, owner.id, privacy.propose(owner.id)))["state"] == "completed"
    with app.db.read() as db:
        for identifier in ids:
            obj = db.get(ObjectRow, identifier)
            assert obj.namespace_id == env["ns"]
            assert obj.payload == {} and obj.state == "revoked", (obj.kind, obj.payload)
        # The source owner's erasure does not destroy the learner's own question.
        from careerforge.models import Turn

        assert db.get(Turn, started["turn_id"]).question == value.question
    for read in [
        lambda: app.conversations.teaching_session(learner, detail["learning_session_id"]),
        lambda: app.conversations.acknowledge_module(learner, ack_value),
        lambda: app.conversations.session_binding(learner, started["turn_id"]),
    ]:
        with pytest.raises(DomainError):
            read()


@pytest.mark.asyncio
async def test_cli_http_get_review_and_explicit_ack_from_returned_hashes(env, tmp_path):
    import json

    import typer
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from careerforge.cli.learning_ext import register_cli

    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    shown, calls = [], []
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    cli = typer.Typer()
    with TestClient(create_app(service=app)) as client:

        def send(method, path, body=None):
            calls.append((method, path, body))
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code < 300, response.text
            return response.json()

        register_cli(cli, send, shown.append)
        path = tmp_path / "tutor.json"
        path.write_text(value.model_dump_json())
        runner = CliRunner()
        result = runner.invoke(cli, ["learning", "session-tutor", conv, str(path)])
        assert result.exit_code == 0, result.exception
        started = shown[-1]
        await app.execute(started["run_id"])
        assert (
            runner.invoke(cli, ["learning", "session-tutor-binding", started["turn_id"]]).exit_code
            == 0
        )
        binding = shown[-1]
        assert (
            runner.invoke(
                cli, ["learning", "teaching-session", binding["learning_session_id"]]
            ).exit_code
            == 0
        )
        detail = shown[-1]
        assert detail["can_acknowledge_module"] and detail["current_binding_active"]
        # Read endpoints and terminal delivery never consent on the user's behalf.
        assert detail["module_ack"] is None
        assert app.learning.profile(actor)["active_module_id"] == value.module_id
        ack_file = tmp_path / "synthetic_user_confirmation.json"
        ack_file.write_text(json.dumps(ack_input(env, detail).model_dump(mode="json")))
        result = runner.invoke(cli, ["learning", "module-ack", str(ack_file)])
        assert result.exit_code == 0, result.exception
        assert shown[-1]["confirmation_provenance"] == "synthetic_user_confirmation"
        assert shown[-1]["delivery"]["delivery_hash"] == detail["delivery"]["delivery_hash"]
        assert runner.invoke(cli, ["learning", "module-ack", str(ack_file)]).exit_code == 0
        assert shown[-1]["replayed"]
        assert (
            runner.invoke(
                cli, ["learning", "teaching-session", binding["learning_session_id"]]
            ).exit_code
            == 0
        )
        assert not shown[-1]["current_binding_active"] and not shown[-1]["can_acknowledge_module"]
        with app.db.transaction() as db:
            _, _, foreign_token = create_principal(db, "http-session-other")
        uri = f"/api/v1/learning/teaching-sessions/{binding['learning_session_id']}"
        assert client.get(uri).status_code == 401
        assert (
            client.get(uri, headers={"Authorization": "Bearer " + foreign_token}).status_code == 404
        )
        assert len(calls) == 6


@pytest.mark.asyncio
async def test_plan_replacement_and_module_ack_serialize_profile_revision(env):
    """Real SQLite race plus PostgreSQL SQL compilation, not a PG runtime test."""
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    from careerforge.models import LearningPlan
    from careerforge.models_learning_ext import LearningProfile

    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env)
    value, before = ack_input(env, detail), app.learning.profile(actor)["revision"]
    publication = detail["module"]["publication_id"]
    second = app.learning.start_session_plan(actor, request(env, [publication], key="replacement"))
    await app.execute(second["run_id"])
    plan = app.runtime.stage(second["run_id"], "result")
    locked_queries = []

    def observe(state):
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            entities = [x.get("entity") for x in getattr(statement, "column_descriptions", [])]
            if LearningProfile in entities or LearningPlan in entities:
                locked_queries.append(
                    (
                        entities,
                        str(statement.compile(dialect=postgresql.dialect())),
                        state.execution_options.get("populate_existing"),
                    )
                )

    event.listen(Session, "do_orm_execute", observe)
    try:

        def apply(which):
            if which == "accept":
                return app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
            try:
                return app.conversations.acknowledge_module(actor, value)
            except DomainError as exc:
                return {"error": exc.code}

        with ThreadPoolExecutor(max_workers=2) as pool:
            ack, acceptance = list(pool.map(apply, ["ack", "accept"]))
    finally:
        event.remove(Session, "do_orm_execute", observe)
    profile = app.learning.profile(actor)
    assert profile["active_plan_id"] == plan["plan_id"]
    assert profile["active_module_id"] == plan["module_ids"][0]
    assert profile["revision"] == before + 1 + int("ack_id" in ack)
    assert acceptance["active_version_id"] == plan["version_id"]
    if "error" in ack:
        assert ack["error"] in {"SESSION_TUTOR_INACTIVE", "REVISION_CONFLICT"}
    history = app.conversations.teaching_session(actor, detail["learning_session_id"])
    assert not history["current_binding_active"] and not history["can_acknowledge_module"]
    assert history["delivery"] == detail["delivery"]
    assert any(
        LearningProfile in entities and "FOR UPDATE" in sql and refresh is True
        for entities, sql, refresh in locked_queries
    )
    assert any(
        LearningPlan in entities and "FOR UPDATE" in sql and refresh is True
        for entities, sql, refresh in locked_queries
    )
    # A fresh ACK against the replaced old module cannot advance it.
    with pytest.raises(DomainError):
        app.conversations.acknowledge_module(
            actor, value.model_copy(update={"idempotency_key": "late-old-module"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["profile", "preference_event"])
async def test_all_profile_writers_lock_refresh_and_cannot_overwrite_ack_revision(env, writer):
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    from careerforge.contracts_learning_events_v166 import PreferenceEventInput
    from careerforge.contracts_learning_ext import LearningPreferences, ProfileUpdate
    from careerforge.models_learning_ext import LearningProfile

    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env, paid=False)
    profile = app.learning.profile(actor)
    value = ack_input(env, detail)
    statements = []

    def observed(state):
        statement = state.statement
        if (
            LearningProfile
            in [x.get("entity") for x in getattr(statement, "column_descriptions", [])]
            and getattr(statement, "_for_update_arg", None) is not None
        ):
            statements.append(
                (
                    str(statement.compile(dialect=postgresql.dialect())),
                    state.execution_options.get("populate_existing"),
                )
            )

    event.listen(Session, "do_orm_execute", observed)
    try:

        def apply(which):
            try:
                if which == "ack":
                    return app.conversations.acknowledge_module(actor, value)
                if writer == "profile":
                    return app.learning.update_profile(
                        actor,
                        ProfileUpdate(
                            expected_revision=profile["revision"],
                            preferences=LearningPreferences(
                                **{**profile["preferences"], "preferred_style": "code_first"}
                            ),
                        ),
                    )
                return app.learning.preference_event(
                    actor,
                    PreferenceEventInput(
                        expected_revision=profile["revision"],
                        idempotency_key="race-preference",
                        key="practice",
                        value=False,
                        lifetime="persistent",
                        durable_consent=True,
                    ),
                )
            except DomainError as exc:
                return {"error": exc.code}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, ["ack", "update"]))
    finally:
        event.remove(Session, "do_orm_execute", observed)
    assert sum("error" not in result for result in results) == 1
    assert next(result["error"] for result in results if "error" in result) == "REVISION_CONFLICT"
    assert app.learning.profile(actor)["revision"] == profile["revision"] + 1
    assert len([sql for sql, refresh in statements if "FOR UPDATE" in sql and refresh is True]) >= 2
    assert (
        app.conversations.teaching_session(actor, detail["learning_session_id"])["delivery"]
        == detail["delivery"]
    )


@pytest.mark.asyncio
async def test_wrong_but_owned_plan_rejected_before_acquiring_any_authority_lock(env):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from careerforge.models import LearningPlan
    from careerforge.models_learning_ext import LearningProfile

    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env, paid=False)
    other = app.learning.propose_plan(
        actor, [detail["module"]["publication_id"]], "function", "different owned plan"
    )
    value = ack_input(env, detail).model_copy(update={"plan_id": other["plan_id"]})
    locks = []

    def observed(state):
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            for descriptor in getattr(statement, "column_descriptions", []):
                if descriptor.get("entity") in {LearningPlan, LearningProfile}:
                    locks.append(descriptor["entity"])

    event.listen(Session, "do_orm_execute", observed)
    try:
        with pytest.raises(DomainError) as error:
            app.conversations.acknowledge_module(actor, value)
    finally:
        event.remove(Session, "do_orm_execute", observed)
    assert error.value.code == "MODULE_ACK_BINDING"
    assert locks == []
    assert (
        app.conversations.teaching_session(actor, detail["learning_session_id"])["module_ack"]
        is None
    )


@pytest.mark.asyncio
async def test_private_namespace_reinitialization_locks_and_increments_only_once(env):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from test_privacy_v16 import confirm, service

    from careerforge.models_learning_ext import LearningProfile

    app, actor = env["app"], env["actor"]
    await delivered(env, paid=False)
    privacy = service(env)
    await confirm(privacy, actor, privacy.propose(actor))
    with app.db.read() as db:
        old = db.scalar(select(LearningProfile).where(LearningProfile.principal_id == actor))
        old_revision, old_namespace = old.revision, old.namespace_id
    locks = []

    def observed(state):
        statement = state.statement
        if (
            LearningProfile
            in [x.get("entity") for x in getattr(statement, "column_descriptions", [])]
            and getattr(statement, "_for_update_arg", None) is not None
        ):
            locks.append(state.execution_options.get("populate_existing"))

    event.listen(Session, "do_orm_execute", observed)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: app.learning.profile(actor), [1, 2]))
    finally:
        event.remove(Session, "do_orm_execute", observed)
    assert all(value["revision"] == old_revision + 1 for value in results)
    with app.db.read() as db:
        current = db.scalar(select(LearningProfile).where(LearningProfile.principal_id == actor))
        assert current.namespace_id != old_namespace
        assert current.active_plan_id is None and current.active_module_id is None
    assert True in locks


@pytest.mark.asyncio
async def test_independent_work_is_visible_ack_blocker_without_destroying_history(env):
    app, actor = env["app"], env["actor"]
    _, _, _, _, detail = await delivered(env, paid=False)
    value = ack_input(env, detail)
    workspace = app.coding.create_workspace(
        actor,
        files={},
        publication_ids=[detail["module"]["publication_id"]],
        assistance_mode="independent",
    )
    history = app.conversations.teaching_session(actor, detail["learning_session_id"])
    assert history["delivery"] == detail["delivery"]
    assert history["ack_requirements"]["independent_workspace_active"]
    assert not history["can_acknowledge_module"]
    with pytest.raises(DomainError) as error:
        app.conversations.acknowledge_module(actor, value)
    assert error.value.code == "INDEPENDENT_ATTEMPT"
    app.coding.close_attempt(actor, workspace["workspace_id"])
    assert app.conversations.teaching_session(actor, detail["learning_session_id"])[
        "can_acknowledge_module"
    ]
    result = app.conversations.acknowledge_module(actor, value)
    assert result["confirmation_provenance"] == "synthetic_user_confirmation"
