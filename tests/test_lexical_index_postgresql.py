"""Opt-in real PostgreSQL upsert race, only in explicit disposable validation DB."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from test_lexical_index_idempotence import authority_counts, index_signature, prepare_index
from test_runtime_postgresql import pg_runtime  # noqa: F401

from careerforge.models import KnowledgeDocument, Namespace, Topic
from careerforge.services.taxonomy import seed

pytestmark = pytest.mark.production


@pytest.mark.parametrize("existing", [False, True])
async def test_pg_two_index_upserts_commit_one_exact_projection(pg_runtime, existing):  # noqa: F811
    app, actor, namespace, funding = pg_runtime
    app.settings = replace(app.settings, allow_fixture_publication=True)
    app.db.settings = app.settings
    with app.db.read() as db:
        public = db.scalar(select(Namespace).where(Namespace.kind == "shared_public"))
        seeded = db.scalar(select(Topic.id).limit(1))
    if public is None:
        app.bootstrap("index-fixture-" + uuid4().hex)
    elif not seeded:
        seed(app.db, public.owner_id, public.id)
    env = {"app": app, "actor": actor, "ns": namespace, "funding": funding, "model": app.provider}
    _, publication, expected = await prepare_index(env, existing=existing)
    before = authority_counts(env)
    barrier = Barrier(2)
    connections, rowcounts = [], []

    def synchronize(connection, _cursor, statement, *_args):
        if statement.startswith("INSERT INTO knowledge_search_documents"):
            assert "ON CONFLICT (publication_id) DO UPDATE" in statement
            connections.append(id(connection.connection.driver_connection))
            barrier.wait(timeout=10)

    def record(_connection, cursor, statement, *_args):
        if statement.startswith("INSERT INTO knowledge_search_documents"):
            rowcounts.append(cursor.rowcount)

    event.listen(app.db.engine, "before_cursor_execute", synchronize)
    event.listen(app.db.engine, "after_cursor_execute", record)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: app.retrieval.index_lexical(actor, publication), range(2)))
    finally:
        event.remove(app.db.engine, "before_cursor_execute", synchronize)
        event.remove(app.db.engine, "after_cursor_execute", record)
    assert len(set(connections)) == 2
    assert sorted(rowcounts) == [0, 1]
    assert index_signature(env, publication) == expected
    assert authority_counts(env) == before
    with app.db.read() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.publication_id == publication)
            )
            == 1
        )
