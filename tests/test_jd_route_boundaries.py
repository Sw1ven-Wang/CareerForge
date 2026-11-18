"""JD mentions must not bypass dialogue recognition and mutate learning goals."""

import pytest
from test_local_web import desk

from careerforge.contracts import ModelOutput
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.services.dialogue_routing import explicit_actions

QUESTIONS = [
    "请解释岗位要求中的 RAG 是什么。",
    "岗位要求是如何提取成技术需求的？",
    "请解释字符串“岗位职责”的含义，不要导入。",
    "不要导入 JD：熟悉 Python",
    "请解释 JD：熟悉 Python 的含义",
    "招聘系统是如何实现的？",
    "我们要求这个函数返回什么？",
    "岗位职责：是什么意思？",
    "请导入岗位要求是什么意思？",
    "JD：",
    "我们要求熟悉 Python 是什么意思？",
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_jd_mentions_are_not_explicit_imports(text):
    message = DialogueMessage(text=text, expected_focus_revision=1, idempotency_key="test")
    assert explicit_actions(message) is None


@pytest.mark.parametrize("prefix", ["JD：", "jd: ", "请导入 JD：", "帮我导入这份岗位要求："])
def test_explicit_import_preserves_payload(prefix):
    payload = "熟悉 Python\n岗位职责：检索，并且不要使用外部服务。"
    message = DialogueMessage(
        text=prefix + payload, expected_focus_revision=1, idempotency_key="test"
    )
    assert explicit_actions(message) == [("import_jd", {"text": payload})]


def test_typed_jd_import_does_not_interpret_document_instructions():
    text = "岗位职责：解释 RAG，不要导入外部数据。"
    message = DialogueMessage(
        text=text, kind="jd", expected_focus_revision=1, idempotency_key="test"
    )
    assert explicit_actions(message) == [("import_jd", {"text": text})]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", QUESTIONS[:3])
@pytest.mark.parametrize("use_model", [False, True])
async def test_actual_desk_jd_questions_do_not_import(env, text, use_model):
    from test_desk_learning_flow import configured, send
    from test_learning_intent import classifier

    configured(env)
    seen = classifier(env, "chat")
    env["model"].overrides["coordinator"] = lambda request: ModelOutput(
        structured={"reply": "这是关于岗位要求的解释问题。"}, synthetic=True
    )
    async with desk(env) as client:
        if use_model:
            job, detail = await send(client, text)
            assert seen, "Question must reach semantic recognition"
        else:
            response = await client.post(
                "/desk/messages",
                json={"question": text, "request_id": "boundary", "use_model": False},
            )
            assert response.status_code == 202
            job = response.json()
            detail = (await client.get("/desk/conversations/" + job["conversation_id"])).json()
        assert "已保存 JD" not in job.get("reply", "")
        assert not detail["flow"]["status"].get("source_jd_revision_id")
        assert not detail["flow"]["status"].get("goal_version_id")
