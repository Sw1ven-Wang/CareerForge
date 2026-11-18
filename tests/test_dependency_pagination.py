"""G12: database-recursive authority barrier and resumable bounded reverse traversal."""

import pytest
from sqlalchemy import func, insert, select

from careerforge.application import Application
from careerforge.errors import DomainError
from careerforge.models import ArtifactDependency, Impact, ObjectRow, Outbox
from careerforge.models_privacy_ext import PrivacyState
from careerforge.models_runtime_ext import DependencyFrontier, DependencyInvalidationJob
from careerforge.services.dependencies import (
    check_closure,
    depend,
    invalidate,
    propagate_batch,
    register,
)
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint, uid


def fanout_graph(env, count):
    """Insert already-type-checked test topology in bounded fixture batches."""
    app, actor, ns = env["app"], env["actor"], env["ns"]
    with app.db.transaction() as db:
        source = register(db, actor, ns, "facet_value", {})
        answer = register(db, actor, ns, "answer_block", {})
        for offset in range(0, count, 256):
            identifiers = [uid() for _ in range(min(256, count - offset))]
            db.execute(
                insert(ObjectRow),
                [
                    {
                        "id": identifier,
                        "namespace_id": ns,
                        "kind": "publication",
                        "payload": {},
                        "content_hash": fingerprint({}),
                    }
                    for identifier in identifiers
                ],
            )
            edges = []
            for identifier in identifiers:
                edges.extend(
                    [
                        {
                            "dependent_id": identifier,
                            "dependent_type": "publication",
                            "source_id": source.id,
                            "source_type": "facet_value",
                            "kind": "uses_facet",
                        },
                        {
                            "dependent_id": answer.id,
                            "dependent_type": "answer_block",
                            "source_id": identifier,
                            "source_type": "publication",
                            "kind": "teaches_from",
                        },
                    ]
                )
            db.execute(
                insert(ArtifactDependency),
                [
                    {
                        **edge,
                        "id": uid(),
                        "edge_namespace_id": ns,
                        "dependent_namespace_id": ns,
                        "source_namespace_id": ns,
                        "enforcement": "blocking",
                        "authorization_ref": "actor:" + actor,
                        "authorization_revision": 1,
                    }
                    for edge in edges
                ],
            )
        return source.id, answer.id


def test_valid_closure_larger_than_ten_thousand_streams_without_graph_size_rejection(env):
    source, answer = fanout_graph(env, 10050)
    with env["app"].db.read() as db:
        summary = check_closure(db, env["actor"], [answer])
        assert summary.checked_objects == 10052
        assert len(summary.closure_hash) == 64
        assert summary.root_revisions == {answer: 1}
        # ORM's streaming weak identity map must not retain the transitive graph.
        assert len(db.identity_map) < 300
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], source, "revoked", "before async propagation")
    with env["app"].db.read() as db:
        with pytest.raises(DomainError) as error:
            check_closure(db, env["actor"], [answer])
        assert error.value.code == "EVIDENCE_UNAVAILABLE"
        assert db.scalar(select(func.count()).select_from(Impact)) == 0


def test_large_graph_fully_propagates_in_bounded_persistent_batches(env):
    app, actor = env["app"], env["actor"]
    source, answer = fanout_graph(env, 10050)
    with app.db.transaction() as db:
        generation = invalidate(db, actor, source, "revoked", "large graph").revision
    batches = 0
    for _ in range(100):
        with app.db.transaction() as db:
            progress = propagate_batch(db, source, generation, batch_size=1000)
            assert progress.edges_scanned <= 1000
        batches += 1
        if progress.complete:
            break
    assert progress.complete and batches > 10
    with app.db.read() as db:
        job = db.get(DependencyInvalidationJob, progress.job_id)
        assert job.edges_scanned == 20100 and job.impacts_created == 10051
        assert db.scalar(select(func.count()).select_from(Impact)) == 10051
        assert (
            db.scalar(select(Impact).where(Impact.consumer_id == answer)).enforcement == "blocking"
        )


def test_historical_invalidation_completes_after_suspension_was_upheld(env):
    app, actor = env["app"], env["actor"]
    source, answer = fanout_graph(env, 4)
    with app.db.transaction() as db:
        obj = invalidate(db, actor, source, "suspended", "historical")
        generation = obj.revision
    with app.db.transaction() as db:
        # This models lifecycle.resolve_dispute's independently-reviewed uphold.
        obj = db.get(ObjectRow, source)
        obj.state, obj.revision = "active", obj.revision + 1
    app.process_projections()
    with app.db.read() as db:
        assert db.scalar(select(Outbox).where(Outbox.aggregate_id == source)).delivered
        assert check_closure(db, actor, [answer]).checked_objects == 6
        assert db.scalar(select(DependencyInvalidationJob)).generation == generation


def test_cursor_commits_resume_after_worker_replacement_and_rollback(env):
    app, actor = env["app"], env["actor"]
    source, answer = fanout_graph(env, 310)
    with app.db.transaction() as db:
        generation = invalidate(db, actor, source, "suspended", "paged").revision
        progress = propagate_batch(db, source, generation, batch_size=17)
        assert not progress.complete and 0 < progress.edges_scanned <= 17
        job_id = progress.job_id
    with app.db.read() as db:
        baseline = db.get(DependencyInvalidationJob, job_id).edges_scanned
        old_impacts = db.scalar(select(func.count()).select_from(Impact))
    with pytest.raises(RuntimeError):
        with app.db.transaction() as db:
            propagate_batch(db, source, generation, batch_size=31)
            raise RuntimeError("simulated worker crash before commit")
    with app.db.read() as db:
        assert db.get(DependencyInvalidationJob, job_id).edges_scanned == baseline
        assert db.scalar(select(func.count()).select_from(Impact)) == old_impacts
    replacement = Application(app.settings, env["model"], env["repos"])
    try:
        replacement.process_projections()
        with replacement.db.read() as db:
            message = db.scalar(select(Outbox).where(Outbox.aggregate_id == source))
            assert not message.delivered
        # There is no fixed overall graph-size cap; each invocation is bounded.
        for _ in range(100):
            replacement.process_projections()
            with replacement.db.read() as db:
                job = db.get(DependencyInvalidationJob, job_id)
                if job.status == "completed":
                    break
        else:
            pytest.fail("bounded batches did not converge")
        with replacement.db.transaction() as db:
            replay = propagate_batch(db, source, generation, batch_size=13)
            assert replay.complete and replay.edges_scanned == replay.new_impacts == 0
            assert db.scalar(select(func.count()).select_from(Impact)) == 311
            assert db.get(DependencyInvalidationJob, job_id).edges_scanned == 620
            assert db.scalar(select(func.count()).select_from(DependencyInvalidationJob)) == 1
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(DependencyFrontier)
                    .where(DependencyFrontier.done.is_(False))
                )
                == 0
            )
            assert db.scalar(select(Outbox).where(Outbox.aggregate_id == source)).delivered
            assert (
                db.scalar(select(Impact).where(Impact.consumer_id == answer)).enforcement
                == "blocking"
            )
    finally:
        replacement.db.engine.dispose()


def test_strongest_enforcement_across_batches_and_provenance_does_not_expand(env):
    app, actor, ns = env["app"], env["actor"], env["ns"]
    with app.db.transaction() as db:
        source = register(db, actor, ns, "facet_value", {})
        pub = register(db, actor, ns, "publication", {})
        block = register(db, actor, ns, "answer_block", {})
        exercise = register(db, actor, ns, "exercise", {})
        a = depend(db, actor, pub.id, source.id, "uses_facet", "impact_only")
        b = depend(db, actor, pub.id, source.id, "uses_facet", "blocking")
        # Explicit IDs ensure the weaker path is reached first.
        a.id, b.id = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
        depend(db, actor, block.id, pub.id, "teaches_from", "blocking")
        depend(db, actor, exercise.id, pub.id, "teaches_from", "impact_only")
        generation = invalidate(db, actor, source.id, "revoked", "mixed").revision
        progress = propagate_batch(db, source.id, generation, batch_size=1)
        assert not progress.complete
        assert (
            db.scalar(select(Impact).where(Impact.consumer_id == pub.id)).enforcement
            == "impact_only"
        )
        root_id, pub_id, block_id, exercise_id = source.id, pub.id, block.id, exercise.id
    for _ in range(12):
        with app.db.transaction() as db:
            progress = propagate_batch(db, root_id, generation, batch_size=1)
        if progress.complete:
            break
    assert progress.complete
    with app.db.read() as db:
        impacts = {row.consumer_id: row.enforcement for row in db.scalars(select(Impact))}
        assert impacts == {pub_id: "blocking", block_id: "blocking", exercise_id: "impact_only"}
        assert not db.scalar(
            select(DependencyFrontier).where(DependencyFrontier.node_id == exercise_id)
        )


def test_new_edge_cannot_appear_behind_cursor_through_revoked_blocking_ancestor(env):
    app, actor, ns = env["app"], env["actor"], env["ns"]
    with app.db.transaction() as db:
        source = register(db, actor, ns, "facet_value", {})
        pub = register(db, actor, ns, "publication", {})
        depend(db, actor, pub.id, source.id, "uses_facet")
        generation = invalidate(db, actor, source.id, "revoked", "late edge").revision
        assert propagate_batch(db, source.id, generation).complete
        block = register(db, actor, ns, "answer_block", {})
        with pytest.raises(DomainError) as error:
            depend(db, actor, block.id, pub.id, "teaches_from")
        assert error.value.code == "EVIDENCE_UNAVAILABLE"


def test_transitive_private_owner_erasure_remains_fail_closed(env):
    app, actor, ns = env["app"], env["actor"], env["ns"]
    with app.db.transaction() as db:
        other, other_ns, _token = create_principal(db, "other-owner")
        set_grant(db, other.id, other_ns.id, actor, "reader", True)
        source = register(db, other.id, other_ns.id, "facet_value", {})
        pub = register(db, actor, ns, "publication", {})
        depend(db, actor, pub.id, source.id, "uses_facet")
        source_owner, pub_id = other.id, pub.id
    with app.db.transaction() as db:
        db.get(PrivacyState, source_owner).blocked = True
    with app.db.read() as db:
        with pytest.raises(DomainError) as error:
            check_closure(db, actor, [pub_id])
        assert error.value.code == "PRIVACY_ERASURE_PENDING"


@pytest.mark.parametrize("batch_size", [0, -1, 1001])
def test_propagation_rejects_unbounded_or_empty_batch(env, batch_size):
    with env["app"].db.transaction() as db:
        with pytest.raises(DomainError) as error:
            propagate_batch(db, "unused", 1, batch_size=batch_size)
        assert error.value.code == "GRAPH_BATCH"


@pytest.mark.parametrize(
    "kind",
    [
        "source_set",
        "evidence",
        "claim",
        "fact_atom",
        "render_element",
        "knowledge_unit_version",
        "review_decision",
        "topic_link",
        "topic_version",
        "facet_value",
        "facet_schema",
        "publication",
    ],
)
def test_private_knowledge_content_sets_bounded_egress_category(env, kind):
    with env["app"].db.transaction() as db:
        obj = register(db, env["actor"], env["ns"], kind, {"fixture": True})
        summary = check_closure(db, env["actor"], [obj.id])
        assert summary.private_source_content and not summary.public_only


@pytest.mark.parametrize(
    "kind",
    [
        "selection_manifest",
        "selection_head",
        "plan_module",
        "exercise",
        "answer_block",
        "comparison",
    ],
)
def test_private_learning_containers_do_not_invent_private_source_category(env, kind):
    with env["app"].db.transaction() as db:
        obj = register(db, env["actor"], env["ns"], kind, {"fixture": True})
        summary = check_closure(db, env["actor"], [obj.id])
        assert not summary.private_source_content and not summary.public_only
