from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from careerforge.api import create_app
from careerforge.models import AgentRun, Conversation, ModelReceipt
from careerforge.services.security import create_principal


@pytest.mark.asyncio
async def test_onboarding_live_progress_replay_and_scope(env):
    app = env["app"]
    transport = httpx.ASGITransport(app=create_app(app))
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://trial/api/v1", headers=headers
    ) as api:
        conversation = (await api.post("/conversations", json={})).json()
        cid = conversation["conversation_id"]
        endpoint = f"/conversations/{cid}"
        initial = (await api.get(endpoint + "/workflow-status")).json()
        assert initial["entry_choices"] == ["jd", "repository", "topic"]
        assert initial["workflow_id"] is None
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        with app.db.transaction() as db:
            conv = db.get(Conversation, cid)
            workflow = app.workflows.create_in(db, conv)
            wid = workflow.id
        current = (await api.get(endpoint)).json()
        created = await api.post(
            endpoint + "/turns",
            json={
                "question": "Tool Calling 如何工作",
                "expected_revision": current["revision"],
                "idempotency_key": uuid4().hex,
                "funding_id": env["funding"],
            },
        )
        assert created.status_code == 202, created.text
        with app.db.transaction() as db:
            conv = db.get(Conversation, cid)
            workflow = app.workflows.owned_in(db, conv)
            run = db.get(AgentRun, created.json()["run_id"])
            app.workflows.link_in(db, workflow, run, "tutor")
        assert app.runtime.claim(run.id, "status-check") is not None
        for _ in range(3):
            status = (await api.get(endpoint + "/workflow-status")).json()
            assert status["workflow_id"] == wid
            assert status["active_stages"][0]["run_ids"] == [run.id]
            assert status["active_stages"][0]["status"] == "running"
            assert status["calls_consumed"] == 0 and status["call_cap"] == run.call_cap
        stream = endpoint + "/activity-events?once=true"
        first = await api.get(stream)
        assert first.status_code == 200 and "run.started" in first.text
        assert (await api.get(stream)).text == first.text
        last_id = [line[4:] for line in first.text.splitlines() if line.startswith("id: ")][-1]
        assert not (await api.get(stream, headers={"Last-Event-ID": last_id})).text
        with app.db.transaction() as db:
            _, _, token = create_principal(db, "other-dialogue-owner")
        outsider = {"Authorization": "Bearer " + token}
        for path in (endpoint + "/workflow-status", endpoint + "/workflows", stream):
            assert (await api.get(path, headers=outsider)).status_code == 404
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
            assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0
        assert env["model"].calls == []
