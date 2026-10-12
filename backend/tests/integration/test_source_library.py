from uuid import uuid4

import pytest
from test_market_research import post
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure


def test_editable_library_keeps_empty_folders_and_frozen_evidence(clients):
    a, b = clients
    root_input = {"name": "Agent 研发", "parent_id": None}
    key = str(uuid4())
    response = post(a, "/source-library/categories", root_input, key)
    assert response.status_code == 201, response.text
    root = response.json()["data"]
    assert post(a, "/source-library/categories", root_input, key).json()["data"] == root
    assert post(a, "/source-library/categories", root_input).status_code == 409
    leaf = post(
        a,
        "/source-library/categories",
        {
            "name": "公司招聘页",
            "parent_id": root["id"],
        },
    ).json()["data"]
    empty = post(a, "/source-library/initialize", {}).json()["data"]
    assert {item["id"] for item in empty} == {root["id"], leaf["id"]}
    assert (
        post(
            b,
            "/source-library/categories",
            {
                "name": "越权来源",
                "parent_id": root["id"],
            },
        ).status_code
        == 404
    )
    assert (
        post(
            a,
            "/source-library/categories",
            {
                "name": "错误三级",
                "parent_id": leaf["id"],
            },
        ).status_code
        == 422
    )

    imported = post(
        a,
        "/sources/import",
        {
            "source_type": "job_posting",
            "title": "Agent 工程师",
            "content": "熟悉 Python。",
            "job_category": root["name"],
            "source_category": leaf["name"],
        },
    ).json()["data"]
    source_id = imported["source_id"]
    before = a.get("/api/v1/sources/" + source_id).json()["data"]
    assert before["library_category_id"] == leaf["id"]
    renamed = post(
        a,
        "/source-library/categories/" + root["id"],
        {
            "name": "智能体开发",
            "parent_id": None,
            "expected_version": root["version"],
        },
    )
    assert renamed.status_code == 200, renamed.text
    assert (
        post(
            a,
            "/source-library/categories/" + root["id"],
            {
                "name": "过期名称",
                "parent_id": None,
                "expected_version": root["version"],
            },
        ).status_code
        == 409
    )
    assert a.get("/api/v1/sources/" + source_id).json()["data"]["snapshots"] == before["snapshots"]

    other = post(
        a,
        "/source-library/categories",
        {
            "name": "研究岗位",
            "parent_id": None,
        },
    ).json()["data"]
    moved = post(
        a,
        "/source-library/categories/" + leaf["id"],
        {
            "name": "招聘官网",
            "parent_id": other["id"],
            "expected_version": leaf["version"],
        },
    )
    assert moved.status_code == 200, moved.text
    edit = {
        "title": "高级 Agent 工程师",
        "source_url": "https://example.test/updated",
        "source_type": "job_posting",
        "category_id": leaf["id"],
        "expected_version": before["version"],
    }
    assert post(b, "/source-library/sources/" + source_id, edit).status_code == 404
    assert post(a, "/source-library/sources/" + source_id, edit).status_code == 200
    after = a.get("/api/v1/sources/" + source_id).json()["data"]
    assert after["title"] == edit["title"] and after["source_url"] == edit["source_url"]
    assert after["snapshots"] == before["snapshots"]
    assert post(a, "/source-library/sources/" + source_id, edit).status_code == 409

    link = post(
        a,
        "/sources/import",
        {
            "source_type": "job_posting",
            "title": "岗位线索",
            "import_mode": "link_clue",
            "source_url": "https://example.test/link",
            "job_category": other["name"],
            "source_category": "招聘官网",
        },
    ).json()["data"]
    assert (
        a.get("/api/v1/sources/" + link["source_id"]).json()["data"]["library_category_id"]
        == leaf["id"]
    )
    standardized = post(
        a,
        "/job-data/import",
        {
            "title": "Agent 新岗位",
            "raw_payload": "要求熟悉 Python。",
            "job_category": other["name"],
            "source_category": "招聘官网",
        },
    )
    assert standardized.status_code == 201, standardized.text
    listed = a.get("/api/v1/sources?limit=100").json()["data"]
    assert all(row["library_category_id"] == leaf["id"] for row in listed)
    assert post(b, "/source-library/initialize", {}).json()["data"] == []
