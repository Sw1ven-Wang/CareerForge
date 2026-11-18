import pytest
from test_local_web import desk

from careerforge.desk_capabilities import intake_reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,expected",
    [
        ("帮我把这个项目入库", "请提供 GitHub"),
        (r"请读取 E:\projects\demo", "暂无法"),
        ("帮我部署这个项目", "暂无法"),
        ("你能做什么", "审核通过并发布后才算"),
    ],
)
async def test_guidance_without_model_or_learning_mutation(env, question, expected):
    async with desk(env) as client:
        response = await client.post(
            "/desk/messages",
            json={"question": question, "request_id": "guide", "use_model": False},
        )
        assert response.status_code == 202
        job = response.json()
        assert job["status"] == "completed"
        assert expected in job["reply"]
        assert not job.get("run_id")
        detail = (await client.get("/desk/conversations/" + job["conversation_id"])).json()
        assert not detail["flow"]["status"].get("source_jd_revision_id")
        assert not detail["flow"]["status"].get("workflow_id")


@pytest.mark.parametrize(
    "question",
    [
        "请解释部署这个项目的原理",
        "不要部署这个项目，只解释源码",
        "JD：岗位职责是部署和修改代码",
        "请解释字符串‘帮我部署这个项目’",
    ],
)
def test_mentions_are_not_capability_commands(question):
    assert intake_reply(question, {}) is None


def test_existing_project_is_not_requested_again():
    reply = intake_reply("帮我把这个项目入库", {"target": "https://github.com/example/demo"})
    assert "无需重复提供地址" in reply
    assert "确认目标" in reply


@pytest.mark.asyncio
async def test_repository_import_saves_target_but_does_not_claim_publication(env):
    async with desk(env) as client:
        response = await client.post(
            "/desk/messages",
            json={
                "question": "帮我把 https://github.com/topoteretes/cognee 入库",
                "request_id": "repo",
                "use_model": False,
            },
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert "已保存指定项目地址" in job["reply"]
        assert "尚未完成知识入库" in job["reply"]
        detail = (await client.get("/desk/conversations/" + job["conversation_id"])).json()
        assert detail["flow"]["status"]["milestone"] == "awaiting_goal_confirmation"
        assert detail["flow"]["status"]["publication_count"] == 0


@pytest.mark.asyncio
async def test_coordinator_receives_capability_boundaries(env):
    from test_desk_learning_flow import configured, send

    from careerforge.contracts import ModelOutput

    configured(env)
    seen = []

    def respond(request):
        seen.append(request["context"])
        return ModelOutput(structured={"reply": "请提供相关代码，我会帮你解释。"}, synthetic=True)

    env["model"].overrides["coordinator"] = respond
    async with desk(env) as client:
        await send(client, "我能把本地文件直接交给你吗？", agent="dialogue")
    assert seen[-1]["capabilities"]["unsupported"]
    assert seen[-1]["capabilities"]["rules"]
    assert "snapshot_at" in seen[-1]
