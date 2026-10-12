import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from careerforge.api.app import app
from careerforge.infrastructure.db.models import SourceSnapshot, UserSession
from careerforge.infrastructure.db.session import get_engine
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.infrastructure
ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def clients():
    values = []
    for _ in range(2):
        client = TestClient(app)
        client.headers["Origin"] = ORIGIN
        credentials = {"email": f"{uuid4()}@example.test", "password": "correct-test-password"}
        assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
        login = client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = client.get("/api/v1/auth/csrf").json()["data"][
            "csrf_token"
        ]
        values.append(client)
    yield values
    for client in values:
        client.close()


def post(client, path, payload, key=None):
    return client.post(path, json=payload, headers={"Idempotency-Key": key or str(uuid4())})


def imported(client, content="原文 Python 工作流\n第二行", key=None):
    payload = {
        "source_type": "job_posting",
        "content": content,
        "source_url": f"https://example.test/{uuid4()}",
    }
    response = post(client, "/api/v1/sources/import", payload, key)
    assert response.status_code == 201, response.text
    return response.json()["data"], payload


def test_two_owner_read_write_and_citation_isolation(clients):
    a, b = clients
    source, _ = imported(a)
    source_id, snapshot_id = source["source_id"], source["source_snapshot_id"]
    assert b.get(f"/api/v1/sources/{source_id}").status_code == 404
    assert b.get(f"/api/v1/sources/{source_id}/snapshots/{snapshot_id}").status_code == 404
    assert (
        post(
            b,
            f"/api/v1/sources/{source_id}/snapshots",
            {
                "expected_version": 1,
                "content": "intrusion",
                "change_reason": "attempt",
            },
        ).status_code
        == 404
    )
    payload = {
        "source_snapshot_id": snapshot_id,
        "char_start": 0,
        "char_end": 2,
        "text_hash": hashlib.sha256("原文".encode()).hexdigest(),
    }
    citation = post(a, "/api/v1/citations", payload).json()["data"]["citation_id"]
    assert b.get(f"/api/v1/citations/{citation}").status_code == 404
    assert post(b, "/api/v1/citations", payload).status_code == 404
    assert b.get("/api/v1/sources").json()["data"] == []
    assert (
        b.request(
            "DELETE",
            f"/api/v1/sources/{source_id}",
            json={"expected_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 404
    )


def test_idempotency_and_revalidation_after_delete(clients):
    a, _ = clients
    key = str(uuid4())
    source, payload = imported(a, key=key)
    assert post(a, "/api/v1/sources/import", payload, key).json()["data"] == source
    assert (
        post(a, "/api/v1/sources/import", {**payload, "content": "different"}, key).status_code
        == 409
    )
    delete_key = str(uuid4())
    path = f"/api/v1/sources/{source['source_id']}"
    response = a.request(
        "DELETE", path, json={"expected_version": 1}, headers={"Idempotency-Key": delete_key}
    )
    assert response.status_code == 202 and response.json()["data"]["status"] == "revoked"
    assert a.get(path).status_code == 404
    assert post(a, "/api/v1/sources/import", payload, key).status_code == 404
    assert (
        a.request(
            "DELETE", path, json={"expected_version": 1}, headers={"Idempotency-Key": delete_key}
        ).json()["data"]
        == response.json()["data"]
    )
    assert a.post("/api/v1/auth/logout").status_code == 200
    assert post(a, "/api/v1/sources/import", payload, key).status_code == 401


def test_snapshot_and_citation_stay_on_old_version(clients):
    a, _ = clients
    source, _ = imported(a, "original text")
    citation = post(
        a,
        "/api/v1/citations",
        {
            "source_snapshot_id": source["source_snapshot_id"],
            "char_start": 0,
            "char_end": 8,
            "text_hash": hashlib.sha256(b"original").hexdigest(),
        },
    )
    assert citation.status_code == 201
    key = str(uuid4())
    payload = {"expected_version": 1, "content": "updated text", "change_reason": "new source"}
    path = f"/api/v1/sources/{source['source_id']}/snapshots"
    updated = post(a, path, payload, key)
    assert updated.status_code == 201 and updated.json()["data"]["version"] == 2
    assert post(a, path, payload, key).json()["data"] == updated.json()["data"]
    assert post(a, path, payload).status_code == 409
    citation_id = citation.json()["data"]["citation_id"]
    assert a.get(f"/api/v1/citations/{citation_id}").json()["data"]["quote"] == "original"
    old = a.get(f"/api/v1/sources/{source['source_id']}/snapshots/{source['source_snapshot_id']}")
    assert old.json()["data"]["content"] == "original text"


def test_metadata_revision_preserves_historical_type(clients):
    a, _ = clients
    source, _ = imported(a)
    changed = post(
        a,
        f"/api/v1/sources/{source['source_id']}/snapshots",
        {
            "expected_version": 1,
            "base_snapshot_id": source["source_snapshot_id"],
            "metadata_overrides": {"source_type": "interview_experience"},
            "change_reason": "fix type",
        },
    )
    assert changed.status_code == 201, changed.text
    old = a.get(f"/api/v1/sources/{source['source_id']}/snapshots/{source['source_snapshot_id']}")
    assert old.json()["data"]["metadata"]["source_type"] == "job_posting"
    assert (
        a.get(f"/api/v1/sources/{source['source_id']}").json()["data"]["source_type"]
        == "interview_experience"
    )


def test_link_clue_has_no_snapshot(clients):
    a, _ = clients
    result = post(
        a,
        "/api/v1/sources/import",
        {
            "source_type": "job_posting",
            "import_mode": "link_clue",
            "source_url": f"https://example.test/{uuid4()}",
        },
    )
    data = result.json()["data"]
    assert data["source_snapshot_id"] is None and data["content_hash"] is None
    assert data["read_status"] == "unread"
    assert a.get(f"/api/v1/sources/{data['source_id']}").json()["data"]["snapshots"] == []


def test_goal_revisions_and_owner_scope(clients):
    a, b = clients
    initial = post(a, "/api/v1/career-goals", {"role_direction": "Agent开发"}).json()["data"]
    identifier = initial["goal_id"]
    assert b.get(f"/api/v1/career-goals/{identifier}").status_code == 404
    payload = {
        "role_direction": "Agent后端",
        "expected_version": 1,
        "change_reason": "narrow scope",
    }
    key = str(uuid4())
    updated = post(a, f"/api/v1/career-goals/{identifier}/versions", payload, key)
    assert updated.status_code == 201
    assert (
        post(a, f"/api/v1/career-goals/{identifier}/versions", payload, key).json()["data"]
        == updated.json()["data"]
    )
    assert post(a, f"/api/v1/career-goals/{identifier}/versions", payload).status_code == 409
    result = a.get(f"/api/v1/career-goals/{identifier}").json()["data"]
    assert result["role_direction"] == "Agent后端" and result["time_budget"]["weekly_hours"] is None


def test_session_expiration_csrf_and_origin(clients):
    a, _ = clients
    payload = {"source_type": "user_note", "content": "private text"}
    token = a.headers.pop("X-CSRF-Token")
    assert post(a, "/api/v1/sources/import", payload).status_code == 403
    a.headers["X-CSRF-Token"] = token
    assert post(a, "/api/v1/sources/import", payload).status_code == 201
    assert (
        a.post(
            "/api/v1/sources/import", json=payload, headers={"Origin": "https://untrusted.test"}
        ).status_code
        == 403
    )
    with Session(get_engine()) as db, db.begin():
        hashed = hashlib.sha256(a.cookies["careerforge_session"].encode()).hexdigest()
        row = db.get(UserSession, hashed)
        row.expires_at = db.scalar(select(func.now())) - timedelta(seconds=1)
    assert a.get("/api/v1/auth/me").status_code == 401


def test_validation_never_echoes_password_or_body(clients):
    a, _ = clients
    response = a.post("/api/v1/auth/register", json={"email": "bad", "password": "secret"})
    assert response.status_code == 422 and "secret" not in response.text
    result = post(
        a,
        "/api/v1/sources/import",
        {"source_type": "job_posting", "content": "private", "owner_id": str(uuid4())},
    )
    assert result.status_code == 422 and "private" not in result.text


def test_concurrent_idempotency_different_connections(clients):
    a, _ = clients
    payload = {"source_type": "user_note", "content": f"synthetic-concurrent-{uuid4()}"}
    key = str(uuid4())

    def submit(_):
        with TestClient(app) as client:
            client.cookies.update(a.cookies)
            client.headers.update(a.headers)
            return post(client, "/api/v1/sources/import", payload, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(submit, range(2))
    assert first.status_code == second.status_code == 201
    assert first.json()["data"] == second.json()["data"]
    snapshot_id = UUID(first.json()["data"]["source_snapshot_id"])
    with Session(get_engine()) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceSnapshot)
                .where(SourceSnapshot.id == snapshot_id)
            )
            == 1
        )


def test_cross_owner_association_rejected_by_postgres(clients):
    a, b = clients
    source, _ = imported(a)
    owner_b = UUID(b.get("/api/v1/auth/me").json()["data"]["user_id"])
    with Session(get_engine()) as db:
        with pytest.raises(IntegrityError), db.begin():
            db.add(
                SourceSnapshot(
                    owner_id=owner_b,
                    source_id=UUID(source["source_id"]),
                    version_no=99,
                    content="wrong owner",
                    content_hash="a" * 64,
                    metadata_hash="a" * 64,
                    metadata_values={},
                )
            )
            db.flush()


def test_invalid_cursor_is_safe(clients):
    a, _ = clients
    assert a.get("/api/v1/sources?cursor=invalid").status_code == 400


def test_normalization_and_hash(clients):
    a, _ = clients
    source, _ = imported(a, "甲\r\n乙\r丙")
    response = a.get(
        f"/api/v1/sources/{source['source_id']}/snapshots/{source['source_snapshot_id']}"
    ).json()["data"]
    assert response["content"] == "甲\n乙\n丙"
    assert response["content_hash"] == hashlib.sha256("甲\n乙\n丙".encode()).hexdigest()
