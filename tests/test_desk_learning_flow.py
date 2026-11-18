"""Web conversation actions persist actual learning effects, not canned replies."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from conftest import publish_fixture
from test_dialogue_workflow_e2e import configure_grouped_extraction
from test_local_web import desk
from test_model_assessment_v166 import setup_model
from test_session_plans_v166 import planner


async def send(client, question, cid=None, **extra):
    response = await client.post(
        "/desk/messages",
        json={
            "question": question,
            "conversation_id": cid,
            "request_id": uuid4().hex,
            "use_model": True,
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    for _ in range(250):
        jobs = (await client.get("/desk/overview")).json()["jobs"]
        current = next(j for j in jobs if j.get("request_hash") == job["request_hash"])
        if current["status"] != "running":
            assert current["status"] == "completed", current
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Desk worker did not reach a terminal status")
    response = await client.get("/desk/conversations/" + job["conversation_id"])
    assert response.status_code == 200, response.text
    return job, response.json()


def configured(env):
    env["app"].settings = replace(
        env["app"].settings,
        model_name="fixture",
        model_api_key="fixture",
        model_base_url="http://fixture.invalid",
    )


@pytest.mark.asyncio
async def test_conversation_learning_plan_practice_feedback_and_advance(env):
    _, pid, _ = await publish_fixture(env)
    planner(env)
    setup_model(env)
    configured(env)
    async with desk(env) as client:
        job, detail = await send(client, "使用所选材料生成计划", publication_ids=[pid])
        cid = job["conversation_id"]
        assert detail["flow"]["plan"]["active_version_id"] is None
        version = detail["flow"]["plan"]["versions"][-1]["version_id"]
        bad = await client.post(
            "/desk/messages",
            json={
                "question": "确认学习计划",
                "conversation_id": cid,
                "request_id": "stale",
                "confirm_version": "old",
            },
        )
        assert bad.status_code == 409
        _, detail = await send(client, "确认学习计划", cid, confirm_version=version)
        assert detail["flow"]["plan"]["active_version_id"] == version
        assert detail["flow"]["teaching"]["delivery_status"] == "completed"
        count = len((await client.get("/desk/overview")).json()["plans"])
        _, detail = await send(client, "我确认采用刚才的学习计划", cid)
        assert len((await client.get("/desk/overview")).json()["plans"]) == count
        _, detail = await send(client, "开始练习", cid)
        attempt = detail["flow"]["attempt"]["attempt_id"]
        _, again = await send(client, "开始练习", cid)
        assert again["flow"]["attempt"]["attempt_id"] == attempt
        _, detail = await send(client, "提交答案：函数返回传入的 value。", cid)
        assert detail["flow"]["attempt"]["state"] == "submitted"
        _, detail = await send(client, "评估我的答案", cid)
        assert detail["flow"]["assessment"]["result"]["competence_verified"] is False
        assert detail["flow"]["assessment"]["result"]["rubric_item_decisions"]
        _, detail = await send(client, "接受材料缺口并确认阅读完成，进入下一模块", cid)
        assert "module_ack" in detail["flow"], detail["actions"][-1].get("reply")
        assert detail["flow"]["module_ack"]["plan_progress_status"] == "finished_by_user_ack"
        assert detail["flow"]["module_ack"]["competence_verified"] is False
        assert detail["flow"]["assessment"]["historical"] is True
        assert detail["flow"]["assessment"]["result"]["attempt_id"] == attempt
        _, detail = await send(client, "查询学习进度", cid)
        assert detail["flow"]["status"]["milestone"] == "learning_completed"


@pytest.mark.asyncio
async def test_conversation_jd_is_imported_extracted_and_resumable(env):
    configured(env)
    configure_grouped_extraction(env)
    async with desk(env) as client:
        job, detail = await send(client, "JD：熟悉 Python 或 Java，Redis 优先，不要求模型微调")
        assert detail["flow"]["status"]["source_jd_revision_id"]
        assert detail["flow"]["status"]["goal_version_id"]
        assert detail["flow"]["goal"]["requirements"]
        history = (await client.get("/desk/overview")).json()["conversations"]
        assert any(c["id"] == job["conversation_id"] for c in history)
    async with desk(env) as fresh:
        detail = (await fresh.get("/desk/conversations/" + job["conversation_id"])).json()
        assert detail["actions"] and detail["flow"]["status"]["goal_version_id"]


@pytest.mark.asyncio
async def test_scoped_worker_never_executes_other_funding_or_historical_jobs(env):
    from careerforge.contracts import TurnInput
    from careerforge.desk_workflow import execute_desk_job
    from careerforge.models import AgentRun

    app = env["app"]
    actor = env["actor"]
    historical_cid = app.conversations.create(actor)
    old = app.conversations.submit(
        actor,
        historical_cid,
        TurnInput(
            question="解释 Tool Calling",
            expected_revision=1,
            idempotency_key="old",
            funding_id=env["funding"],
        ),
    )
    funding = app.runtime.create_funding(actor, env["ns"], 8)
    cid = app.conversations.create(actor)
    new = app.conversations.submit(
        actor,
        cid,
        TurnInput(
            question="解释 Tool Calling",
            expected_revision=1,
            idempotency_key="new",
            funding_id=funding,
        ),
    )
    job = {"actor": actor, "funding_id": funding, "run_id": new["run_id"]}
    await execute_desk_job(app, job)
    with app.db.read() as db:
        assert db.get(AgentRun, old["run_id"]).status == "queued"
        assert db.get(AgentRun, new["run_id"]).status in {"succeeded", "partially_succeeded"}
    assert old["run_id"] not in job["run_refs"]


@pytest.mark.asyncio
async def test_new_source_build_review_publication_and_plan_in_same_chat(env):
    from test_dialogue_workflow_e2e import fixed_source

    configured(env)
    planner(env)
    source = fixed_source(env, "desk-new-source")
    async with desk(env) as client:
        job, _ = await send(client, "我想学习 Tool Calling")
        _, detail = await send(client, "确认目标", job["conversation_id"], source_ids=[source])
        assert detail["flow"]["status"]["publication_count"] > 0, detail["flow"]
        assert detail["flow"]["plan"]["active_version_id"] is None
        assert "build" in detail["flow"]["status"]["completed_stages"]
        assert "plan" in detail["flow"]["status"]["completed_stages"]
