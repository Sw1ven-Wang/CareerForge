import asyncio

import pytest
from conftest import publish_fixture

from careerforge.adapters.cache import MemoryCache
from careerforge.contracts import ModelOutput, RetrievalTaskSpec, TaskType, TurnInput
from careerforge.services.retrieval import choose_plan, rrf


@pytest.mark.parametrize(
    "task,channel,excluded",
    [
        (TaskType.LOCATE, "ast_symbol", "pgvector"),
        (TaskType.VERIFY, "topic_relation", "pgvector"),
        (TaskType.ARCHITECTURE, "pgvector", "ast_symbol"),
        (TaskType.CONCEPT, "pgvector", "ast_symbol"),
    ],
)
def test_task_aware_channels(task, channel, excluded):
    spec = RetrievalTaskSpec(turn_id="x", task_type=task, user_question="test", focus_revision=1)
    plan = choose_plan(spec, vector_available=True, rerank_available=True)
    assert channel in plan.allowed_channels and excluded not in plan.allowed_channels
    if task == TaskType.LOCATE:
        assert plan.rerank == "disabled"


def test_rrf_uses_configured_weights():
    assert rrf([["A", "B"], ["B", "A"]], [2, 0.5])[0] == "A"
    assert rrf([["A", "B"], ["B", "A"]], [0.5, 2])[0] == "B"


@pytest.mark.asyncio
async def test_singleflight_and_mutation_isolation():
    cache = MemoryCache()
    counter = 0

    async def loader():
        nonlocal counter
        counter += 1
        await asyncio.sleep(0.02)
        return {"value": [1]}

    rows = await asyncio.gather(*(cache.singleflight("base", "same", loader) for _ in range(30)))
    assert counter == 1 and all(r == {"value": [1]} for r in rows)
    rows[0]["value"].append(2)
    assert (await cache.get("base", "same")) == {"value": [1]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "forged_fact",
        "wrong_project",
        "condition_drift",
        "ordinary_paraphrase",
        "high_risk",
        "hidden_in_gap",
    ],
)
async def test_answer_guard_preserves_evidence_boundary(env, kind):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)

    def malicious(request):
        u = request["context"]["evidence_pack"]["knowledge_units"][0]
        f = u["facts"][0]
        b = {
            "kind": "repository_fact",
            "text": f["text"],
            "fact_atom_ids": [f["fact_atom_id"]],
            "publication_ids": [pub],
            "conditions": f["conditions"],
            "transformation": "exact",
            "style": "paragraph",
        }
        if kind == "forged_fact":
            b["fact_atom_ids"] = ["invented"]
        if kind == "wrong_project":
            b["publication_ids"] = ["other-project"]
        if kind == "condition_drift":
            b["conditions"] = ["这是新增条件"]
        if kind == "ordinary_paraphrase":
            b.update(text="函数返回随机数。", transformation="paraphrase")
        if kind == "high_risk":
            b.update(text="系统保证安全，永远不会失败。", transformation="paraphrase")
        if kind == "hidden_in_gap":
            b.update(kind="evidence_gap", text="这个项目保证跨进程恢复。")
        return ModelOutput(structured={"blocks": [b]}, synthetic=True)

    env["model"].overrides["tutor"] = malicious
    conv = app.conversations.create(env["actor"])
    t = app.conversations.submit(
        env["actor"],
        conv,
        TurnInput(
            question="讲解Tool Calling",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="guard",
        ),
    )
    await app.drain()
    packet = app.answers.events(env["actor"], t["turn_id"])
    assert packet["status"] == "completed"
    blocks = [
        e["payload"]["content"] for e in packet["events"] if e["type"] == "answer.block.validated"
    ]
    forbidden = ["随机数", "保证安全", "永远不会", "这个项目保证"]
    assert not any(term in b["text"] for b in blocks for term in forbidden)
    assert all(
        b.get("checked") in {"exact_fallback", "server_template", "source_anchored_gap"}
        for b in blocks
    )


@pytest.mark.asyncio
async def test_symbol_lookup_skips_model_and_preserves_followup_focus(env):
    app = env["app"]
    source, _, _ = await publish_fixture(env)
    conv = app.conversations.create(env["actor"])
    before = len(env["model"].calls)
    for idx, q in enumerate(["定位 仓库 fixtures/agent 的 echo 函数", "刚才这个函数在哪"]):
        t = app.conversations.submit(
            env["actor"],
            conv,
            TurnInput(question=q, expected_revision=idx + 1, idempotency_key="locate" + str(idx)),
        )
        await app.conversations.process(t["turn_id"])
        blocks = [
            e["payload"]["content"]
            for e in app.answers.events(env["actor"], t["turn_id"])["events"]
            if e["type"] == "answer.block.validated"
        ]
        assert any(b.get("navigation", {}).get("symbol") == "echo" for b in blocks), blocks
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_cross_project_results_are_grouped(env):
    app = env["app"]
    sources = []
    for i in range(3):
        s, p, _ = await publish_fixture(env, key=str(i), repository="fixtures/agent" + str(i))
        sources.append(s)
    spec = RetrievalTaskSpec(
        turn_id="compare",
        task_type=TaskType.COMPARE,
        user_question="对比Tool Calling",
        topic_keys=["agent.tool_calling"],
        focus_revision=1,
        max_project_samples=2,
    )
    result = await app.retrieval.retrieve(env["actor"], spec)
    assert len(set(u["source_id"] for u in result["knowledge_units"])) == 2
    assert app.retrieval.directory(env["actor"], "agent.tool_calling")["total_projects"] == 3
