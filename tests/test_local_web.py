"""Local desk boundary and native learning operations, isolated from delivery data."""

import json

import httpx
import pytest
from conftest import publish_fixture

from careerforge.contracts_jd_v166 import JDImportInput
from careerforge.local_web import create_local_web
from careerforge.services.security import create_principal


def desk(env):
    (env["app"].settings.state_dir / "client.json").write_text(
        json.dumps(env["owner"]), encoding="utf-8"
    )
    app = create_local_web(env["app"])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8001",
        headers={"X-CareerForge-Desk": "1"},
    )


@pytest.mark.asyncio
async def test_local_boundary_rejects_cross_origin_and_hides_credentials(env):
    async with desk(env) as client:
        home = await client.get("/")
        assert home.status_code == 200
        assert "Content-Security-Policy" in home.headers
        assert env["owner"]["token"] not in home.text
        response = await client.get("/desk/overview")
        assert response.status_code == 200
        assert env["owner"]["token"] not in response.text
        assert "token" not in response.json()
        assert (
            await client.get("/desk/overview", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await client.get("/desk/overview", headers={"X-CareerForge-Desk": ""})
        ).status_code == 403
        assert (
            await client.get("/desk/overview", headers={"Host": "evil.example"})
        ).status_code == 400
        assert (
            await client.post(
                "/desk/goals",
                content="x=y",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        ).status_code == 415
        assert (await client.get("/assets/../../private/client.json")).status_code != 200


@pytest.mark.asyncio
async def test_desk_goals_and_conversations_stay_in_current_owner_scope(env):
    app = env["app"]
    with app.db.transaction() as db:
        other, other_ns, _ = create_principal(db, "other")
        other_id, ns_id = other.id, other_ns.id
    app.jd.import_jd(
        other_id,
        JDImportInput(namespace_id=ns_id, jd_id="other-jd", raw_text="Private other-owner JD"),
    )
    cid = app.conversations.create(other_id)
    async with desk(env) as client:
        response = await client.get("/desk/goals")
        assert response.status_code == 200
        assert "Private other-owner JD" not in response.text
        assert (await client.get("/desk/conversations/" + cid)).status_code == 404
        own = await client.post(
            "/desk/goals",
            json={
                "title": "Example role",
                "text": "Python application development",
                "request_id": "local-jd",
            },
        )
        assert own.status_code == 200
        assert (await client.get("/desk/goals")).json()["documents"][0][
            "raw_text"
        ] == "Python application development"


@pytest.mark.asyncio
async def test_evidence_chat_is_durable_and_idempotent_without_model_calls(env):
    async with desk(env) as client:
        body = {"question": "解释 Tool Calling", "request_id": "smoke", "use_model": False}
        first = await client.post("/desk/messages", json=body)
        assert first.status_code == 202, first.text
        second = await client.post("/desk/messages", json=body)
        assert second.json()["turn_id"] == first.json()["turn_id"]
        conflict = await client.post("/desk/messages", json={**body, "question": "different"})
        assert conflict.status_code == 409
        history = await client.get("/desk/conversations/" + first.json()["conversation_id"])
        assert len(history.json()["turns"]) == 1
        assert history.json()["turns"][0]["status"] == "completed"
        assert len(env["model"].calls) == 0


@pytest.mark.asyncio
async def test_plan_requires_acceptance_and_reflection_does_not_claim_mastery(env):
    _, publication_id, _ = await publish_fixture(env)
    async with desk(env) as client:
        overview = (await client.get("/desk/overview")).json()
        assert len(overview["publications"]) == 1
        proposal = await client.post(
            "/desk/plans",
            json={
                "publication_ids": [publication_id],
                "title": "Learning test",
                "learning_intent": "learn_project",
            },
        )
        assert proposal.status_code == 200, proposal.text
        p = proposal.json()
        assert p["accepted"] is False
        before = (await client.get("/desk/overview")).json()["plans"][0]
        assert before["active_version_id"] is None
        bad = await client.post(
            f"/desk/plans/{p['plan_id']}/accept",
            json={"version_id": p["version_id"], "expected_revision": 99},
        )
        assert bad.status_code >= 400
        accepted = await client.post(
            f"/desk/plans/{p['plan_id']}/accept",
            json={"version_id": p["version_id"], "expected_revision": p["revision"]},
        )
        assert accepted.status_code == 200
        observed = await client.post(
            "/desk/observations",
            json={
                "module_id": p["module_ids"][0],
                "response": "I traced the input conditions.",
                "rubric": {},
                "help_mode": "assisted",
            },
        )
        assert observed.status_code == 200
        overview = (await client.get("/desk/overview")).json()
        assert overview["plans"][0]["active_version_id"] == p["version_id"]
        assert overview["observations"][0]["competence_verified"] is False


@pytest.mark.asyncio
async def test_desk_navigation_reads_snapshot_and_rejects_invalid_evidence(env):
    from test_learning_events_v166 import navigation_block

    from careerforge.services.dependencies import invalidate

    block = await navigation_block(env)
    app = env["app"]
    async with desk(env) as client:
        path = "/desk/navigation/" + block.id
        response = await client.get(path)
        assert response.status_code == 200, response.text
        target = response.json()
        with app.db.read() as db:
            source = app.sources.get(db, env["actor"], target["source_id"])
            body = source.files[target["path"]]
            assert target["repository"] == source.repository_key
        assert target["display"]["text"] == "".join(
            body.splitlines(keepends=True)[target["start_line"] - 1 : target["end_line"]]
        )
        assert target["display"]["content_complete"]
        assert not target["client_highlight_ack"]
        assert (await client.get(path + "?citation_index=100")).status_code == 422
        assert (await client.get(path + "?citation_index=-1")).status_code == 422
        assert (await client.get("/desk/navigation/missing")).status_code == 404
        with app.db.transaction() as db:
            other, _, token = create_principal(db, "source-other")
        config = app.settings.state_dir / "client.json"
        config.write_text(json.dumps({"token": token}), encoding="utf-8")
        assert (await client.get(path)).status_code == 404
        config.write_text(json.dumps(env["owner"]), encoding="utf-8")
        with app.db.transaction() as db:
            invalidate(db, env["actor"], target["source_id"], "revoked", "navigation_test")
        assert (await client.get(path)).status_code != 200
