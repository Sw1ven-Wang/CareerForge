from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients
from test_planning import objective_body

from careerforge.infrastructure.db.models import RepositoryFile
from careerforge.infrastructure.db.session import get_engine

pytestmark = pytest.mark.infrastructure

SOURCE = """def execute_tool(tool, arguments):
    \"\"\"Dispatch a registered tool and return its result.\"\"\"
    return tool(**arguments)


def run_agent(tool):
    return execute_tool(tool, {})
"""


def test_import_search_link_and_reference_integrity(clients):
    a, b = clients
    body = {
        "name": "Agent learning",
        "github_url": "https://github.com/example/agent",
        "requested_ref": "a" * 40,
        "tags": ["工具调用"],
        "files": {
            "src/tools.py": SOURCE,
            "README.md": "# Tool dispatch\nLearning examples\n",
            "client.ts": "export function dispatchTool() { return 1 }\n",
        },
    }
    key = str(uuid4())
    response = post(a, "/knowledge/imports", body, key)
    assert response.status_code == 201, response.text
    project = response.json()["data"]
    sid = project["repository_snapshot_id"]
    assert project["indexed"] and project["file_count"] == 3
    assert project["commit_verified"] is False
    assert post(a, "/knowledge/imports", body, key).json()["data"] == project
    assert post(a, "/knowledge/imports", {**body, "name": "changed"}, key).status_code == 409
    assert (
        post(a, f"/knowledge/snapshots/{sid}/index", {}).json()["data"]["chunk_count"]
        == project["chunk_count"]
    )
    response = a.get("/api/v1/knowledge/search", params={"q": "execute_tool", "snapshot_id": sid})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    hit = data["results"][0]
    assert hit["symbol"] == "execute_tool" and hit["kind"] == "FunctionDef"
    assert hit["content"] == "".join(
        SOURCE.splitlines(keepends=True)[hit["start_line"] - 1 : hit["end_line"]]
    )
    assert data["semantic_enabled"] is False
    assert (
        b.get("/api/v1/knowledge/search", params={"q": "execute_tool"}).json()["data"]["results"]
        == []
    )
    assert b.get("/api/v1/knowledge/chunks/" + hit["chunk_id"]).status_code == 404
    assert post(b, f"/knowledge/snapshots/{sid}/index", {}).status_code == 404
    related = a.get(
        "/api/v1/knowledge/search", params={"q": "run_agent", "snapshot_id": sid}
    ).json()["data"]["results"][0]
    edges = a.get(f"/api/v1/knowledge/chunks/{related['chunk_id']}/related").json()["data"][
        "results"
    ]
    assert any(
        e["symbol"] == "execute_tool" and e["relation"] == "reference_candidate" for e in edges
    )
    obj = post(a, "/learning-objectives", objective_body(goal(a))).json()["data"][
        "objective_version_id"
    ]
    binding = {
        "chunk_id": hit["chunk_id"],
        "objective_version_id": obj,
        "note": "Read tool dispatch",
    }
    assert post(b, "/knowledge/links", binding).status_code == 404
    linked = post(a, "/knowledge/links", binding)
    assert linked.status_code == 201, linked.text
    links = a.get("/api/v1/knowledge/links", params={"objective_version_id": obj}).json()["data"]
    assert len(links) == 1 and links[0]["quote"] == hit["content"]
    cid = links[0]["citation_id"]
    assert a.get("/api/v1/citations/" + cid).json()["data"]["commit"] == "a" * 40
    assert b.get("/api/v1/citations/" + cid).status_code == 404
    # Metadata changes reindex context without moving immutable code or citations.
    metadata = {
        "name": "Renamed",
        "description": "a unique learning topic",
        "tags": ["specialtopic"],
        "expected_revision": 1,
    }
    assert a.put(f"/api/v1/knowledge/snapshots/{sid}/metadata", json=metadata).status_code == 200
    assert a.get("/api/v1/knowledge/search", params={"q": "specialtopic"}).json()["data"]["results"]
    assert a.put(f"/api/v1/knowledge/snapshots/{sid}/metadata", json=metadata).status_code == 409
    # Tampering cannot silently yield a valid code reference.
    with Session(get_engine()) as db:
        from sqlalchemy import select

        file = db.scalar(
            select(RepositoryFile).where(
                RepositoryFile.snapshot_id == UUID(sid), RepositoryFile.path == hit["path"]
            )
        )
        file.content += "# tampered\n"
        db.commit()
    assert a.get("/api/v1/knowledge/chunks/" + hit["chunk_id"]).status_code == 409


def test_rejected_import_and_revocation(clients):
    a, _ = clients
    body = {
        "name": "bounded",
        "github_url": "https://github.com/example/bounded",
        "requested_ref": "b" * 40,
        "files": {"main.py": "print(1)\n"},
    }
    assert post(a, "/knowledge/imports", {**body, "requested_ref": "main"}).status_code == 422
    assert (
        post(a, "/knowledge/imports", {**body, "files": {"../escape.py": "x"}}).status_code == 422
    )
    assert post(a, "/knowledge/imports", {**body, "files": {".env": "private"}}).status_code == 422
    project = post(a, "/knowledge/imports", body).json()["data"]
    result = a.request(
        "DELETE",
        "/api/v1/repositories/" + project["repository_id"],
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 202
    assert a.get("/api/v1/knowledge/search", params={"q": "print"}).json()["data"]["results"] == []
    assert a.get("/api/v1/knowledge/projects").json()["data"] == []
