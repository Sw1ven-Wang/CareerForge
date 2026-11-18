import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import KnowledgeBuildInput, TurnInput
from careerforge.models import AgentRun, FundingAgreement, SourceRead


@pytest.mark.asyncio
async def test_source_build_review_publish_and_teach(env):
    app, actor = env["app"], env["actor"]
    source, pub, run = await publish_fixture(env)
    with app.db.read() as db:
        r = db.get(AgentRun, run)
        # Synthetic facts remain publishable; this fixture does not claim a
        # complete original-task/structure assessment in the new contract.
        assert r.status == "partially_succeeded"
        # The new root pays for one additional independent task-sufficiency
        # assessment instead of treating factual publication as task completion.
        assert r.calls_consumed == 6
        roles = {x.role for x in db.scalars(select(SourceRead))}
        assert roles == {"analysis", "reviewer"}
        funding = db.get(FundingAgreement, env["funding"])
        assert funding.calls_held == 0
        assert funding.calls_consumed == 6
    conv = app.conversations.create(actor)
    answer = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="学习Tool Calling的实现",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="turn1",
        ),
    )
    await app.drain()
    packet = app.answers.events(actor, answer["turn_id"])
    assert packet["status"] == "completed", packet
    blocks = [
        e["payload"]["content"] for e in packet["events"] if e["type"] == "answer.block.validated"
    ]
    assert any(pub in b.get("publication_ids", []) for b in blocks), blocks


@pytest.mark.asyncio
async def test_jd_tree_whole_path(env):
    app = env["app"]
    r = app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="寻找 Tool Calling 工具框架",
            jd_text="熟悉 Tool Calling 工具调用。",
            topic_keys=["agent.tool_calling"],
            call_cap=20,
            # Explicit wall-clock capacity for the original full-tree branch fixture.
            duration_minutes=40,
            max_projects=1,
            max_units_per_project=1,
            idempotency_key="jd",
        ),
    )
    await app.drain()
    with app.db.read() as db:
        assert db.get(AgentRun, r).status == "partially_succeeded"
    research = app.runtime.stage(r, "research.result")
    assert len(research["tree"]) >= 4
    assert app.runtime.stage(r, "result")["publication_ids"]


@pytest.mark.asyncio
async def test_vector_path_and_direct_evidence_mode(env):
    env["model"].supports_embeddings = True
    env["model"].supports_reranking = True
    source, pub, _ = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    t = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="什么是Tool Calling",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="vector",
        ),
    )
    await app.drain()
    out = app.answers.events(actor, t["turn_id"])
    assert out["status"] == "completed", out
    trace = app.runtime.stage(t["run_id"], "retrieval")
    assert "pgvector" in trace["plan"]["allowed_channels"]
    assert pub in [u["publication_id"] for u in trace["knowledge_units"]]
