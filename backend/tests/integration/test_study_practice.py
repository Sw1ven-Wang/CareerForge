"""Synthetic C-package paths only; no real provider or command execution."""

from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_market_research import post, source
from test_owner_scope import clients as clients
from test_studio_delivery import confirmation
from test_studio_learning_agent import model_result

from careerforge.application import studio_service as studio
from careerforge.application import study_service as study
from careerforge.application.common import fingerprint
from careerforge.application.repository_service import code_citation
from careerforge.application.run_service import db_now
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import LearningWorkspace
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.db.study_models import StudyTrack
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ToolCall
from careerforge.workflows.assessment.workflow import execute as assess
from careerforge.workflows.studio_agent import execute as assist

pytestmark = pytest.mark.infrastructure
CODE = "def dispatch(tool, arguments):\n    return tool(**arguments)\n"


def setup_module_plan(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-only"))
    imported = source(client, "岗位要求：熟悉 Python 参数分发和失败边界。")
    created = post(
        client,
        "/study/tracks",
        {"name": "C offline", "source_snapshot_ids": [imported["source_snapshot_id"]]},
    ).json()["data"]
    project = post(
        client,
        "/knowledge/imports",
        {
            "name": "C fixture",
            "github_url": "https://github.com/example/c-evidence",
            "requested_ref": "c" * 40,
            "files": {"tool.py": CODE, "LICENSE": "MIT License"},
        },
    ).json()["data"]
    tid = created["track_id"]
    with Session(get_engine()) as db, db.begin():
        row = db.get(StudyTrack, UUID(tid))
        citation = code_citation(
            db, row.owner_id, UUID(project["repository_snapshot_id"]), "tool.py", 1, 2
        )
        cid = str(citation.id)
        requirements = study.put(
            db,
            row,
            "requirements",
            {
                "requirements": [
                    {
                        "key": "python",
                        "technology": "Python",
                        "keywords": ["dispatch"],
                        "depth": "熟悉",
                        "depth_basis": "explicit",
                        "rationale": "fixture",
                        "evidence": [],
                    }
                ],
                "limitations": ["synthetic fixture"],
            },
        )
        candidate = {
            "github_url": "https://github.com/example/c-evidence",
            "full_name": "example/c-evidence",
            "commit": "c" * 40,
            "origin": "user_import",
            "repository_snapshot_id": project["repository_snapshot_id"],
        }
        review = study.put(
            db,
            row,
            "review",
            {
                "requirements_id": str(requirements.id),
                "candidate_id": str(uuid4()),
                "candidate": candidate,
                "admitted": True,
                "verdict": "strong",
                "summary": "fixture",
                "technology_stack": ["Python"],
                "quality_notes": [],
                "limitations": [],
                "synthetic": True,
                "repository_snapshot_id": project["repository_snapshot_id"],
                "highlights": [
                    {
                        "citation_id": cid,
                        "requirement_key": "python",
                        "path": "tool.py",
                        "start_line": 1,
                        "end_line": 2,
                        "quote": CODE.strip(),
                        "explanation": "fixture",
                        "commit": "c" * 40,
                    }
                ],
            },
        )
        module = {
            "key": "leaf",
            "parent_key": None,
            "title": "参数分发",
            "depth": "熟悉",
            "requirement_keys": ["python"],
            "project_review_ids": [str(review.id)],
            "highlight_ids": [cid],
            "prerequisite_keys": [],
            "steps": ["实现并解释参数分发"],
            "deliverable": "一个可定位的实现和观察记录",
            "acceptance": ["解释正常参数与失败输入的行为"],
        }
        plan = study.put(
            db,
            row,
            "plan",
            {
                "title": "已有模块计划",
                "summary": "fixture",
                "modules": [module],
                "requirements_id": str(requirements.id),
                "review_ids": [str(review.id)],
                "limitations": ["synthetic"],
                "synthetic": True,
                "intent": {"kind": "technical_design", "focus": "参数边界"},
            },
        )
        pid = str(plan.id)
        row.payload = {
            **row.payload,
            "requirements_id": str(requirements.id),
            "active_plan_id": pid,
        }
    return {"tid": tid, "pid": pid, "cid": cid, "source": imported, "module": module}


def start(client, fixture, mode="agent_assisted", kind="practice"):
    path = "/study/tracks/" + fixture["tid"]
    state = client.get("/api/v1" + path).json()["data"]
    body = {
        "expected_revision": state["revision"],
        "plan_id": fixture["pid"],
        "module_key": "leaf",
        "instruction": "实现参数分发并解释失败输入",
        "acceptance": ["记录正常与失败输入"],
        "help_mode": mode,
        "exercise_kind": kind,
        "variation": "用异步工具和非法参数替代参考的同步正常输入" if kind == "transfer" else "",
        "scope_acknowledged": True,
    }
    response = post(client, path + "/attempts", body)
    assert response.status_code == 201, response.text
    return response.json()["data"], body


def work(client, attempt, content="pass\n"):
    wid = attempt["workspace_id"]
    workspace = client.get("/api/v1/studio/workspaces/" + wid).json()["data"]
    result = client.put(
        "/api/v1/studio/workspaces/" + wid + "/file",
        json={"expected_revision": workspace["revision"], "path": "mine.py", "content": content},
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def frozen_command_fixture(attempt):
    # Synthetic receipt, not a sandbox command: this test never executes project code.
    identifier = str(uuid4())
    with Session(get_engine()) as db, db.begin():
        row = db.get(LearningWorkspace, UUID(attempt["workspace_id"]))
        studio.append(
            row,
            {
                "id": identifier,
                "kind": "command",
                "practice_id": attempt["practice_id"],
                "status": "completed",
                "argv": ["python", "mine.py"],
                "exit_code": 0,
                "stdout": "offline fixture\n",
                "stderr": "",
                "execution_input_hash": fingerprint(row.payload["files"]),
                "execution_finished_at": db_now(db).isoformat(),
                "execution_environment": {"runner_contract": "synthetic offline receipt"},
            },
        )
    return identifier


def base(attempt):
    return f"/studio/workspaces/{attempt['workspace_id']}/practices/{attempt['practice_id']}"


def submit(client, attempt, command_id, external="none"):
    state = client.get("/api/v1" + base(attempt) + "/delivery").json()["data"]
    body = {
        "expected_revision": state["workspace_revision"],
        "summary": "synthetic C deliverable",
        "command_ids": [command_id],
        "explanation_answers": [
            {
                "question": c["scenario"],
                "answer": "本人说明：参数按关键字展开，非法键会产生错误；本材料为离线夹具。",
                "criterion_version_ids": [c["criterion_version_id"]],
            }
            for c in state["criteria"]
            if c["dimension"] == "explanation"
        ],
        "transfer_notes": ["fixture: 使用不同输入并记录失败边界；不代表真实用户迁移。"],
        "external_help": external,
        "reported_hint_count": 0,
    }
    response = post(client, base(attempt) + "/submissions", body)
    assert response.status_code == 201, response.text
    state = response.json()["data"]
    assess(UUID(state["assessment_run_id"]), "offline-C")
    state = client.get("/api/v1" + base(attempt) + "/delivery").json()["data"]
    dimensions = {c["dimension"] for c in state["criteria"]}
    assert {"artifact", "explanation"} <= dimensions <= {"artifact", "explanation", "transfer"}
    response = post(
        client,
        base(attempt) + "/assessments/" + state["assessment"]["assessment_id"] + "/confirm",
        confirmation(state),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_module_assisted_submission_human_review_plan_version_and_revocation(clients, monkeypatch):
    a, b = clients
    fixture = setup_module_plan(a, monkeypatch)
    attempt, _ = start(a, fixture)
    work(a, attempt)
    calls = []

    async def complete(self, request):
        calls.append(request)
        return model_result(
            calls=[
                ToolCall(
                    id="proposal",
                    name="submit_reply",
                    arguments={
                        "answer": "offline helper",
                        "citation_ids": [fixture["cid"]],
                        "edits": [
                            {
                                "path": "mine.py",
                                "before": "pass\n",
                                "after": CODE,
                                "reason": "synthetic helper patch",
                            }
                        ],
                        "commands": [],
                    },
                )
            ]
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    wid = attempt["workspace_id"]
    state = a.get("/api/v1/studio/workspaces/" + wid).json()["data"]
    proposed = post(
        a,
        "/studio/workspaces/" + wid + "/agent-runs",
        {
            "expected_revision": state["revision"],
            "instruction": "one bounded step",
            "paths": ["mine.py"],
        },
    )
    assert proposed.status_code == 202, proposed.text
    assist(UUID(proposed.json()["data"]["run_id"]), "offline-C-helper")
    state = a.get("/api/v1/studio/workspaces/" + wid).json()["data"]
    patch = next(x for x in state["actions"] if x["kind"] == "patch")
    assert state["files"]["mine.py"] == "pass\n" and patch["practice_id"] == attempt["practice_id"]
    applied = post(
        a,
        "/studio/workspaces/" + wid + "/patches/" + patch["id"] + "/apply",
        {"expected_revision": state["revision"], "confirmation": "APPLY"},
    )
    assert applied.status_code == 200
    assert b.get("/api/v1" + base(attempt) + "/delivery").status_code == 404
    command_id = frozen_command_fixture(attempt)
    state = submit(a, attempt, command_id)
    evidence = state["learning_evidence"]
    assert (
        evidence["learning_state"] == "assisted_completion" and not evidence["competence_verified"]
    )
    assert evidence["assistance"]["system_agent_requests"] == 1
    assert evidence["assistance"]["system_applied_agent_patches"] == 1 and len(calls) == 1
    target = state["plans"][0]
    assert target["kind"] == "study"
    payload = {
        "plan_id": target["plan_id"],
        "base_plan_version_id": target["plan_version_id"],
        "expected_plan_version": target["version"],
        "reason": "record bounded assisted outcome",
        "scope_acknowledged": True,
    }
    key = str(uuid4())
    updated = post(a, base(attempt) + "/plan-updates", payload, key)
    assert updated.status_code == 201, updated.text
    new_id = updated.json()["data"]["plan_id"]
    assert (
        post(a, base(attempt) + "/plan-updates", payload, key).json()["data"]["plan_id"] == new_id
    )
    track = a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    old = next(p for p in track["plans"] if p["id"] == fixture["pid"])
    new = next(p for p in track["plans"] if p["id"] == new_id)
    assert old["modules"] == new["modules"] and "module_outcomes" not in old
    assert new["module_outcomes"]["leaf"]["learning_state"] == "assisted_completion"
    assert not track["progress"]
    state = a.get("/api/v1" + base(attempt) + "/delivery").json()["data"]
    revoke = post(
        a,
        base(attempt) + "/assessments/" + state["assessment"]["assessment_id"] + "/revoke",
        {
            "expected_version": state["assessment"]["version"],
            "expected_task_version": state["task"]["version"],
            "reason": "fixture withdrawal",
        },
    )
    assert revoke.status_code == 200, revoke.text
    track = a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    assert (
        next(p for p in track["plans"] if p["id"] == new_id)["module_outcomes"]["leaf"]["effective"]
        is False
    )
    removed = a.request(
        "DELETE",
        "/api/v1/sources/" + fixture["source"]["source_id"],
        json={"expected_version": fixture["source"]["version"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert removed.status_code == 202
    assert a.get("/api/v1" + base(attempt) + "/delivery").status_code == 404


@pytest.mark.parametrize(
    "external,kind,expected",
    [
        ("none", "transfer", "transfer_passed_declared_independent"),
        ("used", "practice", "assisted_completion"),
    ],
)
def test_independent_isolation_sealing_and_repeated_attempts(
    clients, monkeypatch, external, kind, expected
):
    a, _ = clients
    fixture = setup_module_plan(a, monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No provider is allowed in the independent path")

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", forbidden)
    attempt, body = start(a, fixture, "independent", kind)
    wid = attempt["workspace_id"]
    assert attempt["workspace"]["files"] == {}
    request = post(
        a,
        "/studio/workspaces/" + wid + "/agent-runs",
        {"expected_revision": 1, "instruction": "omitted practice ID must not bypass isolation"},
    )
    assert (
        request.status_code == 409
        and request.json()["error"]["code"] == "INDEPENDENT_ATTEMPT_ACTIVE"
    )
    track = a.get("/api/v1/study/tracks/" + fixture["tid"]).json()["data"]
    request = post(
        a,
        "/study/tracks/" + fixture["tid"] + "/runs",
        {
            "expected_revision": track["revision"],
            "role": "tutor",
            "plan_id": fixture["pid"],
            "module_key": "leaf",
            "question": "a hint?",
        },
    )
    assert (
        request.status_code == 409
        and request.json()["error"]["code"] == "INDEPENDENT_ATTEMPT_ACTIVE"
    )
    patch_id = str(uuid4())
    with Session(get_engine()) as db, db.begin():
        row = db.get(LearningWorkspace, UUID(wid))
        studio.append(
            row,
            {
                "id": patch_id,
                "kind": "patch",
                "status": "pending",
                "practice_id": attempt["practice_id"],
                "edits": [
                    {
                        "path": "forbidden.py",
                        "before": None,
                        "after": "pass\n",
                        "reason": "injected fixture",
                    }
                ],
            },
        )
    rejected = post(
        a,
        "/studio/workspaces/" + wid + "/patches/" + patch_id + "/apply",
        {"expected_revision": 1, "confirmation": "APPLY"},
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["error"]["code"] == "INDEPENDENT_PATCH_FORBIDDEN"
    )
    workspace = work(a, attempt, CODE)
    command = post(
        a,
        "/studio/workspaces/" + wid + "/commands",
        {
            "expected_revision": workspace["revision"],
            "argv": ["python", "mine.py"],
            "reason": "manual proposal",
        },
    )
    assert command.status_code == 201
    assert any(
        x["kind"] == "command" and x["status"] == "pending"
        for x in command.json()["data"]["actions"]
    )
    state = submit(a, attempt, frozen_command_fixture(attempt), external)
    assert state["learning_evidence"]["learning_state"] == expected
    assert state["learning_evidence"]["assistance"]["system_agent_requests"] == 0
    repeat = post(
        a,
        base(attempt) + "/submissions",
        {
            "expected_revision": state["workspace_revision"],
            "summary": "cannot change sealed independence",
        },
    )
    assert repeat.status_code == 409 and repeat.json()["error"]["code"] == "ATTEMPT_CLOSED"
    second, _ = start(a, fixture, "independent", kind)
    assert second["workspace_id"] != wid and second["workspace"]["files"] == {}
    assert second["workspace"]["study_attempt"]["exercise_version"] == 1
    assert second["workspace"]["study_attempt"]["ordinal"] == 2
    ended = post(
        a,
        f"/study/tracks/{fixture['tid']}/attempts/{second['attempt_id']}/close",
        {"expected_revision": second["workspace"]["revision"], "reason": "end synthetic attempt"},
    )
    assert ended.status_code == 200
