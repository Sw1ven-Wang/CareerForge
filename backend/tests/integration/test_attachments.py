from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from careerforge.application.run_service import db_now
from careerforge.infrastructure.db.models import Attachment
from careerforge.infrastructure.db.session import get_engine
from sqlalchemy.orm import Session
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure


def upload(client, name, body, key=None, purpose="source"):
    return client.post(
        "/api/v1/attachments",
        files={"file": (name, body, "text/plain")},
        data={"purpose": purpose},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def import_attachment(client, identifier, url):
    return client.post(
        "/api/v1/sources/import",
        json={
            "source_type": "job_posting",
            "import_mode": "attachment",
            "attachment_id": identifier,
            "source_url": url,
        },
        headers={"Idempotency-Key": str(uuid4())},
    )


@pytest.mark.parametrize(
    "name,body",
    [
        ("payload.zip", b"PKfake"),
        ("fake.txt", b"\x00binary"),
        ("bad.json", b"{broken"),
        ("bad.py", b"def ("),
        (
            "bad.xml",
            b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuite>&x;</testsuite>',
        ),
        ("wrong.xml", b"<invoice/>"),
        ("large.log", b"a" * (1024 * 1024 + 1)),
    ],
    ids=["zip", "binary", "json", "python", "xml-entity", "xml-type", "size"],
)
def test_reject_unsupported_or_invalid_files(clients, name, body):
    a, _ = clients
    assert upload(a, name, body).status_code in [413, 422]


def test_binding_owner_root_and_attachment_revocation(clients):
    a, b = clients
    raw = "实习\n任职要求：\nPython 编程".encode()
    attachment = upload(a, "jd.md", raw).json()["data"]
    identity = attachment["attachment_id"]
    assert b.get("/api/v1/attachments/" + identity + "/content").status_code == 404
    assert import_attachment(b, identity, "https://example.test/other-owner").status_code == 404
    first = import_attachment(a, identity, "https://example.test/first")
    assert first.status_code == 201, first.text
    source = first.json()["data"]
    assert (
        a.get(
            "/api/v1/attachments/" + identity + "/content",
            params={"snapshot_id": source["source_snapshot_id"]},
        ).content
        == raw
    )
    assert import_attachment(a, identity, "https://example.test/second").status_code == 409
    revision = a.post(
        "/api/v1/sources/" + source["source_id"] + "/snapshots",
        json={"expected_version": 1, "change_reason": "同根新版本", "attachment_id": identity},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert revision.status_code == 201, revision.text
    deleted = a.request(
        "DELETE",
        "/api/v1/attachments/" + identity,
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert deleted.status_code == 202
    assert a.get("/api/v1/attachments/" + identity + "/content").status_code == 404
    assert (
        a.get(
            "/api/v1/sources/" + source["source_id"] + "/snapshots/" + source["source_snapshot_id"]
        ).status_code
        == 404
    )


def test_root_deletion_revokes_content_and_old_upload_replay(clients):
    a, _ = clients
    key = str(uuid4())
    attachment = upload(a, "sample.json", b'{"synthetic":true}', key).json()["data"]
    identity = attachment["attachment_id"]
    source = import_attachment(a, identity, "https://example.test/" + str(uuid4())).json()["data"]
    assert (
        a.request(
            "DELETE",
            "/api/v1/sources/" + source["source_id"],
            json={"expected_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 202
    )
    assert a.get("/api/v1/attachments/" + identity + "/content").status_code == 404
    assert upload(a, "sample.json", b'{"synthetic":true}', key).status_code == 404


def test_unbound_expiry_and_safe_junit(clients):
    a, _ = clients
    attachment = upload(
        a, "result.xml", b'<testsuite tests="1"><testcase name="synthetic"/></testsuite>'
    ).json()["data"]
    identity = attachment["attachment_id"]
    with Session(get_engine()) as db, db.begin():
        db.get(Attachment, UUID(identity)).expires_at = db_now(db) - timedelta(seconds=1)
    assert a.get("/api/v1/attachments/" + identity + "/content").status_code == 404
