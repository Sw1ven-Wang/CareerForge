import asyncio
import threading

import pytest
from conftest import publish_fixture
from test_desk_learning_flow import configured, send
from test_local_web import desk
from test_session_plans_v166 import planner

from careerforge.contracts import ModelOutput
from careerforge.learning_intent import fuse, local_lanes


def classifier(env, intent, secondary=(), confidence=0.98):
    original = env["model"].complete
    seen = []

    async def complete(request):
        if "secondary" in (request.get("schema") or {}).get("properties", {}):
            seen.append(request["context"])
            return ModelOutput(
                structured={
                    "intent": intent,
                    "confidence": confidence,
                    "secondary": list(secondary),
                    "reasoning": "结合历史识别当前意图",
                },
                synthetic=True,
            )
        return await original(request)

    env["model"].complete = complete
    return seen


@pytest.mark.parametrize(
    "confidence,secondary,expected",
    [
        (0.98, ["practice"], ["reexplain", "practice"]),
        (0.3, [], ["unknown"]),
        (0.98, ["advance"], ["unknown"]),
        (0.98, [], ["reexplain"]),
    ],
)
def test_fusion_uncertainty_and_bounded_compound(confidence, secondary, expected):
    a, b = local_lanes("刚才那个我还不懂，换个方式讲，然后给我一道题")
    result = fuse(
        {
            "intent": "reexplain",
            "confidence": confidence,
            "secondary": secondary,
            "reasoning": "test",
        },
        a,
        b,
    )
    assert result["intents"] == expected
    assert result["sources"]["similarity"]["kind"] == "local_character_hash"


@pytest.mark.asyncio
async def test_semantic_reexplain_then_real_practice(env):
    _, pid, _ = await publish_fixture(env)
    planner(env)
    configured(env)
    async with desk(env) as client:
        job, d = await send(client, "使用材料生成计划", publication_ids=[pid])
        cid = job["conversation_id"]
        version = d["flow"]["plan"]["versions"][-1]["version_id"]
        await send(client, "确认学习计划", cid, confirm_version=version)
        seen = classifier(env, "reexplain", ["practice"])
        job, d = await send(client, "刚才那个我还不懂，换个方式讲，然后给我一道题", cid)
        assert d["flow"]["activity"] and d["flow"]["attempt"]
        action = d["actions"][-1]
        assert action["routing"]["intents"] == ["reexplain", "practice"]
        assert [s["status"] for s in action["steps"]] == ["completed", "completed"]
        assert len(d["turns"]) == 2
        assert seen[0]["teaching_history"]
        assert seen[0]["status_snapshot"]["workflow_id"]
        assert seen[0]["plan"]["active_version_id"] == version
        assert "当前模块的已审核源码" in d["turns"][-1]["question"]
        assert "给我一道题" not in d["turns"][-1]["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["confirm_plan", "submit_answer", "advance", "unknown"])
async def test_inferred_consent_never_executes_mutations(env, intent):
    configured(env)
    classifier(env, intent)
    async with desk(env) as client:
        _, d = await send(client, "那就按你刚才说的办吧")
        assert not d["turns"]
        assert not d["flow"].get("plan")
        assert not d["flow"].get("attempt")
        assert d["actions"][-1]["reply"]
        assert not d["actions"][-1].get("steps")


@pytest.mark.asyncio
async def test_semantic_compound_stops_without_accepted_plan(env):
    configured(env)
    classifier(env, "reexplain", ["practice"])
    async with desk(env) as client:
        _, d = await send(client, "刚才那个我没懂，换个方式讲，然后出一道题")
        assert not d["flow"].get("attempt")
        assert d["actions"][-1]["steps"][-1]["status"] in {"blocked", "completed"}


@pytest.mark.asyncio
async def test_focus_change_during_recognition_blocks_old_actions(env):
    configured(env)
    classifier(env, "practice")
    original = env["model"].complete
    release = threading.Event()
    started = threading.Event()

    async def delayed(request):
        if "secondary" in (request.get("schema") or {}).get("properties", {}):
            started.set()
            await asyncio.to_thread(release.wait, 15)
        return await original(request)

    env["model"].complete = delayed
    async with desk(env) as client:
        response = await client.post(
            "/desk/messages",
            json={"question": "考考我之前学的东西", "request_id": "slow-intent", "use_model": True},
        )
        assert response.status_code == 202
        cid = response.json()["conversation_id"]
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert started.is_set()
            changed = await client.post(
                "/desk/messages",
                json={
                    "question": "我想学习 Python",
                    "conversation_id": cid,
                    "request_id": "change-focus",
                },
            )
            assert changed.status_code == 202, changed.text
        finally:
            release.set()
        for _ in range(100):
            d = (await client.get("/desk/conversations/" + cid)).json()
            a = next(a for a in d["actions"] if a["question"] == "考考我之前学的东西")
            if a["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert "焦点已改变" in a["reply"]
        assert not d["flow"].get("attempt")
