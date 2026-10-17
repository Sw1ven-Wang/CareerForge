import json
from uuid import UUID, uuid4

import pytest
from careerforge.application import studio_service as studio
from careerforge.application.studio_learning_service import validate_guide
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import LearningWorkspace
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, Usage
from careerforge.schemas.studio import LearningGuide
from careerforge.workflows.studio_agent import execute
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients
from test_studio_learning_agent import setup_learning

pytestmark = pytest.mark.infrastructure


def test_report_practice_patch_command_and_revocation(clients, monkeypatch):
    a, b = clients
    project, workspace, learning_request = setup_learning(a, monkeypatch)
    wid, objective = workspace["workspace_id"], learning_request["objective_version_id"]
    hit = a.get(
        "/api/v1/knowledge/search",
        params={
            "q": "execute_tool",
            "snapshot_id": project["repository_snapshot_id"],
        },
    ).json()["data"]["results"][0]
    linked = post(
        a,
        "/knowledge/links",
        {
            "chunk_id": hit["chunk_id"],
            "objective_version_id": objective,
        },
    ).json()["data"]
    cid = linked["citation_id"]
    source = a.get("/api/v1/citations/" + cid).json()["data"]
    report_id = str(uuid4())
    with Session(get_engine()) as db, db.begin():
        row = db.get(LearningWorkspace, UUID(wid))
        guide = LearningGuide(
            status="grounded",
            summary="离线练习说明",
            points=[
                {
                    "title": "参数分发",
                    "explanation": "将参数传给工具。",
                    "citation_id": cid,
                    "quote": "return tool(**arguments)",
                }
            ],
            reading_order=[cid],
            practice=["在自己的项目中实现参数分发并观察输出。"],
            questions=["参数不符合约定时怎样处理？"],
            limitations=["离线样例，未执行。"],
        )
        points, sources = validate_guide(db, row.owner_id, guide, [source])
        studio.append(
            row,
            {
                "id": report_id,
                "kind": "learning_report",
                "status": "completed",
                "objective_version_id": objective,
                "objective_title": "工具调用",
                "guide": {**guide.model_dump(), "points": points},
                "sources": sources,
                "synthetic": True,
            },
        )
    calls = []

    async def complete(self, request):
        calls.append(request)
        context = json.loads(request.prompt[request.prompt.index('{"files"') :])
        assert context["practice"]["exercise"] == guide.practice[0]
        assert context["practice"]["synthetic_source"] is True
        assert context["references"][0]["citation_id"] == cid
        assert request.max_output_tokens == get_settings().model_max_output_tokens
        return ModelResult(
            provider="offline-mock",
            finish_reason="stop",
            synthetic=True,
            usage=Usage(input_tokens=100, output_tokens=50, status="known"),
            structured={
                "answer": "按当前练习提出小型变更，尚未执行。",
                "citation_ids": [cid],
                "edits": [
                    {
                        "path": "mine.py",
                        "before": "print(1)\n",
                        "after": "print(2)\n",
                        "reason": "practice",
                    }
                ],
                "commands": [{"argv": ["python", "mine.py"], "reason": "observe"}],
            },
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    url = f"/studio/workspaces/{wid}/learning-reports/{report_id}/practices"
    body = {"expected_revision": 1, "exercise_index": 0}
    assert post(b, url, body).status_code == 404
    key = str(uuid4())
    created = post(a, url, body, key)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    pid, workspace = data["practice_id"], data["workspace"]
    assert workspace["revision"] == 2 and workspace["files"] == {"mine.py": "print(1)\n"}
    assert post(a, url, body, key).json()["data"]["practice_id"] == pid
    assert post(a, url, {**body, "expected_revision": 2}).json()["data"]["practice_id"] == pid
    assert len(calls) == 0
    agent = {
        "expected_revision": 2,
        "instruction": "实现一个练习步骤",
        "mode": "assist",
        "objective_version_id": objective,
        "practice_id": pid,
        "paths": ["mine.py"],
    }
    assert (
        post(
            a,
            f"/studio/workspaces/{wid}/agent-runs",
            {
                **agent,
                "objective_version_id": str(uuid4()),
            },
        ).status_code
        == 422
    )
    response = post(a, f"/studio/workspaces/{wid}/agent-runs", agent)
    assert response.status_code == 202, response.text
    rid = UUID(response.json()["data"]["run_id"])
    execute(rid, "offline-practice")
    execute(rid, "duplicate")
    assert len(calls) == 1
    data = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    artifacts = [v for v in data["actions"] if v.get("practice_id") == pid]
    assert {v["kind"] for v in artifacts} == {"message", "patch", "command"}
    patch = next(v for v in artifacts if v["kind"] == "patch")
    command = next(v for v in artifacts if v["kind"] == "command")
    assert data["files"]["mine.py"] == "print(1)\n" and command["status"] == "pending"
    applied = post(
        a,
        f"/studio/workspaces/{wid}/patches/{patch['id']}/apply",
        {
            "expected_revision": 2,
            "confirmation": "APPLY",
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["files"]["mine.py"] == "print(2)\n"
    from careerforge.infrastructure import workspace_runner

    commands = []

    def run(files, argv):
        commands.append(argv)
        return {
            "exit_code": 0,
            "stdout": "2\n",
            "stderr": "",
            "files_captured": True,
            "result_files": {**files, "observation.txt": "offline simulated result"},
        }

    monkeypatch.setattr(workspace_runner, "run", run)
    endpoint = f"/studio/workspaces/{wid}/commands/{command['id']}/execute"
    assert post(a, endpoint, {"expected_revision": 3, "confirmation": "APPLY"}).status_code == 422
    assert commands == []
    executed = post(a, endpoint, {"expected_revision": 3, "confirmation": "RUN"})
    assert executed.status_code == 200, executed.text
    data = executed.json()["data"]
    assert commands == [["python", "mine.py"]] and "observation.txt" not in data["files"]
    generated = next(v for v in data["actions"] if v.get("command_id") == command["id"])
    assert generated["practice_id"] == pid and generated["status"] == "pending"
    note_url = f"/api/v1/studio/workspaces/{wid}/practices/{pid}/note"
    response = a.patch(note_url, json={"expected_revision": 3, "note": "需要再理解参数校验"})
    assert response.status_code == 200, response.text
    assert a.patch(note_url, json={"expected_revision": 3, "note": "stale"}).status_code == 409
    practice = next(v for v in response.json()["data"]["actions"] if v["id"] == pid)
    assert practice["verified"] is False and practice["status"] == "draft"
    a.request(
        "DELETE",
        "/api/v1/repositories/" + project["repository_id"],
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    hidden = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    related = [v for v in hidden["actions"] if v["id"] == pid or v.get("practice_id") == pid]
    assert all(v["status"] == "unavailable" for v in related)
    assert all("exercise" not in v and "answer" not in v and "edits" not in v for v in related)
    assert (
        post(
            a,
            f"/studio/workspaces/{wid}/patches/{generated['id']}/apply",
            {
                "expected_revision": 4,
                "confirmation": "APPLY",
            },
        ).status_code
        == 404
    )
    assert (
        post(
            a,
            f"/studio/workspaces/{wid}/agent-runs",
            {
                **agent,
                "expected_revision": 4,
            },
        ).status_code
        == 404
    )
    assert len(calls) == 1 and len(commands) == 1
