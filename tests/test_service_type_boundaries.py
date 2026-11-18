"""Missing projections and invalid run contracts stop before unapproved side effects."""

import pytest
from conftest import publish_fixture
from sqlalchemy import delete, func, select

from careerforge.contracts import QueryPlan, RetrievalTaskSpec, TaskType
from careerforge.errors import DomainError
from careerforge.models import AgentRun, KnowledgeDocument, ResearchNode
from careerforge.services import retrieval


@pytest.mark.parametrize("missing", [True, False])
def test_research_expansion_requires_parent_in_current_run(env, missing):
    app = env["app"]
    run_id = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 2, "node-owner"
    )
    epoch = app.runtime.claim(run_id, "worker")
    parent_id = "missing-node"
    if not missing:
        other_run = app.runtime.create(
            env["actor"], env["ns"], env["funding"], "research", {}, 2, "other-node-owner"
        )
        with app.db.transaction() as db:
            node = ResearchNode(
                run_id=other_run,
                parent_id=None,
                depth=0,
                question="Another tree",
                node_key="other-root",
                payload={},
            )
            db.add(node)
            db.flush()
            parent_id = node.id
    with app.db.read() as db:
        initial_count = db.scalar(select(func.count()).select_from(ResearchNode))
        initial_sequence = db.get(AgentRun, run_id).event_seq
    with pytest.raises(DomainError) as error:
        app.research._expand(run_id, epoch, "worker", parent_id, QueryPlan(), 5, 2, [])
    assert error.value.code == "RESEARCH_NODE"
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(ResearchNode)) == initial_count
        assert db.get(AgentRun, run_id).event_seq == initial_sequence
        if not missing:
            assert db.get(ResearchNode, parent_id).status == "pending"
    assert not env["model"].calls


def test_research_data_classes_reject_missing_run(env):
    with pytest.raises(DomainError) as error:
        env["app"].research._data_classes("missing-run")
    assert error.value.code == "NOT_FOUND"
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_missing_vector_document_does_not_send_paid_embedding(env, monkeypatch):
    app = env["app"]
    _, publication_id, _ = await publish_fixture(env, key="missing-vector-document")
    with app.db.transaction() as db:
        db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.publication_id == publication_id)
        )
    # Explicit fault injection: the projection vanishes after lexical indexing.
    monkeypatch.setattr(app.retrieval, "index_lexical", lambda *_, **__: None)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await app.retrieval.index_vector(env["actor"], publication_id, ("unused", 1, "worker"))
    assert error.value.code == "INDEX_UNAVAILABLE"
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["pgvector", "reranker"])
async def test_model_retrieval_plan_never_supplies_missing_run_authority(env, monkeypatch, channel):
    app = env["app"]
    for index in range(2):
        await publish_fixture(
            env, key=f"funding-scope-{index}", repository=f"fixtures/scoped-{index}"
        )
    choose = retrieval.choose_plan

    def unauthorized_plan(*args, **kwargs):
        plan = choose(*args, **kwargs)
        if channel == "pgvector":
            plan.allowed_channels.append("pgvector")
        else:
            plan.rerank = "enabled"
        return plan

    monkeypatch.setattr(retrieval, "choose_plan", unauthorized_plan)
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await app.retrieval.retrieve(
            env["actor"],
            RetrievalTaskSpec(
                turn_id="unfunded-retrieval",
                task_type=TaskType.ARCHITECTURE,
                user_question="Tool Calling",
                topic_keys=["agent.tool_calling"],
                focus_revision=1,
            ),
        )
    assert error.value.code == "RETRIEVAL_FUNDING"
    assert len(env["model"].calls) == before
