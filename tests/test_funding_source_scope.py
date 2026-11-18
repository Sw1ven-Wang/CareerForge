"""Fixed source funding is enforced independently of ordinary object ACLs."""

import pytest
from sqlalchemy import select
from support import FILES

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, QueryPlan
from careerforge.errors import DomainError
from careerforge.models import FundingAgreement, ModelReceipt
from careerforge.services.dependencies import assert_source_scope, depend, register
from careerforge.services.security import create_principal


def source(env, key, namespace=None):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=namespace or env["ns"],
            repository_key="fixtures/" + key,
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
        ),
    )


def scoped_run(env, allowed):
    app, actor = env["app"], env["actor"]
    funding = app.runtime.create_funding(actor, env["ns"], 10, allowed_source_ids=allowed)
    identifier = app.runtime.create(actor, env["ns"], funding, "research", {}, 4, "scoped")
    epoch = app.runtime.claim(identifier, "scoped-worker")
    assert epoch is not None
    return funding, (identifier, epoch, "scoped-worker")


def test_scope_sql_checks_transitive_sources_without_treating_acl_as_payment(env):
    app, actor = env["app"], env["actor"]
    allowed, outside = source(env, "allowed"), source(env, "outside")
    with app.db.transaction() as db:
        evidence = register(db, actor, env["ns"], "evidence", {})
        claim = register(db, actor, env["ns"], "claim", {})
        fact = register(db, actor, env["ns"], "fact_atom", {})
        publication = register(db, actor, env["ns"], "publication", {})
        depend(db, actor, evidence.id, outside, "grounded_in")
        depend(db, actor, claim.id, evidence.id, "requires_evidence")
        depend(db, actor, fact.id, claim.id, "requires_claim")
        depend(db, actor, publication.id, fact.id, "requires_fact")
        with pytest.raises(DomainError) as error:
            assert_source_scope(db, [publication.id], [allowed])
        assert error.value.code == "FUNDING_SOURCE_SCOPE"
        assert_source_scope(db, [publication.id], [allowed, outside])
        assert_source_scope(db, [publication.id], [])  # Existing unrestricted semantics.


def test_funding_creation_validates_real_source_acl_and_public_scope(env):
    app, actor = env["app"], env["actor"]
    private = source(env, "private")
    funding = app.runtime.create_funding(
        actor, env["ns"], 10, allowed_source_ids=[private, private]
    )
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).allowed_source_ids == [private]
        assert db.get(FundingAgreement, env["funding"]).allowed_source_ids == []
    with pytest.raises(DomainError) as error:
        app.runtime.create_funding(
            actor, env["owner"]["public_namespace_id"], 10, allowed_source_ids=[private]
        )
    assert error.value.code == "PRIVATE_TO_PUBLIC"
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "scoped-other")
        other_id, other_ns = other.id, ns.id
    with pytest.raises(DomainError) as error:
        app.runtime.create_funding(other_id, other_ns, 10, allowed_source_ids=[private])
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(DomainError):
        app.runtime.create_funding(actor, env["ns"], 10, allowed_source_ids=["unknown"])


@pytest.mark.asyncio
async def test_model_source_outside_funding_is_rejected_before_provider_or_receipt(env):
    allowed, outside = source(env, "allowed"), source(env, "outside")
    funding, run = scoped_run(env, [allowed])
    with pytest.raises(DomainError) as error:
        await env["app"].gateway.call(
            *run,
            "outside",
            "planner",
            "Fixture",
            {},
            QueryPlan,
            data_classes=["private_source"],
            access_dependencies=[outside],
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    assert not env["model"].calls
    with env["app"].db.read() as db:
        assert db.get(FundingAgreement, funding).calls_consumed == 0
        assert not db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))


@pytest.mark.asyncio
async def test_model_allowed_and_unrestricted_source_contracts_still_work(env):
    allowed = source(env, "allowed")
    funding, run = scoped_run(env, [allowed])
    result = await env["app"].gateway.call(
        *run,
        "allowed",
        "planner",
        "Fixture",
        {},
        QueryPlan,
        data_classes=["private_source"],
        access_dependencies=[allowed],
    )
    assert result.structured["queries"]
    with env["app"].db.read() as db:
        assert db.get(FundingAgreement, funding).calls_consumed == 1


@pytest.mark.asyncio
async def test_source_scope_narrowed_while_provider_in_flight_redacts_body_without_rebilling(
    env, monkeypatch
):
    app = env["app"]
    allowed, replacement = source(env, "allowed"), source(env, "replacement")
    funding, run = scoped_run(env, [allowed])
    original = env["model"].complete

    async def changed_scope(request):
        with app.db.transaction() as db:
            db.get(FundingAgreement, funding).allowed_source_ids = [replacement]
        return await original(request)

    monkeypatch.setattr(env["model"], "complete", changed_scope)
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            *run,
            "inflight",
            "planner",
            "Fixture",
            {},
            QueryPlan,
            data_classes=["private_source"],
            access_dependencies=[allowed],
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))
        assert receipt.status == "redacted" and receipt.response is None
        assert db.get(FundingAgreement, funding).calls_consumed == 1
    assert len(env["model"].calls) == 1


def test_build_cannot_reserve_for_a_different_fixed_source(env):
    app, actor = env["app"], env["actor"]
    allowed, outside = source(env, "allowed"), source(env, "outside")
    funding = app.runtime.create_funding(actor, env["ns"], 10, allowed_source_ids=[allowed])
    with pytest.raises(DomainError) as error:
        app.build.start(
            actor,
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=funding,
                source_id=outside,
                question="Fixed source scope",
                topic_keys=["agent.tool_calling"],
                call_cap=10,
                max_units_per_project=1,
                idempotency_key="outside-build",
            ),
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).calls_held == 0


@pytest.mark.asyncio
async def test_source_scope_changed_after_reservation_prevents_dispatch(env, monkeypatch):
    app = env["app"]
    allowed, replacement = source(env, "allowed"), source(env, "replacement")
    funding, run = scoped_run(env, [allowed])
    original = app.runtime.validate
    changed = False

    def change_after_last_lease_check(*args):
        nonlocal changed
        original(*args)
        if not changed:
            changed = True
            with app.db.transaction() as db:
                db.get(FundingAgreement, funding).allowed_source_ids = [replacement]

    monkeypatch.setattr(app.runtime, "validate", change_after_last_lease_check)
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            *run,
            "dispatch",
            "planner",
            "Fixture",
            {},
            QueryPlan,
            data_classes=["private_source"],
            access_dependencies=[allowed],
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    assert not env["model"].calls
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))
        assert receipt.response is None


def test_singleflight_reuse_cannot_bypass_new_requests_fixed_source_funding(env):
    from test_shared_builds import public_contract

    app, actor = env["app"], env["actor"]
    body = public_contract(env)
    run_id = app.shared_builds.create(actor, body)["run_id"]
    unrelated = source(env, "unrelated-public", namespace=body.namespace_id)
    restricted = app.runtime.create_funding(
        actor, body.namespace_id, 10, allowed_source_ids=[unrelated]
    )
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(
            actor,
            body.model_copy(
                update={"funding_id": restricted, "idempotency_key": "restricted-reuse"}
            ),
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    with app.db.read() as db:
        assert db.get(FundingAgreement, restricted).calls_held == 0
    assert app.shared_builds.detail(actor, run_id)["status"] == "queued"


def test_runtime_rechecks_funding_source_scope_after_service_preparation(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    allowed, replacement = source(env, "allowed"), source(env, "replacement")
    funding = app.runtime.create_funding(actor, env["ns"], 10, allowed_source_ids=[allowed])
    original = app.build.prepare_in

    def narrow_after_prepare(db, user, body):
        manifest = original(db, user, body)
        db.get(FundingAgreement, funding).allowed_source_ids = [replacement]
        db.flush()
        return manifest

    monkeypatch.setattr(app.build, "prepare_in", narrow_after_prepare)
    with pytest.raises(DomainError) as error:
        app.build.start(
            actor,
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=funding,
                source_id=allowed,
                question="Prepared source scope",
                topic_keys=["agent.tool_calling"],
                call_cap=10,
                max_units_per_project=1,
                idempotency_key="prepared-build",
            ),
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).calls_held == 0


@pytest.mark.asyncio
async def test_explicit_private_optional_content_requires_private_source_egress(env):
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        public = register(db, actor, env["owner"]["public_namespace_id"], "publication", {})
        private = register(db, actor, env["ns"], "facet_value", {"value": "private fixture"})
        roots = [public.id, private.id]
    funding = app.runtime.create_funding(
        actor, env["ns"], 10, data_classes=["learning_state", "public_source"]
    )
    run_id = app.runtime.create(actor, env["ns"], funding, "research", {}, 1, "optional-private")
    epoch = app.runtime.claim(run_id, "optional-worker")
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            run_id,
            epoch,
            "optional-worker",
            "optional",
            "planner",
            "Fixture",
            {},
            QueryPlan,
            data_classes=["learning_state", "public_source"],
            access_dependencies=roots,
        )
    assert error.value.code == "EGRESS_OR_ROLE_DENIED"
    assert not env["model"].calls
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).calls_consumed == 0
        assert not db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))


@pytest.mark.asyncio
async def test_private_selection_of_only_public_knowledge_remains_learning_state(env):
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        public = register(db, actor, env["owner"]["public_namespace_id"], "publication", {})
        selection = register(db, actor, env["ns"], "selection_manifest", {})
        depend(db, actor, selection.id, public.id, "selects_source")
        root = selection.id
    funding = app.runtime.create_funding(
        actor, env["ns"], 10, data_classes=["learning_state", "public_source"]
    )
    run_id = app.runtime.create(actor, env["ns"], funding, "research", {}, 1, "public-selection")
    epoch = app.runtime.claim(run_id, "selection-worker")
    output = await app.gateway.call(
        run_id,
        epoch,
        "selection-worker",
        "selection",
        "planner",
        "Fixture",
        {},
        QueryPlan,
        data_classes=["learning_state", "public_source"],
        access_dependencies=[root],
    )
    assert output.structured["queries"] and len(env["model"].calls) == 1
