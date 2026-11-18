"""Synthetic contract/security tests. No live-model or mastery claim."""

from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select
from test_session_plans_v166 import planner, request

from careerforge.api import create_app
from careerforge.contracts import TurnInput
from careerforge.contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from careerforge.contracts_learning_events_v166 import PreferenceEventInput
from careerforge.contracts_session_plan_v166 import SessionPreferences
from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, AnswerBlock, Conversation, ModelReceipt, ObjectRow, Turn
from careerforge.models_learning_ext import LearningActivity
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.dependencies import invalidate
from careerforge.services.evolution_replay import engine_versions
from careerforge.services.security import create_principal
from careerforge.services.session_tutor import SESSION_TUTOR_CONTRACT, SESSION_TUTOR_KEY
from careerforge.util import fingerprint


async def accepted(env, *, accept=True, key="session", prefs=None):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env, key="build-" + key)
    planner(env, [10])
    started = app.learning.start_session_plan(
        actor,
        request(
            env,
            [publication],
            key=key,
            preferences=prefs
            or SessionPreferences(
                practice=False, check_understanding=False, requested_style="code_first"
            ),
        ),
    )
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    if accept:
        app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    conv = app.conversations.create(actor)
    value = SessionTutorTurnInput(
        question="该函数返回什么 value？",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="bound",
        namespace_id=env["ns"],
        plan_id=result["plan_id"],
        version_id=result["version_id"],
        module_id=result["module_ids"][0],
        expected_plan_revision=2,
    )
    return conv, value, result


@pytest.mark.asyncio
async def test_explicit_session_tutor_delivers_real_module_context_and_no_activity_completion(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    before = app.learning.profile(actor)
    env["model"].calls.clear()
    started = app.conversations.submit_session(actor, conv, value)
    frozen = app.conversations.session_binding(actor, started["turn_id"])
    assert frozen["contract"] == SESSION_TUTOR_CONTRACT
    assert frozen["effective_preferences"]["practice"] is False
    assert frozen["preference_sources"]["practice"]["kind"] == "accepted_session"
    await app.execute(started["run_id"])
    with app.db.read() as db:
        run, turn = db.get(AgentRun, started["run_id"]), db.get(Turn, started["turn_id"])
        assert run.status == "succeeded", app.runtime.stage(run.id, "result")
        assert turn.status == "completed"
        assert run.input_manifest[SESSION_TUTOR_KEY] == turn.focus[SESSION_TUTOR_KEY]
        assert list(db.scalars(select(LearningActivity))) == []
    tutor = [c for c in env["model"].calls if c["role"] == "tutor"]
    assert tutor and not any(
        c["role"] in {"coordinator", "query_rewriter"} for c in env["model"].calls
    )
    ctx = tutor[0]["context"]["focus"]["session_module"]
    assert ctx["module_id"] == value.module_id
    assert ctx["version_id"] == value.version_id and ctx["plan_id"] == value.plan_id
    assert ctx["teaching_steps"] and ctx["steps"] == ["read_reviewed", "explain_conditions"]
    assert ctx["effective_preferences"]["requested_style"] == "code_first"
    assert ctx["effective_preferences"]["practice"] is False
    assert "learning_state" in tutor[0]["data_classes"]
    assert "private_source" in tutor[0]["data_classes"]
    assert ctx["binding_manifest_id"] in tutor[0]["access_dependencies"]
    assert not ctx["module_completed"] and not ctx["competence_verified"]
    assert not {"title", "goal", "session_request", "history"} & ctx.keys()
    assessment = app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE)
    assert assessment is not None
    assert assessment["binding"][SESSION_TUTOR_KEY]["snapshot_hash"] == frozen["snapshot_hash"]
    packet = app.answers.events(actor, started["turn_id"])
    (complete,) = [
        event["payload"] for event in packet["events"] if event["type"] == "answer.completed"
    ]
    assert packet["status"] == "completed"
    assert complete["tutor_answer_status"] == "completed"
    assert complete["tutor_answer_progress"]["used_fact_ids"]
    assert complete["tutor_answer_progress"]["missing_aspects"] == []
    assert app.learning.profile(actor) == before
    assert SESSION_TUTOR_KEY not in app.conversations.detail(actor, conv)["focus"]


@pytest.mark.asyncio
async def test_wrong_scope_version_module_draft_and_revision_rejected_without_calls(env):
    app, actor = env["app"], env["actor"]
    conv, value, result = await accepted(env, accept=False)
    count = len(env["model"].calls)
    with pytest.raises(DomainError, match="当前已采纳"):
        app.conversations.submit_session(actor, conv, value)
    app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    changes = [
        {"namespace_id": "other"},
        {"plan_id": "other"},
        {"version_id": "other"},
        {"module_id": "other"},
        {"expected_plan_revision": 1},
        {"expected_revision": 2},
    ]
    for change in changes:
        with pytest.raises(DomainError):
            app.conversations.submit_session(actor, conv, value.model_copy(update=change))
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "other-session-owner")
    other_conv = app.conversations.create(other.id)
    with pytest.raises(DomainError):
        app.conversations.submit_session(other.id, conv, value)
    with pytest.raises(DomainError):
        app.conversations.submit_session(other.id, other_conv, value)
    assert len(env["model"].calls) == count


@pytest.mark.asyncio
async def test_full_request_idempotency_and_legacy_endpoint_cannot_cross_replay(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    one = app.conversations.submit_session(actor, conv, value)
    assert app.conversations.submit_session(actor, conv, value)["turn_id"] == one["turn_id"]
    for change in [
        {"question": "different"},
        {"expected_revision": 2},
        {"funding_id": None},
        {"namespace_id": "other"},
        {"plan_id": "other"},
        {"version_id": "other"},
        {"module_id": "other"},
        {"expected_plan_revision": 3},
        {"model_call_cap": 9},
        {"preferences": SessionPreferences(practice=True)},
    ]:
        with pytest.raises(DomainError) as error:
            app.conversations.submit_session(actor, conv, value.model_copy(update=change))
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    old = TurnInput(**{k: getattr(value, k) for k in TurnInput.model_fields})
    with pytest.raises(DomainError) as error:
        app.conversations.submit(actor, conv, old)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    second_conv = app.conversations.create(actor)
    app.conversations.submit(actor, second_conv, old)
    with pytest.raises(DomainError) as error:
        app.conversations.submit_session(actor, second_conv, value)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "plan", "module", "privacy", "revoked"])
async def test_replaced_plan_privacy_or_revoked_evidence_fences_queued_dispatch_and_replay(
    env, change
):
    app, actor = env["app"], env["actor"]
    conv, value, result = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    if change == "version":
        revised = app.learning.revise_plan(actor, value.plan_id, 2, "换版本", result["module_ids"])
        app.learning.accept_plan(
            actor, value.plan_id, revised["version_id"], revised["expected_revision"]
        )
    elif change == "plan":
        with app.db.read() as db:
            publication = db.get(ObjectRow, value.module_id).payload["publication_id"]
        replacement = app.learning.start_session_plan(
            actor, request(env, [publication], key="replacement")
        )
        await app.execute(replacement["run_id"])
        replacement = app.runtime.stage(replacement["run_id"], "result")
        app.learning.accept_plan(actor, replacement["plan_id"], replacement["version_id"], 1)
    elif change == "module":
        from careerforge.models_learning_ext import LearningProfile

        with app.db.transaction() as db:
            profile = db.scalar(
                select(LearningProfile).where(LearningProfile.principal_id == actor)
            )
            profile.active_module_id = None
    elif change == "privacy":
        with app.db.transaction() as db:
            state = db.get(PrivacyState, actor)
            state.generation += 1
    else:
        with app.db.transaction() as db:
            invalidate(db, actor, value.module_id, "revoked", "test")
    count = len(env["model"].calls)
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    assert len(env["model"].calls) == count
    with app.db.read() as db:
        assert db.get(Turn, started["turn_id"]).status != "completed"
        assert (
            list(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == started["turn_id"])))
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("at_role", ["tutor", "answer_checker"])
async def test_midflight_plan_replacement_redacts_paid_body_and_never_commits(env, at_role):
    app, actor = env["app"], env["actor"]
    conv, value, result = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    provider = env["model"]
    original = provider.complete

    async def replace_after_response(model_request):
        output = await original(model_request)
        if model_request["role"] == at_role:
            revised = app.learning.revise_plan(
                actor, value.plan_id, 2, "replace", result["module_ids"]
            )
            app.learning.accept_plan(
                actor, value.plan_id, revised["version_id"], revised["expected_revision"]
            )
        return output

    provider.complete = replace_after_response
    await app.execute(started["run_id"])
    with app.db.read() as db:
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == started["run_id"], ModelReceipt.role == at_role
            )
        )
        assert receipt.status == "redacted" and receipt.response is None
        assert db.get(Turn, started["turn_id"]).status != "completed"
        assert (
            list(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == started["turn_id"])))
            == []
        )


@pytest.mark.asyncio
async def test_cancellation_and_funding_revocation_block_dispatch(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    with app.db.read() as db:
        revision = db.get(AgentRun, started["run_id"]).revision
    app.runtime.cancel(actor, started["run_id"], revision)
    app.conversations.mark_failed(started["turn_id"], "USER_CANCELLED")
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)
    conv2 = app.conversations.create(actor)
    started2 = app.conversations.submit_session(actor, conv2, value)
    before = len(env["model"].calls)
    app.runtime.revoke_funding(actor, env["funding"])
    await app.execute(started2["run_id"])
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_preferences_defaults_plan_event_request_order_and_turn_event_consumption(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    with app.db.transaction() as db:
        row = db.get(Conversation, conv)
        row.preferences = {**row.preferences, "practice": True, "requested_style": "concept_first"}
    # Mere stored defaults cannot override the accepted plan's explicit choices.
    one = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"funding_id": None})
    )
    assert (
        app.conversations.session_binding(actor, one["turn_id"])["effective_preferences"][
            "practice"
        ]
        is False
    )
    await app.conversations.process(one["turn_id"])
    event = app.learning.preference_event(
        actor,
        PreferenceEventInput(
            expected_revision=app.learning.profile(actor)["revision"],
            idempotency_key="one-turn-style",
            key="requested_style",
            value="architecture_first",
            lifetime="turn",
            conversation_id=conv,
        ),
    )
    before = app.learning.profile(actor)
    rev = app.conversations.detail(actor, conv)["revision"]
    two = app.conversations.submit_session(
        actor,
        conv,
        value.model_copy(
            update={
                "funding_id": None,
                "expected_revision": rev,
                "idempotency_key": "second",
                "preferences": SessionPreferences(practice=True),
            }
        ),
    )
    binding = app.conversations.session_binding(actor, two["turn_id"])
    assert binding["effective_preferences"]["requested_style"] == "architecture_first"
    assert binding["preference_sources"]["requested_style"]["event_id"] == event["event_id"]
    assert binding["effective_preferences"]["practice"] is True
    assert binding["preference_sources"]["practice"]["kind"] == "turn_request"
    await app.conversations.process(two["turn_id"])
    three = app.conversations.submit_session(
        actor,
        conv,
        value.model_copy(
            update={
                "funding_id": None,
                "expected_revision": app.conversations.detail(actor, conv)["revision"],
                "idempotency_key": "third",
            }
        ),
    )
    binding = app.conversations.session_binding(actor, three["turn_id"])
    assert binding["effective_preferences"]["requested_style"] == "code_first"
    assert binding["effective_preferences"]["practice"] is False
    assert app.learning.profile(actor) == before


@pytest.mark.asyncio
async def test_no_standalone_fact_and_mutated_snapshot_are_rejected(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    original = app.knowledge.publication_pack

    def no_standalone(*args, **kwargs):
        pack = deepcopy(original(*args, **kwargs))
        if "consumer_context" in pack:
            for fact in pack["consumer_context"]["facts"].values():
                fact["standalone"] = False
        return pack

    monkeypatch.setattr(app.knowledge, "publication_pack", no_standalone)
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)
    monkeypatch.setattr(app.knowledge, "publication_pack", original)
    one = app.conversations.submit_session(actor, conv, value)
    with app.db.transaction() as db:
        turn = db.get(Turn, one["turn_id"])
        obj = db.get(ObjectRow, turn.focus[SESSION_TUTOR_KEY]["manifest_id"])
        changed = deepcopy(obj.payload)
        changed["effective_preferences"]["practice"] = True
        obj.payload, obj.content_hash = changed, fingerprint(changed)
    before = len(env["model"].calls)
    await app.execute(one["run_id"])
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_http_contract_and_read_only_binding_forbidden_cross_owner(env):
    app = env["app"]
    conv, value, _ = await accepted(env)
    with app.db.transaction() as db:
        _, _, other_token = create_principal(db, "foreign-session-reader")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer " + env["owner"]["token"]}
        path = f"/api/v1/conversations/{conv}/session-tutor-turns"
        result = await client.post(path, json=value.model_dump(mode="json"), headers=headers)
        assert result.status_code == 202, result.text
        binding_path = f"/api/v1/turns/{result.json()['turn_id']}/session-tutor-binding"
        assert (await client.get(binding_path, headers=headers)).json()[
            "contract"
        ] == SESSION_TUTOR_CONTRACT
        assert (
            await client.get(binding_path, headers={"Authorization": "Bearer " + other_token})
        ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/conversations/{conv}/turns",
                json=value.model_dump(mode="json"),
                headers=headers,
            )
        ).status_code == 422


def test_old_turn_contract_is_unchanged_and_engine_binds_new_semantics():
    assert set(TurnInput.model_fields) == {
        "question",
        "expected_revision",
        "funding_id",
        "idempotency_key",
    }
    with pytest.raises(ValidationError):
        TurnInput(question="test", expected_revision=1, idempotency_key="x", plan_id="x")
    assert "session_tutor_implementation" in engine_versions()


@pytest.mark.asyncio
async def test_legacy_followup_never_inherits_session_preferences_or_binding(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    first = app.conversations.submit_session(actor, conv, value)
    await app.execute(first["run_id"])
    before = len(env["model"].calls)
    ordinary = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question=value.question,
            expected_revision=app.conversations.detail(actor, conv)["revision"],
            funding_id=env["funding"],
            idempotency_key="ordinary",
        ),
    )
    await app.execute(ordinary["run_id"])
    with app.db.read() as db:
        turn = db.get(Turn, ordinary["turn_id"])
        assert SESSION_TUTOR_KEY not in turn.focus
        assert turn.focus["preferences"]["practice"] is True
        assert turn.focus["preferences"]["requested_style"] == "concept_first"
    for call in env["model"].calls[before:]:
        assert "session_module" not in call["context"].get("focus", {})


@pytest.mark.asyncio
async def test_conversation_optout_and_request_override_are_auditable(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(
        env, prefs=SessionPreferences(practice=True, check_understanding=True)
    )
    event = app.learning.preference_event(
        actor,
        PreferenceEventInput(
            expected_revision=app.learning.profile(actor)["revision"],
            idempotency_key="no-practice",
            key="practice",
            value=False,
            lifetime="conversation",
            conversation_id=conv,
        ),
    )
    first = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"funding_id": None})
    )
    frozen = app.conversations.session_binding(actor, first["turn_id"])
    assert frozen["effective_preferences"]["practice"] is False
    assert frozen["preference_sources"]["practice"]["event_id"] == event["event_id"]
    await app.conversations.process(first["turn_id"])
    second = app.conversations.submit_session(
        actor,
        conv,
        value.model_copy(
            update={
                "funding_id": None,
                "idempotency_key": "override",
                "expected_revision": 2,
                "preferences": SessionPreferences(practice=True),
            }
        ),
    )
    frozen = app.conversations.session_binding(actor, second["turn_id"])
    assert frozen["effective_preferences"]["practice"] is True
    assert frozen["preference_sources"]["practice"]["kind"] == "turn_request"


@pytest.mark.asyncio
async def test_personal_erasure_scrubs_snapshot_receipts_and_prevents_replay(env):
    from test_privacy_v16 import confirm, service

    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    token = app.conversations.session_binding(actor, started["turn_id"])
    await app.execute(started["run_id"])
    privacy = service(env)
    erased = await confirm(privacy, actor, privacy.propose(actor))
    assert erased["state"] == "completed"
    with app.db.read() as db:
        obj = db.get(ObjectRow, token["manifest_id"])
        assert obj.payload == {} and obj.state == "revoked"
        assert all(
            receipt.response is None
            for receipt in db.scalars(
                select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
            )
        )
    with pytest.raises(DomainError):
        app.conversations.session_binding(actor, started["turn_id"])
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)


@pytest.mark.asyncio
async def test_source_grant_revocation_blocks_paid_receipt_replay(env):
    from careerforge.services.security import set_grant

    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "source-owner")
    foreign = {
        **env,
        "actor": other.id,
        "ns": ns.id,
        "funding": app.runtime.create_funding(other.id, ns.id, 100),
    }
    _, publication, _ = await publish_fixture(foreign, key="foreign-build")
    with app.db.transaction() as db:
        set_grant(db, other.id, ns.id, actor, "reader", True)
    planner(env, [10])
    started = app.learning.start_session_plan(actor, request(env, [publication]))
    await app.execute(started["run_id"])
    plan = app.runtime.stage(started["run_id"], "result")
    app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
    conv = app.conversations.create(actor)
    value = SessionTutorTurnInput(
        question="函数返回什么 value",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="foreign-module",
        namespace_id=env["ns"],
        plan_id=plan["plan_id"],
        version_id=plan["version_id"],
        module_id=plan["module_ids"][0],
        expected_plan_revision=2,
    )
    turn = app.conversations.submit_session(actor, conv, value)
    await app.execute(turn["run_id"])
    with app.db.transaction() as db:
        set_grant(db, other.id, ns.id, actor, "reader", False)
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)
    with pytest.raises(DomainError):
        app.answers.events(actor, turn["turn_id"])
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("classes", [["public_source"], ["private_source"], ["learning_state"]])
async def test_session_context_cannot_bypass_funding_data_classes(env, classes):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    funding = app.runtime.create_funding(actor, env["ns"], 8, data_classes=classes)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        app.conversations.submit_session(
            actor, conv, value.model_copy(update={"funding_id": funding})
        )
    assert error.value.code == "EGRESS_OR_ROLE_DENIED"
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_no_run_binding_cannot_be_stripped_to_silently_downgrade(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"funding_id": None})
    )
    with app.db.transaction() as db:
        turn = db.get(Turn, started["turn_id"])
        turn.focus = {k: v for k, v in turn.focus.items() if k != SESSION_TUTOR_KEY}
    with pytest.raises(DomainError) as error:
        await app.conversations.process(started["turn_id"])
    assert error.value.code == "SESSION_TUTOR_BINDING"


@pytest.mark.asyncio
async def test_explicit_activity_after_tutor_joins_frozen_module_without_completing_it(env):
    from careerforge.contracts_learning_ext import ActivityInput, AttemptInput, AttemptSubmission

    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    frozen = app.conversations.session_binding(actor, started["turn_id"])
    # The accepted session disabled checks. A delivered answer grants no activity consent.
    with pytest.raises(DomainError) as error:
        app.learning.create_activity(actor, ActivityInput(module_id=value.module_id))
    assert error.value.code == "LEARNING_OPT_OUT"
    # A separate explicit activity request uses the real existing contract.
    activity = app.learning.create_activity(
        actor,
        ActivityInput(
            module_id=value.module_id,
            explicitly_requested=True,
            focus_fact_ids=frozen["module"]["focus_fact_ids"],
        ),
    )
    spec = activity["specification"]
    assert spec["module_id"] == frozen["module"]["module_id"]
    assert spec["module_content_hash"] == frozen["module"]["module_content_hash"]
    assert spec["publication_ids"] == [frozen["module"]["publication_id"]]
    assert {f["fact_atom_id"] for f in spec["facts"]} == set(frozen["module"]["focus_fact_ids"])
    attempt = app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(
            assistance_mode="assisted",
            idempotency_key="followup-attempt",
        ),
    )
    submitted = app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(
            response="该函数返回传入的 value。",
            external_help="used",
        ),
    )
    assert submitted["state"] == "submitted" and submitted["requires_rubric_review"]
    assert submitted["competence_verified"] is False
    assert app.learning.profile(actor)["active_module_id"] == value.module_id
    assert app.conversations.session_binding(actor, started["turn_id"])["module_completed"] is False


@pytest.mark.asyncio
async def test_non_session_plan_and_empty_unbuildable_draft_cannot_authorize_tutor(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    with app.db.read() as db:
        publication = db.get(ObjectRow, value.module_id).payload["publication_id"]
    generic = app.learning.propose_plan(actor, [publication], "function", "not a session")
    app.learning.accept_plan(actor, generic["plan_id"], generic["version_id"], 1)
    with pytest.raises(DomainError) as error:
        app.conversations.submit_session(
            actor,
            conv,
            value.model_copy(
                update={
                    "plan_id": generic["plan_id"],
                    "version_id": generic["version_id"],
                    "module_id": generic["module_ids"][0],
                }
            ),
        )
    assert error.value.code == "SESSION_TUTOR_PLAN"
    empty = app.learning.start_session_plan(actor, request(env, [], key="empty"))
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        app.conversations.submit_session(
            actor,
            conv,
            value.model_copy(
                update={
                    "plan_id": empty["draft_id"],
                    "version_id": empty["draft_id"],
                    "module_id": empty["draft_id"],
                }
            ),
        )
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_completion_checker_replacement_blocks_terminal_success(env):
    app, actor = env["app"], env["actor"]
    conv, value, plan = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    original = env["model"].complete

    async def replace_after_completion(request):
        output = await original(request)
        if request["context"].get("assessment_kind") == "rendered_answer_completeness":
            revision = app.learning.revise_plan(
                actor, value.plan_id, 2, "replace", plan["module_ids"]
            )
            app.learning.accept_plan(
                actor, value.plan_id, revision["version_id"], revision["expected_revision"]
            )
        return output

    env["model"].complete = replace_after_completion
    await app.execute(started["run_id"])
    assert app.runtime.stage(started["run_id"], "result") is None
    with app.db.read() as db:
        assert db.get(Turn, started["turn_id"]).status == "interrupted"
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == started["run_id"],
                ModelReceipt.stage_key == ANSWER_COMPLETION_STAGE,
            )
        )
        assert receipt.status == "redacted" and receipt.response is None
        assert list(
            db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == started["turn_id"]))
        )
    history = app.answers.events(actor, started["turn_id"])
    assert not any(event["type"] == "answer.completed" for event in history["events"])


@pytest.mark.asyncio
async def test_explicit_module_resolves_continue_without_inheriting_unrelated_focus(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    with app.db.transaction() as db:
        row = db.get(Conversation, conv)
        row.focus = {"topic_keys": ["unrelated.topic"], "project_source_ids": ["unrelated-source"]}
    started = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"question": "继续", "funding_id": None})
    )
    await app.conversations.process(started["turn_id"])
    with app.db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        assert turn.focus["topic_keys"] == ["agent.tool_calling"]
        assert turn.focus["project_source_ids"] != ["unrelated-source"]
    assert any(block["fact_atom_ids"] for block in app.answers.committed(actor, started["turn_id"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["role", "source"])
async def test_session_funding_role_and_source_rejected_before_admission(env, restriction):
    from support import FILES

    from careerforge.contracts import ImportSourceInput

    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    kwargs = {"roles": ["tutor"]}
    if restriction == "source":
        unrelated = app.sources.import_source(
            actor,
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key="fixtures/unrelated",
                commit_sha="c" * 40,
                license_id="MIT",
                files=FILES,
                source_metadata={"synthetic": True},
            ),
        )
        kwargs = {"allowed_source_ids": [unrelated]}
    funding = app.runtime.create_funding(actor, env["ns"], 8, **kwargs)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        app.conversations.submit_session(
            actor, conv, value.model_copy(update={"funding_id": funding})
        )
    assert error.value.code == (
        "EGRESS_OR_ROLE_DENIED" if restriction == "role" else "FUNDING_SOURCE_SCOPE"
    )
    assert len(env["model"].calls) == before
    assert app.conversations.detail(actor, conv)["revision"] == 1


@pytest.mark.asyncio
async def test_guided_endpoint_resolves_accepted_module_without_learner_question(env):
    app, actor = env["app"], env["actor"]
    conv, value, plan = await accepted(env, accept=False)
    body = {
        "expected_revision": 1,
        "idempotency_key": "guided",
        "preferences": {"practice": False, "check_understanding": False},
    }
    path = f"/api/v1/conversations/{conv}/guided-turns"
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test", headers=headers
    ) as client:
        not_ready = await client.post(path, json=body)
        assert not_ready.status_code == 409
        app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
        started = await client.post(path, json=body)
        assert started.status_code == 202, started.text
        turn = started.json()
        assert turn["run_id"] is None
        binding = app.conversations.session_binding(actor, turn["turn_id"])
        assert binding["module"]["module_id"] == value.module_id
        assert binding["module"]["version_id"] == value.version_id
        assert not binding["module_completed"]
        event = app.answers.events(actor, turn["turn_id"])
        assert event["status"] == "completed"
        assert any(
            row["payload"]["content"]["citations"]
            for row in event["events"]
            if row["type"] == "answer.block.validated"
        )
        replay = await client.post(path, json=body)
        assert replay.json()["turn_id"] == turn["turn_id"]
        assert (await client.post(path, json={**body, "question": "换个问题"})).status_code == 409
        assert (await client.post(path, json={**body, "module_id": "untrusted"})).status_code == 422


@pytest.mark.asyncio
async def test_natural_continuation_from_chat_binds_accepted_module_and_exact_replay(env):
    app, actor = env["app"], env["actor"]
    conv, selected, _ = await accepted(env)
    body = TurnInput(
        question="请接着讲一下当前学习模块",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="chat-continue",
    )
    started = app.conversations.submit(actor, conv, body)
    binding = app.conversations.session_binding(actor, started["turn_id"])
    assert binding["module"]["module_id"] == selected.module_id
    assert binding["module"]["version_id"] == selected.version_id
    assert app.conversations.submit(actor, conv, body)["turn_id"] == started["turn_id"]
    with pytest.raises(DomainError) as raised:
        app.conversations.submit(actor, conv, body.model_copy(update={"question": "开始本模块"}))
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
