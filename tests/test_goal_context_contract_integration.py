"""Merged M3/opt-in packed transport, real domain pipeline and synthetic inference."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select
from support import FixtureModel, FixtureRepositories
from test_dialogue_actions import api_client, send
from test_dialogue_workflow_e2e import fixed_source
from test_goal_amendment_dialogue_m3 import setup_goal

from careerforge.adapters.request_contract import (
    CONTEXT_ONCE_CONTRACT,
    MODEL_REQUEST_MAX_BYTES,
    json_bytes,
    legacy_request,
    request_context,
    validate_request,
)
from careerforge.application import Application
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, FundingAgreement, GoalVersion, ModelReceipt
from careerforge.models_dialogue import WorkflowExecution, WorkflowSession
from careerforge.services.dependencies import invalidate
from careerforge.services.research_coverage import read_goal_version
from careerforge.util import fingerprint


class PackedGoalFixture(FixtureModel):
    request_contract = CONTEXT_ONCE_CONTRACT
    profile_version = "synthetic-packed-goal-m3-v1"

    def __init__(self):
        super().__init__()
        self.packed_calls = []

    def validate_request(self, request):
        validate_request(request, authority=True)

    async def complete(self, request):
        self.packed_calls.append(deepcopy(request))
        # Only local fake inference decodes; actual transport retains packed bytes.
        return await super().complete(legacy_request(request))


@pytest.fixture
def packed_goal_env(env, tmp_path):
    provider = PackedGoalFixture()
    settings = replace(
        env["app"].settings,
        model_request_contract=CONTEXT_ONCE_CONTRACT,
        database_url="sqlite+pysqlite:///" + str(tmp_path / "packed-goal.db"),
        state_dir=tmp_path / "packed-goal-state",
    )
    app = Application(settings, provider, FixtureRepositories())
    owner = app.bootstrap("packed-goal")
    funding = app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 100)
    yield dict(
        app=app,
        model=provider,
        owner=owner,
        actor=owner["principal_id"],
        ns=owner["namespace_id"],
        funding=funding,
    )
    app.db.engine.dispose()


def admit(env):
    app, path, _, old, snapshot = setup_goal(env)
    source = fixed_source(env)
    cid = path.split("/")[-1]
    with app.db.transaction() as db:
        revision = db.get(Conversation, cid).revision
        db.get(FundingAgreement, env["funding"]).allowed_source_ids = [source]
    result = app.dialogue.dispatch(
        env["actor"],
        cid,
        DialogueMessage(
            text="增加 agent.tool_calling，要求掌握。",
            expected_focus_revision=revision,
            idempotency_key="packed-goal",
            authorization_ref=env["funding"],
            source_id=source,
        ),
        forced_actions=[
            (
                "add_learning_objective",
                {
                    "target": "agent.tool_calling",
                    "target_depth": "掌握",
                    "learning_priority": "required",
                },
            ),
            ("start_build", {}),
        ],
    )
    assert [x["status"] for x in result["action_receipts"]] == ["applied", "applied"], result
    version = result["action_receipts"][0]["new_goal_version_id"]
    eid = result["action_receipts"][1]["execution_id"]
    with app.db.read() as db:
        scope = deepcopy(
            db.get(WorkflowExecution, eid).binding_payload["admitted_scope"]["goal_build_scope"]
        )
    return path, result, source, old, snapshot, version, eid, scope


def usage(env, root):
    with env["app"].db.read() as db:
        run = db.get(AgentRun, root)
        funding = db.get(FundingAgreement, env["funding"])
        receipts = list(db.scalars(select(ModelReceipt.id)))
        return (
            run.calls_consumed,
            run.held_calls,
            funding.calls_consumed,
            funding.calls_held,
            receipts,
        )


async def test_packed_v2_build_review_plan_and_exact_acceptance_preserve_lineage(packed_goal_env):
    env = packed_goal_env
    app, actor, model = env["app"], env["actor"], env["model"]
    path, result, source, old, snapshot, version, eid, scope = admit(env)
    assert not model.packed_calls
    await app.drain()
    with app.db.read() as db:
        execution = db.get(WorkflowExecution, eid)
        workflow = db.get(WorkflowSession, result["workflow_id"])
        assert execution.wait_kind == "approval", execution.last_error_code
        assert execution.goal_version_id == version and not workflow.target.get("accepted_plan")
        assert db.get(GoalVersion, version).parent_goal_version_id == old
        assert db.get(GoalVersion, version).run_id is None
        assert read_goal_version(db, actor, old) == snapshot
        effective = read_goal_version(db, actor, version)
        assert effective["source_text"] == snapshot["source_text"]
        assert effective["requirements"] == snapshot["requirements"]
        assert effective["user_learning_objectives"][0]["target_depth"] == "掌握"
        proposal = deepcopy(workflow.target["proposal"])
        frozen_plan = db.get(AgentRun, proposal["run_id"]).input_manifest
        assert frozen_plan["goal"]["goal_version_id"] == version
        assert frozen_plan["goal_amendment_plan"]["scope_hash"] == scope["scope_hash"]
        receipts = list(db.scalars(select(ModelReceipt)))
        assert receipts and all(
            r.status == "completed" and r.profile_version == model.profile_version for r in receipts
        )
        assert (
            db.get(FundingAgreement, env["funding"]).calls_consumed
            == len(receipts)
            == len(model.packed_calls)
        )
    roles = {r["role"] for r in model.packed_calls}
    assert {"analysis", "reviewer", "learning_planner"} <= roles
    for request in model.packed_calls:
        validate_request(request, authority=True)
        assert "context_binding" in request and "context" not in request
        assert len(json_bytes(request)) <= MODEL_REQUEST_MAX_BYTES == 250_000
        assert request["max_output_tokens"] <= 8192 and request["funding_source_scope"] == [source]
        assert request["profile_version"] == model.profile_version
        context = request_context(request)
        assert context == legacy_request(request)["context"]
        assert request["context_binding"]["sha256"] == fingerprint(context)
        if request["role"] in {"analysis", "reviewer"}:
            assert context["research_task"]["goal_version_id"] == version
            assert context["research_task"]["requirements"] == []
        if request["role"] == "learning_planner":
            assert context["frozen_input"]["goal"]["goal_version_id"] == version
            assert (
                context["frozen_input"]["goal_amendment_plan"]["scope_hash"] == scope["scope_hash"]
            )
    before = len(model.packed_calls)
    async with api_client(env) as api:
        pause = (await send(api, path, "/pause")).json()
        assert pause["action_receipts"][0]["status"] == "applied"
        await app.drain()
        assert (await send(api, path, "/resume")).json()["action_receipts"][0][
            "status"
        ] == "applied"
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, result["workflow_id"])
            assert not workflow.target.get("accepted_plan")
            revision = workflow.revision
        assert len(model.packed_calls) == before  # Resume itself cannot approve or pay.
        accepted = await api.post(
            f"/workflows/{result['workflow_id']}/confirmations/{proposal['version_id']}",
            json={"expected_revision": revision, "idempotency_key": "packed-exact-plan"},
        )
        assert accepted.status_code == 202, accepted.text
    await app.drain()
    with app.db.read() as db:
        assert db.get(WorkflowExecution, eid).status == "completed"
        assert read_goal_version(db, actor, old) == snapshot
        workflow = db.get(WorkflowSession, result["workflow_id"])
        assert workflow.target["accepted_plan"]["active_version_id"] == proposal["version_id"]
        assert all(run.calls_consumed <= run.call_cap for run in db.scalars(select(AgentRun)))
    # The explicit confirmation route also starts Tutor. No build/planner replay.
    assert all(
        r["role"] not in {"analysis", "reviewer", "learning_planner"}
        for r in model.packed_calls[before:]
    )
    assert all(r["profile_version"] == model.profile_version for r in model.packed_calls)


@pytest.mark.parametrize("failure", ["envelope", "profile", "scope", "source"])
async def test_scoped_packed_dispatch_guards_fail_before_receipt_or_charge(
    packed_goal_env, failure
):
    env = packed_goal_env
    app, model = env["app"], env["model"]
    _, result, source, old, snapshot, version, _, scope = admit(env)
    root = result["run_refs"][0]
    epoch = app.runtime.claim(root, "packed-guard")
    assert epoch is not None
    before = usage(env, root)
    if failure == "profile":
        model.profile_version = "unexpected-new-profile"
    elif failure == "scope":
        with app.db.transaction() as db:
            run = db.get(AgentRun, root)
            run.input_manifest = {
                **run.input_manifest,
                "goal_build_scope": {**scope, "goal_version_id": old},
            }
            # A self-consistent hash still cannot rebind the admitted GoalVersion.
            run.input_hash = fingerprint(run.input_manifest)
    elif failure == "source":
        with app.db.transaction() as db:
            invalidate(db, env["actor"], source, "revoked", "scope guard test")
    context = {"goal_version_id": version, "goal_build_scope": scope}
    if failure == "envelope":
        context["full_data"] = "x" * MODEL_REQUEST_MAX_BYTES
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            root,
            epoch,
            "packed-guard",
            "guarded",
            "analysis",
            "unchanged",
            context,
            data_classes=["private_source", "private_jd"],
            access_dependencies=[source, version],
        )
    assert (
        error.value.code
        in {
            "envelope": {"MODEL_CONTEXT_LIMIT"},
            "profile": {"MODEL_PROFILE_CHANGED"},
            "scope": {"GOAL_BUILD_SCOPE"},
            "source": {"EVIDENCE_UNAVAILABLE"},
        }[failure]
    )
    assert usage(env, root) == before
    assert not model.packed_calls
    with app.db.read() as db:
        assert read_goal_version(db, env["actor"], old) == snapshot
