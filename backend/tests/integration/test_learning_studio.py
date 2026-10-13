from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients

from careerforge.application import studio_service as service
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult
from careerforge.workflows.studio_agent import execute

pytestmark = pytest.mark.infrastructure


def test_reviewed_patch_and_confirmed_command(clients, monkeypatch):
    a, b = clients
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-placeholder"))
    created = post(
        a, "/studio/workspaces", {"name": "practice", "files": {"main.py": "print(1)\n"}}
    )
    assert created.status_code == 201, created.text
    w = created.json()["data"]
    wid = w["workspace_id"]
    assert b.get("/api/v1/studio/workspaces/" + wid).status_code == 404
    response = post(
        a,
        f"/studio/workspaces/{wid}/agent-runs",
        {"expected_revision": 1, "instruction": "Change output", "paths": ["main.py"]},
    )
    assert response.status_code == 202, response.text
    calls = []

    async def complete(self, request):
        calls.append(request)
        return ModelResult(
            provider="mock",
            finish_reason="stop",
            structured={
                "answer": "Proposed change; not executed",
                "citation_ids": [],
                "edits": [
                    {
                        "path": "main.py",
                        "before": "print(1)\n",
                        "after": "print(2)\n",
                        "reason": "practice",
                    }
                ],
                "commands": [{"argv": ["python", "main.py"], "reason": "observe output"}],
            },
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    rid = UUID(response.json()["data"]["run_id"])
    execute(rid, "studio-mock")
    execute(rid, "redelivery")
    assert len(calls) == 1 and calls[0].max_output_tokens <= 2048
    w = a.get(f"/api/v1/studio/workspaces/{wid}").json()["data"]
    assert w["files"]["main.py"] == "print(1)\n"
    patch = next(x for x in w["actions"] if x["kind"] == "patch")
    command = next(x for x in w["actions"] if x["kind"] == "command")
    assert command["status"] == "pending"
    wrong = post(
        a,
        f"/studio/workspaces/{wid}/commands/{command['id']}/execute",
        {"expected_revision": 1, "confirmation": "APPLY"},
    )
    assert wrong.status_code == 422
    applied = post(
        a,
        f"/studio/workspaces/{wid}/patches/{patch['id']}/apply",
        {"expected_revision": 1, "confirmation": "APPLY"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["files"]["main.py"] == "print(2)\n"
    from careerforge.infrastructure import workspace_runner

    runs = []

    def run(files, argv):
        runs.append((files, argv))
        return {
            "exit_code": 0,
            "stdout": "2\n",
            "stderr": "",
            "files_captured": True,
            "result_files": {**files, "artifact.txt": "result"},
        }

    monkeypatch.setattr(workspace_runner, "run", run)
    response = post(
        a,
        f"/studio/workspaces/{wid}/commands/{command['id']}/execute",
        {"expected_revision": 2, "confirmation": "RUN"},
    )
    assert response.status_code == 200, response.text
    w = response.json()["data"]
    assert len(runs) == 1 and "artifact.txt" not in w["files"]
    assert any(x["kind"] == "patch" and x["status"] == "pending" for x in w["actions"])
    post(
        a,
        f"/studio/workspaces/{wid}/commands/{command['id']}/execute",
        {"expected_revision": 2, "confirmation": "RUN"},
    )
    assert len(runs) == 1
    # Applied changes are never overwritten by a stale reviewed base.
    with Session(get_engine()) as db:
        from careerforge.infrastructure.db.models import LearningWorkspace

        row = db.get(LearningWorkspace, UUID(wid))
        stale = {
            "id": "00000000-0000-0000-0000-000000000003",
            "kind": "patch",
            "status": "pending",
            "edits": patch["edits"],
        }
        service.append(row, stale)
        db.commit()
    response = post(
        a,
        f"/studio/workspaces/{wid}/patches/{stale['id']}/apply",
        {"expected_revision": 2, "confirmation": "APPLY"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "name", ["../main.py", "/etc/passwd", ".env", "src/../../x", "a/.ssh/id_rsa"]
)
def test_workspace_paths_stay_in_project(name):
    with pytest.raises(BusinessError):
        service.path(name)
