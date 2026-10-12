import json
from uuid import UUID, uuid4

import pytest
from careerforge.api.app import app
from careerforge.application.run_service import create_run
from careerforge.infrastructure.db.models import AgentRun, User
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.demo import VERSION, execute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.infrastructure


@pytest.fixture
def case():
    clients = []
    for _ in range(2):
        client = TestClient(app)
        client.headers["Origin"] = "http://127.0.0.1:5173"
        credentials = {"email": f"{uuid4()}@example.test", "password": "correct-test-password"}
        assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
        client.headers["X-CSRF-Token"] = client.get("/api/v1/auth/csrf").json()["data"][
            "csrf_token"
        ]
        clients.append(client)
    with Session(get_engine()) as db, db.begin():
        owner = db.scalar(select(User.id).where(User.email == credentials["email"]))
        run = create_run(db, owner, "market_research", VERSION, {"synthetic": True})
        run_id = run.id
    execute(run_id, "api-test")
    yield clients[1], clients[0], run_id
    for client in clients:
        client.close()


def test_run_scope_filters_and_cursor(case):
    a, b, run_id = case
    assert b.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert b.get(f"/api/v1/runs/{run_id}/events").status_code == 404
    assert b.post(f"/api/v1/runs/{run_id}/cancel", json={"expected_version": 3}).status_code == 404
    assert a.get("/api/v1/runs?status=waiting_user&run_type=market_research").json()["data"][0][
        "run_id"
    ] == str(run_id)
    assert a.get("/api/v1/runs?cursor=invalid").status_code == 400


def test_json_sse_replay_and_invalid_cursors(case):
    a, _, run_id = case
    path = f"/api/v1/runs/{run_id}/events"
    rows = a.get(path).json()["data"]
    stream = a.get(path, headers={"Accept": "text/event-stream"})
    frames = [
        json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
    ]
    assert frames == rows
    last = rows[-1]["seq"]
    assert a.get(path + f"?after_seq={last}").json()["data"] == []
    assert len(a.get(path + "?after_seq=1").json()["data"]) == len(rows) - 1
    assert a.get(path + "?after_seq=-1").status_code == 400
    assert a.get(path + f"?after_seq={last + 1}").status_code == 400
    for value in ["bad", f"{uuid4()}:1", f"{run_id}:-1"]:
        assert a.get(path, headers={"Last-Event-ID": value}).status_code == 400
    assert a.get(path + "?after_seq=0", headers={"Last-Event-ID": f"{run_id}:1"}).status_code == 400
    with Session(get_engine()) as db, db.begin():
        db.get(AgentRun, run_id).min_event_seq = 3
    expired = a.get(path, headers={"Accept": "text/event-stream"})
    assert expired.status_code == 410 and "EVENT_HISTORY_EXPIRED" in expired.text


def test_resume_idempotency_and_schema_validation(case):
    a, _, run_id = case
    run = a.get(f"/api/v1/runs/{run_id}").json()["data"]
    wait = run["waiting_input"]
    body = {
        "expected_version": run["version"],
        "wait_request_id": wait["wait_request_id"],
        "input_manifest_hash": run["input_manifest_hash"],
        "response": {"choice": "bogus"},
    }
    path = f"/api/v1/runs/{run_id}/resume"
    key = {"Idempotency-Key": str(uuid4())}
    assert a.post(path, json=body, headers=key).status_code == 422
    body["response"] = {"choice": "accept"}
    assert a.post(path, json=body, headers=key).status_code == 202
    assert a.post(path, json=body, headers=key).status_code == 202
    assert a.post(path, json=body, headers={"Idempotency-Key": str(uuid4())}).status_code == 409
    body["response"] = {"choice": "reject"}
    assert a.post(path, json=body, headers=key).status_code == 409
    execute(run_id, "resumed-api-test")
    assert a.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "succeeded"


def test_event_endpoint_does_not_accept_another_wait(case):
    a, _, run_id = case
    run = a.get(f"/api/v1/runs/{run_id}").json()["data"]
    body = {
        "expected_version": run["version"],
        "wait_request_id": str(UUID(int=1)),
        "input_manifest_hash": run["input_manifest_hash"],
        "response": {"choice": "accept"},
    }
    assert (
        a.post(
            f"/api/v1/runs/{run_id}/resume", json=body, headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 404
    )
