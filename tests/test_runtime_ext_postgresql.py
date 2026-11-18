"""Opt-in G07/G12 concurrency on an explicitly disposable real PostgreSQL database."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from test_dependency_pagination import fanout_graph
from test_runtime_postgresql import pg_runtime as pg_runtime
from test_shared_builds import public_contract, subscriber

from careerforge.contracts_runtime_ext import SubscribeInput, UnsubscribeInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, Impact, Namespace, Principal
from careerforge.models_runtime_ext import DependencyInvalidationJob, SharedBuild, TaskSubscription
from careerforge.services.dependencies import (
    check_closure,
    depend,
    invalidate,
    propagate_batch,
    register,
)
from careerforge.services.taxonomy import seed

pytestmark = pytest.mark.production


@pytest.fixture
def pg_shared(pg_runtime):
    app, actor, ns, funding = pg_runtime
    with app.db.transaction() as db:
        db.get(Principal, actor).is_admin = True
        public = Namespace(kind="shared_public", owner_id=actor, name="shared-concurrency-test")
        db.add(public)
        db.flush()
        public_id = public.id
    seed(app.db, actor, public_id)
    return {
        "app": app,
        "actor": actor,
        "ns": ns,
        "funding": funding,
        "owner": {"public_namespace_id": public_id},
    }


def test_pg_shared_concurrent_different_keys_create_one_root_and_reservation(pg_shared):
    app, actor = pg_shared["app"], pg_shared["actor"]
    body = public_contract(pg_shared)
    barrier = threading.Barrier(6)

    def create(index):
        barrier.wait(timeout=5)
        return app.shared_builds.create(
            actor, body.model_copy(update={"idempotency_key": f"request-{index}"})
        )["run_id"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(create, range(6)))
    assert len(set(ids)) == 1
    with app.db.read() as db:
        assert db.get(FundingAgreement, body.funding_id).calls_held == body.call_cap
        assert (
            db.scalar(
                select(func.count())
                .select_from(SharedBuild)
                .where(SharedBuild.namespace_id == body.namespace_id)
            )
            == 1
        )


def test_pg_concurrent_subscriptions_and_final_detach_settle_once(pg_shared):
    app, actor = pg_shared["app"], pg_shared["actor"]
    body = public_contract(pg_shared, policy="cancel_after_last_detach")
    run_id = app.shared_builds.create(actor, body)["run_id"]
    users = [subscriber(pg_shared, f"sub-{index}")["actor"] for index in range(6)]
    barrier = threading.Barrier(6)

    def join(user):
        barrier.wait(timeout=5)
        return app.shared_builds.subscribe(user, run_id, SubscribeInput(idempotency_key="join"))

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(result["subscribed"] for result in pool.map(join, users))
    barrier = threading.Barrier(6)

    def leave(user):
        barrier.wait(timeout=5)
        return app.shared_builds.unsubscribe(
            user, run_id, UnsubscribeInput(idempotency_key="leave")
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert not any(result["subscribed"] for result in pool.map(leave, users))
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "cancelled"
        assert db.get(FundingAgreement, body.funding_id).calls_held == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskSubscription)
                .where(TaskSubscription.run_id == run_id)
            )
            == 6
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskSubscription)
                .where(TaskSubscription.run_id == run_id, TaskSubscription.active.is_(True))
            )
            == 0
        )


def test_pg_same_subscription_key_different_runs_conflicts_not_unique_violation(pg_shared):
    app, actor = pg_shared["app"], pg_shared["actor"]
    body = public_contract(pg_shared)
    one = app.shared_builds.create(actor, body)["run_id"]
    two = app.shared_builds.create(
        actor, body.model_copy(update={"call_cap": 11, "idempotency_key": "other"})
    )["run_id"]
    user = subscriber(pg_shared)["actor"]
    barrier = threading.Barrier(2)

    def join(run_id):
        barrier.wait(timeout=5)
        try:
            app.shared_builds.subscribe(user, run_id, SubscribeInput(idempotency_key="same"))
            return "joined"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(join, [one, two])) == ["IDEMPOTENCY_CONFLICT", "joined"]
    with app.db.read() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskSubscription)
                .where(TaskSubscription.subscriber_id == user)
            )
            == 1
        )


def test_pg_duplicate_propagators_serialize_durable_cursor_and_strong_impacts(pg_shared):
    app, actor = pg_shared["app"], pg_shared["actor"]
    source, answer = fanout_graph(pg_shared, 420)
    with app.db.transaction() as db:
        generation = invalidate(db, actor, source, "revoked", "pg concurrent invalidation").revision
    barrier = threading.Barrier(4)

    def propagate(_):
        barrier.wait(timeout=5)
        for _ in range(200):
            with app.db.transaction() as db:
                progress = propagate_batch(db, source, generation, batch_size=23)
            if progress.complete:
                return progress.job_id
        raise AssertionError("propagation did not converge")

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(propagate, range(4)))
    assert len(set(ids)) == 1
    with app.db.read() as db:
        job = db.get(DependencyInvalidationJob, ids[0])
        assert job.status == "completed" and job.edges_scanned == 840
        assert job.impacts_created == 421
        assert (
            db.scalar(select(func.count()).select_from(Impact).where(Impact.source_id == source))
            == 421
        )
        with pytest.raises(DomainError) as error:
            check_closure(db, actor, [answer])
        assert error.value.code == "EVIDENCE_UNAVAILABLE"


def test_pg_large_valid_closure_then_complete_paginated_propagation(pg_shared):
    app, actor = pg_shared["app"], pg_shared["actor"]
    source, answer = fanout_graph(pg_shared, 10050)
    with app.db.read() as db:
        assert check_closure(db, actor, [answer]).checked_objects == 10052
        assert len(db.identity_map) < 300
    with app.db.transaction() as db:
        generation = invalidate(db, actor, source, "revoked", "pg large graph").revision
    for _ in range(100):
        with app.db.transaction() as db:
            progress = propagate_batch(db, source, generation, batch_size=1000)
            assert progress.edges_scanned <= 1000
        if progress.complete:
            break
    assert progress.complete
    with app.db.read() as db:
        job = db.get(DependencyInvalidationJob, progress.job_id)
        assert job.edges_scanned == 20100 and job.impacts_created == 10051
        assert (
            db.scalar(select(func.count()).select_from(Impact).where(Impact.source_id == source))
            == 10051
        )


def test_pg_edge_waiting_for_revocation_cannot_commit_behind_persistent_cursor(pg_shared):
    app, actor, ns = pg_shared["app"], pg_shared["actor"], pg_shared["ns"]
    with app.db.transaction() as db:
        source = register(db, actor, ns, "facet_value", {})
        pub = register(db, actor, ns, "publication", {})
        block = register(db, actor, ns, "answer_block", {})
        depend(db, actor, pub.id, source.id, "uses_facet")
        source_id, pub_id, block_id = source.id, pub.id, block.id
    invalidated, consumer_loaded = threading.Event(), threading.Event()

    def revoke():
        with app.db.transaction() as db:
            generation = invalidate(db, actor, source_id, "revoked", "race").revision
            invalidated.set()
            assert consumer_loaded.wait(timeout=5)
        with app.db.transaction() as db:
            assert propagate_batch(db, source_id, generation).complete

    def append_edge():
        assert invalidated.wait(timeout=5)
        with app.db.transaction() as db:
            # Simulate a long-lived caller that preloaded the active-looking pub.
            from careerforge.models import ObjectRow

            assert db.get(ObjectRow, pub_id).state == "active"
            consumer_loaded.set()
            try:
                depend(db, actor, block_id, pub_id, "teaches_from")
                return "created"
            except DomainError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(revoke)
        assert pool.submit(append_edge).result(timeout=10) == "EVIDENCE_UNAVAILABLE"
        future.result(timeout=10)


@pytest.mark.parametrize("operation", ["plan", "replay"])
def test_pg_new_learning_run_does_not_hold_graph_lock_while_waiting_for_funding(
    pg_shared, monkeypatch, operation
):
    """The starting operation and an existing leased commit share actual funding."""
    import asyncio
    from dataclasses import replace

    from conftest import publish_fixture
    from test_evolution_replay_v16 import replay_data, semantic_candidate
    from test_learning_loop_v16 import plan_input

    from careerforge.contracts_learning_ext import ReplayInput
    from careerforge.services.runtime import leased

    app, actor = pg_shared["app"], pg_shared["actor"]
    # These are explicit synthetic fixture publications, not production inference.
    settings = replace(app.settings, allow_fixture_publication=True)
    app.settings = app.db.settings = app.gateway.settings = settings
    pg_shared.update(model=app.provider, repos=app.repositories)

    async def own_drain(limit=100):
        # The shared disposable DB also contains other tests' intentionally queued
        # runtime contracts; this fixture executes only its own principal's work.
        executions = 0
        while executions < limit:
            with app.db.read() as db:
                identifier = db.scalar(
                    select(AgentRun.id)
                    .where(AgentRun.principal_id == actor, AgentRun.status == "queued")
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .limit(1)
                )
            if identifier is None:
                break
            await app.execute(identifier)
            executions += 1
        return {"executions": executions}

    monkeypatch.setattr(app, "drain", own_drain)

    async def prepare():
        if operation == "plan":
            _, publication, _ = await publish_fixture(pg_shared, key="lock-source")
            value = plan_input(pg_shared, publication, key="lock-plan")
            return publication, lambda: app.learning.start_personalized_plan(actor, value)
        candidate_id, _, _ = await semantic_candidate(pg_shared)
        dataset, specification = await replay_data(pg_shared, name="lock-replay")
        value = ReplayInput(
            dataset_id=dataset["dataset_id"],
            funding_id=pg_shared["funding"],
            idempotency_key="lock-replay",
        )
        return specification.cases[0].publication_ids[0], lambda: app.evolution.start_replay(
            actor, candidate_id, value
        )

    publication, start = asyncio.run(prepare())
    existing = app.runtime.create(
        actor, pg_shared["ns"], pg_shared["funding"], "research", {}, 1, "holding-funding"
    )
    epoch = app.runtime.claim(existing, "lease-holder")
    assert epoch is not None
    funding_locked, create_reached = threading.Event(), threading.Event()
    original = app.runtime.create_in

    def traced_create(*args, **kwargs):
        create_reached.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(app.runtime, "create_in", traced_create)

    def commit_existing():
        with app.db.transaction() as db:
            leased(db, existing, epoch, "lease-holder")
            funding_locked.set()
            assert create_reached.wait(timeout=10)
            selection = register(db, actor, pg_shared["ns"], "selection_manifest", {})
            depend(db, actor, selection.id, publication, "selects_source")

    def create_new():
        assert funding_locked.wait(timeout=10)
        return start()

    with ThreadPoolExecutor(max_workers=2) as pool:
        committed = pool.submit(commit_existing)
        created = pool.submit(create_new)
        created_id = created.result(timeout=20)["run_id"]
        committed.result(timeout=20)
    # Later fixtures share this isolated database and their drain() is global.
    # Close only our own intentionally unexecuted roots, never another test's run.
    for identifier in [created_id, existing]:
        with app.db.read() as db:
            revision = db.get(AgentRun, identifier).revision
        app.runtime.cancel(actor, identifier, revision)


def test_pg_final_replay_waiter_rechecks_candidate_after_consent_state_commit(
    pg_shared, monkeypatch
):
    import asyncio
    from dataclasses import replace

    from test_evolution_replay_v16 import replay_data, semantic_candidate

    import careerforge.services.evolution_replay as replay_module
    from careerforge.contracts_learning_ext import ReplayInput
    from careerforge.db import locked
    from careerforge.models import EvolutionCandidate, EvolutionEvaluation

    app, actor = pg_shared["app"], pg_shared["actor"]
    settings = replace(app.settings, allow_fixture_publication=True)
    app.settings = app.db.settings = app.gateway.settings = settings
    pg_shared.update(model=app.provider, repos=app.repositories)

    async def own_drain(limit=100):
        executions = 0
        while executions < limit:
            with app.db.read() as db:
                identifier = db.scalar(
                    select(AgentRun.id)
                    .where(AgentRun.principal_id == actor, AgentRun.status == "queued")
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .limit(1)
                )
            if identifier is None:
                break
            await app.execute(identifier)
            executions += 1
        return {"executions": executions}

    monkeypatch.setattr(app, "drain", own_drain)

    async def prepare():
        candidate_id, _, _ = await semantic_candidate(pg_shared)
        dataset, _ = await replay_data(pg_shared, name="candidate-waiter")
        return candidate_id, app.evolution.start_replay(
            actor,
            candidate_id,
            ReplayInput(
                dataset_id=dataset["dataset_id"],
                funding_id=pg_shared["funding"],
                idempotency_key="candidate-waiter",
            ),
        )["run_id"]

    candidate_id, run_id = asyncio.run(prepare())
    waiting = threading.Event()
    original_locked = replay_module.locked

    def traced_locked(db, cls, identifier):
        if cls is EvolutionCandidate and identifier == candidate_id:
            waiting.set()
        return original_locked(db, cls, identifier)

    monkeypatch.setattr(replay_module, "locked", traced_locked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with app.db.transaction() as db:
            candidate = locked(db, EvolutionCandidate, candidate_id)
            candidate.status = "consent_revoked"
            db.flush()
            worker = pool.submit(asyncio.run, app.execute(run_id, "candidate-waiter"))
            reached = waiting.wait(timeout=15)
            if not reached:
                from careerforge.models import RunEvent

                with app.db.read() as inspection:
                    outcome = inspection.scalar(
                        select(RunEvent).where(
                            RunEvent.run_id == run_id, RunEvent.event_type == "run.finished"
                        )
                    )
                    status = inspection.get(AgentRun, run_id).status
                raise AssertionError(
                    f"worker missed final candidate row lock: done={worker.done()}, "
                    f"status={status}, terminal={outcome.payload if outcome else None}"
                )
            assert not worker.done()
        worker.result(timeout=15)
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, candidate_id).status == "consent_revoked"
        assert db.get(AgentRun, run_id).status == "failed"
        assert not db.scalar(
            select(EvolutionEvaluation).where(EvolutionEvaluation.candidate_id == candidate_id)
        )
        assert app.runtime.stage(run_id, "result") is None


def test_pg_funding_source_scope_uses_full_transitive_sql_closure(pg_shared):
    import asyncio

    from test_funding_source_scope import scoped_run, source

    from careerforge.contracts import QueryPlan
    from careerforge.models import ModelReceipt

    app, actor = pg_shared["app"], pg_shared["actor"]
    allowed, outside = source(pg_shared, "pg-allowed"), source(pg_shared, "pg-outside")
    funding, run = scoped_run(pg_shared, [allowed])
    with app.db.transaction() as db:
        evidence = register(db, actor, pg_shared["ns"], "evidence", {})
        claim = register(db, actor, pg_shared["ns"], "claim", {})
        depend(db, actor, evidence.id, outside, "grounded_in")
        depend(db, actor, claim.id, evidence.id, "requires_evidence")
        root = claim.id
    with pytest.raises(DomainError) as error:
        asyncio.run(
            app.gateway.call(
                *run,
                "pg-scope",
                "planner",
                "Fixture",
                {},
                QueryPlan,
                data_classes=["private_source"],
                access_dependencies=[root],
            )
        )
    assert error.value.code == "FUNDING_SOURCE_SCOPE"
    with app.db.read() as db:
        assert not db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))
        assert db.get(FundingAgreement, funding).calls_consumed == 0
