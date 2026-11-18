import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FILES

from careerforge.contracts import (
    ImportSourceInput,
    KnowledgeBuildInput,
    ModelOutput,
    RetrievalTaskSpec,
    TaskType,
    TurnInput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    ArtifactDependency,
    FactAtom,
    Impact,
    Publication,
    SelectionHead,
    TopicLink,
)
from careerforge.services.dependencies import check_closure, depend, invalidate, register
from careerforge.services.security import create_principal, set_grant


def spec(key="q", **kwargs):
    return RetrievalTaskSpec(
        turn_id=key,
        task_type=TaskType.IMPLEMENTATION,
        user_question="工具调用 Tool Calling 返回值",
        focus_revision=1,
        topic_keys=["agent.tool_calling"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_private_namespace_no_admin_override_and_hot_cache_revoke(env):
    app, actor = env["app"], env["actor"]
    source, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        b, bns, _ = create_principal(db, "other")
        bid = b.id
    with app.db.read() as db:
        with pytest.raises(DomainError):
            app.knowledge.publication_pack(db, bid, pub)
    a = await app.retrieval.retrieve(actor, spec("one"))
    b = await app.retrieval.retrieve(actor, spec("two"))
    assert a["knowledge_units"] and b["knowledge_units"]
    with app.db.transaction() as db:
        invalidate(db, actor, pub, "revoked", "confirmed test error")
    after = await app.retrieval.retrieve(actor, spec("three"))
    assert after["knowledge_units"] == []
    # No dependency worker needed to reject the previously frozen manifest.
    with app.db.read() as db:
        with pytest.raises(DomainError):
            check_closure(db, actor, [a["retrieval_manifest_id"]])


@pytest.mark.asyncio
async def test_shared_source_private_consumer_and_impact_privacy(env):
    app = env["app"]
    source, pub, _ = await publish_fixture(env, namespace=env["owner"]["public_namespace_id"])
    with app.db.transaction() as db:
        b, bns, _ = create_principal(db, "reader")
        bid = b.id
        bnsid = bns.id
    conv = app.conversations.create(bid)
    turn = app.conversations.submit(
        bid,
        conv,
        TurnInput(question="Tool Calling实现", expected_revision=1, idempotency_key="shared"),
    )
    await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        links = db.scalars(
            select(ArtifactDependency).where(
                ArtifactDependency.source_id == pub,
                ArtifactDependency.dependent_type == "answer_block",
            )
        ).all()
        assert links and links[0].edge_namespace_id == bnsid
        assert links[0].source_namespace_id == env["owner"]["public_namespace_id"]
    with app.db.transaction() as db:
        invalidate(db, env["actor"], pub, "revoked", "test")
    assert any(
        x["type"] == "answer.evidence_invalidated"
        for x in app.answers.events(bid, turn["turn_id"])["events"]
    )
    app.process_projections()
    with app.db.read() as db:
        assert db.scalar(select(Impact).where(Impact.namespace_id == bnsid))
    with app.db.transaction() as db:
        private = register(db, bid, bnsid, "publication", {})
        public = register(db, env["actor"], env["owner"]["public_namespace_id"], "answer_block", {})
        with pytest.raises(DomainError):
            depend(db, env["actor"], public.id, private.id, "teaches_from")


@pytest.mark.asyncio
async def test_permission_revocation_blocks_cached_knowledge(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        b, bns, _ = create_principal(db, "guest")
        bid = b.id
        set_grant(db, env["actor"], env["ns"], bid, "reader", True)
    assert (await app.retrieval.retrieve(bid, spec("before")))["knowledge_units"]
    with app.db.transaction() as db:
        set_grant(db, env["actor"], env["ns"], bid, "reader", False)
    assert not (await app.retrieval.retrieve(bid, spec("after")))["knowledge_units"]


@pytest.mark.asyncio
async def test_concept_default_head_does_not_fork_by_sourceset(env):
    app = env["app"]
    pubs = []
    for n in [1, 2]:
        src = app.sources.import_source(
            env["actor"],
            ImportSourceInput(
                namespace_id=env["ns"],
                origin_kind="concept",
                repository_key="concept:return-value",
                license_id="MIT",
                files={**FILES, "README.md": f"edition {n}"},
            ),
        )
        run = app.build.start(
            env["actor"],
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=env["funding"],
                question="返回值",
                source_id=src,
                topic_keys=["agent.tool_calling"],
                call_cap=10,
                max_units_per_project=1,
                idempotency_key="concept" + str(n),
            ),
        )
        await app.drain()
        pubs.append(app.runtime.stage(run, "result")["publication_ids"][0])
    with app.db.read() as db:
        heads = db.scalars(select(SelectionHead)).all()
        assert len(heads) == 1
        assert heads[0].scope_key == "concept"
        assert heads[0].publication_id == pubs[0]
    app.knowledge.select_head(env["actor"], env["ns"], pubs[1], 1)
    with app.db.read() as db:
        assert [p.id for p in app.knowledge.selected_publications(db, env["actor"])] == [pubs[1]]
        assert app.knowledge.publication_pack(db, env["actor"], pubs[0])  # Historical still valid.
    with pytest.raises(DomainError):
        app.knowledge.select_head(env["actor"], env["ns"], pubs[0], 1)


@pytest.mark.asyncio
async def test_evidence_fact_tamper_rejected(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        p = db.get(Publication, pub)
        f = db.scalar(select(FactAtom).where(FactAtom.unit_version_id == p.unit_version_id))
        f.approved_text = "伪造安全保证"
    with app.db.read() as db:
        with pytest.raises(DomainError):
            app.knowledge.publication_pack(db, env["actor"], pub)


@pytest.mark.asyncio
async def test_topic_link_revoke_not_whole_publication(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        p = db.get(Publication, pub)
        link = db.scalar(select(TopicLink).where(TopicLink.unit_version_id == p.unit_version_id))
        invalidate(db, env["actor"], link.id, "revoked", "wrong classification")
    with app.db.read() as db:
        app.knowledge.publication_pack(db, env["actor"], pub)
    assert app.retrieval.directory(env["actor"], "agent.tool_calling")["total_projects"] == 0


@pytest.mark.asyncio
async def test_full_directory_pagination_and_authority_cursor(env):
    app = env["app"]
    for i in range(3):
        await publish_fixture(env, key=f"p{i}", repository=f"fixtures/proj{i}")
    one = app.retrieval.directory(env["actor"], "agent.tool_calling", limit=1)
    assert one["total_projects"] == 3 and one["next_cursor"]
    two = app.retrieval.directory(env["actor"], "agent.tool_calling", one["next_cursor"], 1)
    assert one["projects"][0]["source_id"] != two["projects"][0]["source_id"]
    with app.db.transaction() as db:
        invalidate(db, env["actor"], one["projects"][0]["publication_ids"][0], "revoked", "test")
    with pytest.raises(DomainError):
        app.retrieval.directory(env["actor"], "agent.tool_calling", one["next_cursor"], 1)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/abs.py",
        ".env",
        "src/../../x",
        "src\\x.py",
        ".git/config",
        "src/.env.local",
    ],
)
def test_source_path_rejection(env, path):
    with pytest.raises(DomainError):
        env["app"].sources.import_source(
            env["actor"],
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key="x/y",
                commit_sha="a" * 40,
                license_id="MIT",
                files={path: "hello"},
            ),
        )


@pytest.mark.asyncio
async def test_review_failure_cannot_publish(env):
    app = env["app"]
    env["model"].overrides["coverage"] = lambda r: ModelOutput(
        structured={
            "all_facts_bound": False,
            "conditions_preserved": False,
            "uncovered_elements": ["hidden fact"],
            "unsupported_titles": [],
            "rationale_codes": [],
        },
        synthetic=True,
    )
    src = app.sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/a",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
        ),
    )
    app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="Tool Calling",
            source_id=src,
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="bad-review",
        ),
    )
    await app.drain()
    with app.db.read() as db:
        assert not db.scalar(select(Publication))
