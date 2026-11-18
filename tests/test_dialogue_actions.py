import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from careerforge.api import create_app
from careerforge.contracts_conversation_actions_v1 import ConversationActionPlan
from careerforge.models import AgentRun, ModelReceipt
from careerforge.models_dialogue import GoalAddendum
from careerforge.services.security import create_principal

CASES = {
    row["case_id"]: row
    for line in (Path(__file__).parent / "fixtures/dialogue_cases.jsonl")
    .read_text(encoding="utf-8-sig")
    .splitlines()
    if line.strip()
    for row in [json.loads(line)]
}


def api_client(env):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])),
        base_url="http://local/api/v1",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    )


async def open_dialogue(api):
    value = (await api.post("/conversations", json={})).json()
    return f"/conversations/{value['conversation_id']}"


async def send(api, path, text, **kwargs):
    revision = kwargs.pop("revision", None)
    if revision is None:
        revision = (await api.get(path)).json()["revision"]
    return await api.post(
        path + "/messages",
        json={
            "text": text,
            "expected_focus_revision": revision,
            "idempotency_key": kwargs.pop("key", uuid4().hex),
            **kwargs,
        },
    )


@pytest.mark.asyncio
async def test_T02_T04_T05_T06_T08_control_lane_keeps_original_jd_and_running_answer(env):
    app = env["app"]
    raw = CASES["T02"]["user_message"]
    async with api_client(env) as api:
        path = await open_dialogue(api)
        imported = (await send(api, path, raw, authorization_ref=env["funding"])).json()
        assert imported["lane"] == "control" and len(imported["run_refs"]) == 1, imported
        extract_id = imported["run_refs"][0]
        doc_id = imported["action_receipts"][0]["jd_revision_id"]
        with app.db.read() as db:
            manifest = db.get(AgentRun, extract_id).input_hash
        assert app.jd.read(env["actor"], doc_id)["raw_text"] == raw
        answer = await send(api, path, "解释这个项目的工具调用。", authorization_ref=env["funding"])
        assert answer.status_code == 202 and answer.json()["lane"] == "answer", answer.text
        assert app.runtime.claim(answer.json()["run_id"], "answer-lane")
        status = await send(api, path, CASES["T04"]["user_message"])
        assert status.json()["lane"] == "control", status.text
        busy = await send(api, path, "讲解另一个实现细节。", authorization_ref=env["funding"])
        assert busy.status_code == 409 and "CONVERSATION_BUSY" in busy.text
        clarify = (await send(api, path, CASES["T06"]["user_message"])).json()
        assert "原 JD" in clarify["reply"] and not clarify["run_refs"]
        revision = (await api.get(path)).json()["revision"]
        amend = dict(key="user-topic", revision=revision)
        first = (await send(api, path, CASES["T05"]["user_message"], **amend)).json()
        second = (await send(api, path, CASES["T05"]["user_message"], **amend)).json()
        assert first == second and first["workflow_id"] != imported["workflow_id"]
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(GoalAddendum)) == 1
            item = db.get(GoalAddendum, first["action_receipts"][0]["goal_addendum_id"])
            assert item.manifest["topic"] == "LangGraph"
            assert item.manifest["origin"] == "user_addendum"
            assert item.manifest["modality"] is None and item.manifest["required_depth"] is None
            assert db.get(AgentRun, extract_id).input_hash == manifest
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
            assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0
        assert app.jd.read(env["actor"], doc_id)["raw_text"] == raw


@pytest.mark.asyncio
async def test_T07_T09_compare_and_swap_and_foreign_controls(env):
    async with api_client(env) as api:
        path = await open_dialogue(api)
        assert (await send(api, path, "我想学习 Python")).status_code == 202
        revision = (await api.get(path)).json()["revision"]
        rows = await asyncio.gather(
            *[send(api, path, CASES["T07"]["user_message"], revision=revision) for _ in range(2)]
        )
        assert sorted(row.status_code for row in rows) == [202, 409]
        foreign = await send(api, path, CASES["T09"]["user_message"])
        assert foreign.status_code == 404
        with env["app"].db.transaction() as db:
            other, ns, _ = create_principal(db, "foreign-action-owner")
        funding = env["app"].runtime.create_funding(other.id, ns.id, 20)
        assert (await send(api, path, "讲解模块职责", authorization_ref=funding)).status_code == 404
        assert env["model"].calls == []


@pytest.mark.parametrize(
    "action",
    [
        {"type": "request_pause", "funding_id": "foreign"},
        {"type": "query_progress", "run_id": "foreign"},
        {"type": "add_learning_topic", "target": "LangGraph", "tool_permission": "all"},
    ],
)
def test_T09_action_schema_forbids_untrusted_authority(action):
    with pytest.raises(ValidationError):
        ConversationActionPlan.model_validate(
            {
                "conversation_id": "bound",
                "expected_focus_revision": 1,
                "idempotency_key": "one",
                "actions": [{"step_id": "first", "action": action}],
            }
        )
