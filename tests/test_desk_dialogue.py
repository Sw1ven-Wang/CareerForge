"""Conversation coordinator stays responsive while the teacher is running."""

import asyncio
import threading

import pytest
from conftest import publish_fixture
from test_desk_learning_flow import configured, send
from test_local_web import desk

from careerforge.contracts import ModelOutput


@pytest.mark.asyncio
async def test_start_learning_and_progress_need_no_source_or_model(env):
    async with desk(env) as client:
        response = await client.post(
            "/desk/messages", json={"question": "开始学习", "request_id": "start"}
        )
        assert response.status_code == 202
        job = response.json()
        assert job["agent"] == "dialogue" and job["status"] == "completed"
        assert not job.get("run_id")
        assert "学习计划" in job["reply"]
        assert "不足以支持" not in job["reply"]
        again = await client.post(
            "/desk/messages", json={"question": "开始学习", "request_id": "start"}
        )
        assert again.json() == job
        response = await client.post(
            "/desk/messages",
            json={
                "question": "现在在做什么？",
                "conversation_id": job["conversation_id"],
                "request_id": "status",
            },
        )
        assert response.status_code == 202
        assert "没有正在执行" in response.json()["reply"]


@pytest.mark.asyncio
async def test_coordinator_model_is_separate_and_running_teacher_does_not_block_it(
    env, monkeypatch
):
    import careerforge.desk_workflow as worker

    await publish_fixture(env)
    configured(env)
    seen = []

    def respond(request):
        seen.append(request)
        return ModelOutput(
            structured={"reply": "可以每天用半小时学习，先阅读再练习。后台任务状态请看进度卡片。"},
            synthetic=True,
        )

    env["model"].overrides["coordinator"] = respond
    original = worker.execute_desk_job
    release = threading.Event()

    async def held(service, job, *, status_key="status"):
        if job.get("agent") not in {"dialogue", "intent"}:
            release.wait(30)
            job[status_key] = "interrupted"
            return
        await original(service, job, status_key=status_key)

    monkeypatch.setattr(worker, "execute_desk_job", held)
    async with desk(env) as client:
        try:
            r = await client.post(
                "/desk/messages",
                json={
                    "question": "请讲解 Tool Calling",
                    "agent": "tutor",
                    "request_id": "teacher",
                    "use_model": True,
                },
            )
            assert r.status_code == 202, r.text
            cid = r.json()["conversation_id"]
            assert r.json()["status"] == "running"
            p = await client.post(
                "/desk/messages",
                json={
                    "question": "还在运行吗？",
                    "request_id": "progress-during",
                    "conversation_id": cid,
                },
            )
            assert p.status_code == 202
            assert "仍在运行" in p.json()["reply"] or "正在执行" in p.json()["reply"]
            _, detail = await send(
                client, "我每天只有半小时，你建议如何安排时间？", cid, agent="dialogue"
            )
            assert seen and detail["actions"][-1]["agent"] == "dialogue"
            assert "半小时" in detail["actions"][-1]["reply"]
            assert seen[-1]["context"]["question"].startswith("我每天")
            from test_learning_intent import classifier

            classifier(env, "chat")
            _, detail = await send(client, "我有点分心，能聊聊如何集中精力吗？", cid)
            assert detail["actions"][-1]["routing"]["intents"] == ["chat"]
            assert len(detail["turns"]) == 1  # No teacher turn for the coordinator reply.
            jobs = (await client.get("/desk/overview")).json()["jobs"]
            assert (
                next(j for j in jobs if j.get("question") == "请讲解 Tool Calling")["status"]
                == "running"
            )
        finally:
            release.set()
            await asyncio.sleep(0.1)
