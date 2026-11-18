"""At-least-once lexical projection keeps authority and exact material semantics."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import publish_fixture
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from careerforge.errors import DomainError
from careerforge.models import (
    FundingAgreement,
    KnowledgeDocument,
    ModelReceipt,
    Outbox,
    Publication,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal
from careerforge.util import fingerprint


def index_signature(env, publication):
    with env["app"].db.read() as db:
        doc = db.get(KnowledgeDocument, publication)
        return doc.search_text, doc.content_hash


def authority_counts(env):
    with env["app"].db.read() as db:
        funding = db.get(FundingAgreement, env["funding"])
        return (
            len(env["model"].calls),
            db.scalar(select(func.count()).select_from(ModelReceipt)),
            db.scalar(select(func.count()).select_from(Publication)),
            funding.calls_held,
            funding.calls_consumed,
        )


async def prepare_index(env, *, existing=False):
    source, publication, _ = await publish_fixture(env)
    expected = index_signature(env, publication)
    with env["app"].db.transaction() as db:
        if existing:
            doc = db.get(KnowledgeDocument, publication)
            doc.search_text, doc.content_hash = "stale projection", "0" * 64
        else:
            db.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.publication_id == publication)
            )
    return source, publication, expected


async def test_duplicate_index_and_changed_search_projection_are_exact_noop_or_refresh(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    _, publication, expected = await prepare_index(env)
    before = authority_counts(env)
    rowcounts = []

    def record(_conn, cursor, statement, *_args):
        if statement.startswith("INSERT INTO knowledge_search_documents"):
            assert "ON CONFLICT (publication_id) DO UPDATE" in statement
            rowcounts.append(cursor.rowcount)

    event.listen(app.db.engine, "after_cursor_execute", record)
    try:
        app.retrieval.index_lexical(actor, publication)
        app.retrieval.index_lexical(actor, publication)
        assert index_signature(env, publication) == expected
        with app.db.transaction() as db:
            db.get(KnowledgeDocument, publication).content_hash = "0" * 64
        app.retrieval.index_lexical(actor, publication)
        assert index_signature(env, publication) == expected
        # Taxonomy search terms can change without changing the immutable pack hash.
        from careerforge.services import retrieval

        original = retrieval.topic_terms
        monkeypatch.setattr(retrieval, "topic_terms", lambda *a: [*original(*a), "newsearchalias"])
        app.retrieval.index_lexical(actor, publication)
        text, content_hash = index_signature(env, publication)
        assert "newsearchalias" in text and content_hash == expected[1]
    finally:
        event.remove(app.db.engine, "after_cursor_execute", record)
    assert rowcounts == [1, 0, 1, 1]
    assert authority_counts(env) == before
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1
        assert fingerprint(app.knowledge.publication_pack(db, actor, publication)) == expected[1]


@pytest.mark.parametrize("existing", [False, True])
async def test_sqlite_two_independent_index_transactions_are_idempotent(env, existing):
    _, publication, expected = await prepare_index(env, existing=existing)
    before = authority_counts(env)
    barrier = Barrier(2)

    def index(_):
        barrier.wait(timeout=10)
        env["app"].retrieval.index_lexical(env["actor"], publication)

    # File-backed SQLite's BEGIN IMMEDIATE serializes writers. Do not place a
    # post-BEGIN barrier that would require a second writer to hold the same lock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(index, range(2)))
    assert index_signature(env, publication) == expected
    assert authority_counts(env) == before


@pytest.mark.parametrize(
    "revocation", ["publication", "source", "privacy", "generation", "foreign"]
)
async def test_replayed_projection_never_recreates_revoked_or_unauthorized_content(env, revocation):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await prepare_index(env)
    before = authority_counts(env)
    generation = None
    with app.db.transaction() as db:
        if revocation in {"publication", "source"}:
            invalidate(
                db, actor, publication if revocation == "publication" else source, "revoked", "test"
            )
        elif revocation == "privacy":
            db.get(PrivacyState, actor).blocked = True
        elif revocation == "generation":
            state = db.get(PrivacyState, actor)
            generation = state.generation
            state.generation += 1
        else:
            actor = create_principal(db, "foreign-index-reader")[0].id
    with pytest.raises(DomainError) as error:
        app.retrieval.index_lexical(actor, publication, expected_generation=generation)
    assert (
        error.value.code
        == {
            "publication": "EVIDENCE_UNAVAILABLE",
            "source": "EVIDENCE_UNAVAILABLE",
            "privacy": "PRIVACY_ERASURE_PENDING",
            "generation": "PRIVACY_GENERATION_CHANGED",
            "foreign": "NOT_FOUND",
        }[revocation]
    )
    with app.db.read() as db:
        assert db.get(KnowledgeDocument, publication) is None
    assert authority_counts(env) == before


@pytest.mark.parametrize("failure", ["write", "unsupported_backend"])
async def test_unexpected_index_write_failure_keeps_projection_pending(env, monkeypatch, failure):
    app = env["app"]
    _, publication, expected = await prepare_index(env)
    with app.db.transaction() as db:
        message = Outbox(kind="index", aggregate_id=publication, payload={})
        db.add(message)
        db.flush()
        identifier = message.id
    original = Session.execute

    def fail(session, statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "knowledge_search_documents"
        ):
            raise RuntimeError("injected write failure")
        return original(session, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(Session, "execute", fail)
        else:
            patch.setattr(app.db.engine.dialect, "name", "unsupported")
        with pytest.raises(RuntimeError):
            app.process_projections()
    with app.db.read() as db:
        assert not db.get(Outbox, identifier).delivered
        assert db.get(KnowledgeDocument, publication) is None
    app.process_projections()
    with app.db.read() as db:
        assert db.get(Outbox, identifier).delivered
    assert index_signature(env, publication) == expected
