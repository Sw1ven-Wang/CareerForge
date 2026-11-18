"""G07: real API/service transactions; only model/repository inference is synthetic."""

import json

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from support import FILES

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput
from careerforge.contracts_runtime_ext import SharedBuildInput, SubscribeInput, UnsubscribeInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt, StageArtifact
from careerforge.models_privacy_ext import PrivacyState
from careerforge.models_runtime_ext import SharedBuild, SharedBuildSlot, TaskSubscription
from careerforge.services.security import create_principal, set_grant


def public_contract(env, *, policy="continue", key="shared-create"):
    app, actor = env["app"], env["actor"]
    public = env["owner"]["public_namespace_id"]
    funding = app.runtime.create_funding(actor, public, 100)
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=public,
            repository_key="fixtures/public-shared",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    body = SharedBuildInput(
        namespace_id=public,
        funding_id=funding,
        source_id=source,
        topic_keys=["agent.tool_calling"],
        call_cap=10,
        max_units_per_project=1,
        no_subscribers_policy=policy,
        idempotency_key=key,
        confirm="SYSTEM_BUILD",
    )
    return body


def subscriber(env, name="subscriber"):
    with env["app"].db.transaction() as db:
        user, private, token = create_principal(db, name)
        return {"actor": user.id, "ns": private.id, "token": token}


def test_public_singleflight_never_moves_reservation_to_subscriber_or_second_funding(env):
    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    first = app.shared_builds.create(actor, body)
    assert app.shared_builds.create(actor, body)["run_id"] == first["run_id"]
    second_funding = app.runtime.create_funding(actor, body.namespace_id, 50)
    second = app.shared_builds.create(
        actor, body.model_copy(update={"funding_id": second_funding, "idempotency_key": "other"})
    )
    assert second["run_id"] == first["run_id"]
    user = subscriber(env)
    assert (
        app.shared_builds.subscribe(
            user["actor"], first["run_id"], SubscribeInput(idempotency_key="join")
        )["subscription_is_payment_consent"]
        is False
    )
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(SharedBuild)) == 1
        assert db.scalar(select(func.count()).select_from(SharedBuildSlot)) == 1
        assert db.get(FundingAgreement, body.funding_id).calls_held == body.call_cap
        assert db.get(FundingAgreement, second_funding).calls_held == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(FundingAgreement)
                .where(FundingAgreement.payer_id == user["actor"])
            )
            == 0
        )
        subscription = db.scalar(select(TaskSubscription))
        assert subscription.payment_consent_id is None
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(
            actor,
            body.model_copy(
                update={"funding_id": env["funding"], "idempotency_key": "private-funding"}
            ),
        )
    assert error.value.code == "SYSTEM_FUNDING"


@pytest.mark.parametrize(
    "policy,expected", [("continue", "queued"), ("cancel_after_last_detach", "cancelled")]
)
def test_detach_only_own_subscription_and_explicit_last_detach_policy(env, policy, expected):
    app, actor = env["app"], env["actor"]
    body = public_contract(env, policy=policy)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    one, two = subscriber(env, "one"), subscriber(env, "two")
    join = SubscribeInput(idempotency_key="join")
    for user in [one, two]:
        app.shared_builds.subscribe(user["actor"], run_id, join)
    app.shared_builds.unsubscribe(one["actor"], run_id, UnsubscribeInput(idempotency_key="leave"))
    # Replayed old join cannot resurrect a later explicit detach.
    assert not app.shared_builds.subscribe(one["actor"], run_id, join)["subscribed"]
    assert app.shared_builds.detail(two["actor"], run_id)["status"] == "queued"
    app.shared_builds.unsubscribe(two["actor"], run_id, UnsubscribeInput(idempotency_key="leave"))
    app.shared_builds.unsubscribe(two["actor"], run_id, UnsubscribeInput(idempotency_key="leave"))
    with app.db.read() as db:
        run, funding = db.get(AgentRun, run_id), db.get(FundingAgreement, body.funding_id)
        assert run.status == expected
        assert funding.calls_held == (0 if expected == "cancelled" else body.call_cap)
        assert all(row.revision == 2 for row in db.scalars(select(TaskSubscription)))


def test_subscriber_cannot_cancel_public_root_or_subscribe_private_task(env):
    app, actor = env["app"], env["actor"]
    run_id = app.shared_builds.create(actor, public_contract(env))["run_id"]
    user = subscriber(env)
    joined = app.shared_builds.subscribe(
        user["actor"], run_id, SubscribeInput(idempotency_key="join")
    )
    with pytest.raises(DomainError):
        app.runtime.cancel(user["actor"], run_id, joined["revision"])
    private = app.runtime.create(
        actor, env["ns"], env["funding"], "research", {"secret": "PRIVATE"}, 1, "private"
    )
    with pytest.raises(DomainError) as error:
        app.shared_builds.subscribe(
            user["actor"], private, SubscribeInput(idempotency_key="private")
        )
    assert error.value.code == "NOT_FOUND"
    with app.db.read() as db:
        assert db.get(SharedBuild, private) is None


def test_private_inputs_cannot_be_promoted_even_by_system_admin(env):
    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/private",
            commit_sha="b" * 40,
            license_id="MIT",
            files=FILES,
        ),
    )
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(actor, body.model_copy(update={"source_id": source}))
    assert error.value.code == "PRIVATE_TO_PUBLIC"
    for field in ["question", "jd_text", "private_run_id", "subscriber_id", "payment_consent_id"]:
        with pytest.raises(ValidationError):
            SharedBuildInput.model_validate({**body.model_dump(), field: "PRIVATE"})


@pytest.mark.asyncio
async def test_public_execution_and_http_projection_do_not_disclose_other_subscribers(env):
    app = env["app"]
    body = public_contract(env)
    api = create_app(app)
    one, two = subscriber(env, "http-one"), subscriber(env, "http-two")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + env["owner"]["token"]
        created = await client.post("/api/v1/shared-builds", json=body.model_dump())
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        for user in [one, two]:
            client.headers["Authorization"] = "Bearer " + user["token"]
            joined = await client.post(
                f"/api/v1/shared-builds/{run_id}/subscriptions", json={"idempotency_key": "join"}
            )
            assert joined.status_code == 200, joined.text
        await app.drain()
        client.headers["Authorization"] = "Bearer " + one["token"]
        packet = await client.get(f"/api/v1/shared-builds/{run_id}")
        assert packet.status_code == 200, packet.text
        value = packet.json()
        # The public facts are admitted, but the synthetic model supplies no
        # complete task assessment under the new research contract.
        assert value["status"] == "partially_succeeded" and value["publication_ids"]
        serialized = json.dumps(value)
        for forbidden in [
            two["actor"],
            two["ns"],
            body.funding_id,
            "subscriber_count",
            "input_manifest",
            "question",
        ]:
            assert forbidden not in serialized
        # Existing run endpoint must not become a private manifest bypass.
        direct = await client.get(f"/api/v1/runs/{run_id}")
        assert direct.status_code == 404
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).funding_id == body.funding_id
        assert all(
            row.funding_id == body.funding_id
            for row in db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id))
        )
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) > 0


def test_shared_contract_tampering_is_fenced_before_claim(env):
    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        run.input_manifest = {**run.input_manifest, "jd_text": "private text"}
    assert app.runtime.claim(run_id, "attacker") is None
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "failed"
        assert db.get(FundingAgreement, body.funding_id).calls_held == 0
        assert not db.scalar(select(ModelReceipt))


def test_private_funding_revocation_and_personal_erasure_do_not_cancel_public_system_run(env):
    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    private = app.runtime.create(actor, env["ns"], env["funding"], "research", {}, 1, "private")
    app.runtime.revoke_funding(actor, env["funding"])
    with app.db.transaction() as db:
        db.get(PrivacyState, actor).blocked = True
        assert app.runtime.cancel_owned_in(db, actor) == 0
    epoch = app.runtime.claim(run_id, "public-worker")
    assert epoch is not None
    app.runtime.save_stage(run_id, epoch, "public-worker", "public-proof", {"public": True})
    with app.db.read() as db:
        assert db.get(AgentRun, private).status == "cancelled"
        assert db.get(AgentRun, run_id).status == "running"
        assert db.scalar(select(StageArtifact).where(StageArtifact.run_id == run_id))


def test_erasure_cancels_work_in_owned_namespace_even_when_another_principal_runs_it(env):
    app, actor = env["app"], env["actor"]
    user = subscriber(env)
    with app.db.transaction() as db:
        set_grant(db, actor, env["ns"], user["actor"], "writer", True)
    funding = app.runtime.create_funding(user["actor"], env["ns"], 10)
    run_id = app.runtime.create(user["actor"], env["ns"], funding, "research", {}, 2, "delegated")
    with app.db.transaction() as db:
        db.get(PrivacyState, actor).blocked = True
    with app.db.transaction() as db:
        assert app.runtime.cancel_owned_in(db, actor) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "cancelled"
        assert db.get(FundingAgreement, funding).calls_held == 0


def test_new_roles_do_not_expand_existing_explicit_funding_scopes(env):
    app, actor = env["app"], env["actor"]
    funding = app.runtime.create_funding(actor, env["ns"], 10, roles=["tutor"])
    for workflow in ["retrieval_index", "learning_plan", "evolution_replay"]:
        assert app.runtime.create(actor, env["ns"], funding, workflow, {}, 1, workflow)
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).allowed_roles == ["tutor"]


@pytest.mark.parametrize(
    "policy,expected", [("continue", "queued"), ("cancel_after_last_detach", "cancelled")]
)
def test_trusted_erasure_detach_respects_policy_while_personal_mutations_are_blocked(
    env, policy, expected
):
    app, actor = env["app"], env["actor"]
    body = public_contract(env, policy=policy)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    user = subscriber(env)["actor"]
    app.shared_builds.subscribe(user, run_id, SubscribeInput(idempotency_key="join"))
    with app.db.transaction() as db:
        db.get(PrivacyState, user).blocked = True
    for operation in [
        lambda: app.shared_builds.detail(user, run_id),
        lambda: app.shared_builds.subscribe(user, run_id, SubscribeInput(idempotency_key="again")),
    ]:
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "PRIVACY_ERASURE_PENDING"
    with app.db.transaction() as db:
        assert app.shared_builds.detach_owned_in(db, user) == 1
        assert app.shared_builds.detach_owned_in(db, user) == 0
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == expected
        assert db.scalar(select(TaskSubscription)).active is False


def test_public_system_rejects_corrupt_private_transitive_ancestry_before_spending(env):
    """Defense in depth for historical/imported graph rows bypassing depend()."""
    from careerforge.models import ArtifactDependency
    from careerforge.services.dependencies import register

    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    with app.db.transaction() as db:
        private = register(db, actor, env["ns"], "facet_value", {"secret": "private"})
        db.add(
            ArtifactDependency(
                edge_namespace_id=body.namespace_id,
                dependent_id=body.source_id,
                dependent_type="source_set",
                dependent_namespace_id=body.namespace_id,
                source_id=private.id,
                source_type="facet_value",
                source_namespace_id=env["ns"],
                kind="corrupt_historical_import",
                enforcement="blocking",
                authorization_ref="test-only-corrupt-row",
                authorization_revision=1,
            )
        )
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(actor, body)
    assert error.value.code == "PRIVATE_TO_PUBLIC"
    with app.db.read() as db:
        assert db.get(FundingAgreement, body.funding_id).calls_held == 0
        assert not db.scalar(select(SharedBuild))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["stage_hash", "different_source", "same_source_other_run"])
async def test_shared_result_is_bound_to_its_frozen_source_and_execution_lineage(env, tamper):
    from conftest import publish_fixture

    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    await app.drain()
    assert app.shared_builds.detail(actor, run_id)["publication_ids"]
    if tamper != "stage_hash":
        repository = (
            "fixtures/public-shared" if tamper == "same_source_other_run" else "fixtures/unrelated"
        )
        source, wrong_pub, _ = await publish_fixture(
            env, key="other-approved-run", repository=repository, namespace=body.namespace_id
        )
        assert (source == body.source_id) == (tamper == "same_source_other_run")
    with app.db.transaction() as db:
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        if tamper == "stage_hash":
            stage.input_hash = "0" * 64
        else:
            stage.payload = {**stage.payload, "publication_ids": [wrong_pub]}
            # Matching the root hash cannot make a foreign approved publication a result.
            stage.input_hash = db.get(AgentRun, run_id).input_hash
    with pytest.raises(DomainError) as error:
        app.shared_builds.detail(actor, run_id)
    assert error.value.code == "SHARED_BUILD_SCOPE"
