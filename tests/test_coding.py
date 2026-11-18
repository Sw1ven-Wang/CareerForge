"""Model and command execution are explicit test doubles; not Docker/Pi validation."""

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import TurnInput
from careerforge.errors import DomainError
from careerforge.models import PatchProposal


class FixtureSandbox:
    synthetic = True

    def __init__(self):
        self.calls = []

    def execute(self, execution, argv):
        self.calls.append((execution, argv))
        return {
            "exit_code": 0,
            "stdout": "synthetic execution receipt, not real code execution",
            "stderr": "",
            "files": execution["files"],
            "synthetic": True,
        }


@pytest.mark.asyncio
async def test_coding_review_apply_run_submit_accept(env):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    ws = app.coding.create_workspace(actor, publication_ids=[pub])
    wid = ws["workspace_id"]
    run = app.coding.start(actor, wid, 1, [], "写示例函数", env["funding"], 1, "coding")
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result, result
    with app.db.read() as db:
        proposal = db.scalar(select(PatchProposal).where(PatchProposal.workspace_id == wid))
        pid = proposal.id
    assert app.coding.detail(actor, wid)["files"] == {}
    with pytest.raises(DomainError):
        app.coding.apply(actor, wid, pid, 1, "yes")
    applied = app.coding.apply(actor, wid, pid, 1, "APPLY")
    assert applied["applied_revision"] == 2
    assert app.coding.apply(actor, wid, pid, 1, "APPLY")["replayed"]
    command = app.coding.propose_command(actor, wid, 2, ["python", "-B", "answer.py"])
    box = FixtureSandbox()
    assert app.coding.process_commands(box) == 0 and box.calls == []
    with pytest.raises(DomainError):
        app.coding.confirm_command(actor, command["command_id"], "bad", "RUN")
    app.coding.confirm_command(actor, command["command_id"], command["approval_nonce"], "RUN")
    assert app.coding.process_commands(box) == 1 and len(box.calls) == 1
    assert app.coding.process_commands(box) == 0
    receipt = app.coding.command_result(actor, command["command_id"])["receipt"]
    assert receipt["input_hash"] == applied["manifest_hash"] and receipt["synthetic"]
    sub = app.coding.submit(actor, wid, 2, "解释返回值", "迁移到小型函数", "used", "submission")
    reviewed = app.coding.review_submission(
        actor,
        sub["submission_id"],
        {"artifact_and_runtime": True, "explanation": True, "transfer": True},
        "REVIEW",
    )
    assert reviewed["accepted"] and not reviewed["competence_verified"]


@pytest.mark.asyncio
async def test_old_patch_conflict_and_command_binds_revision(env):
    app, actor = env["app"], env["actor"]
    ws = app.coding.create_workspace(actor)
    wid = ws["workspace_id"]
    app.coding.start(actor, wid, 1, [], "写函数", env["funding"], 1, "edit")
    await app.drain()
    with app.db.read() as db:
        p = db.scalar(select(PatchProposal).where(PatchProposal.workspace_id == wid))
        pid = p.id
    app.coding.save(actor, wid, 1, "other.py", "x=1\n")
    with pytest.raises(DomainError):
        app.coding.apply(actor, wid, pid, 2, "APPLY")
    cmd = app.coding.propose_command(actor, wid, 2, ["python", "-c", "print(1)"])
    app.coding.save(actor, wid, 2, "other.py", "x=2\n")
    with pytest.raises(DomainError):
        app.coding.confirm_command(actor, cmd["command_id"], cmd["approval_nonce"], "RUN")


def test_independent_attempt_pauses_chat_and_seals_first_submission(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    ws = app.coding.create_workspace(actor, assistance_mode="independent")
    wid = ws["workspace_id"]
    with pytest.raises(DomainError):
        app.conversations.submit(
            actor, conv, TurnInput(question="讲解", expected_revision=1, idempotency_key="blocked")
        )
    with pytest.raises(DomainError):
        app.coding.start(actor, wid, 1, [], "写代码", env["funding"], 1, "blocked")
    app.coding.save(actor, wid, 1, "mine.py", "print(1)\n")
    s = app.coding.submit(actor, wid, 2, "我写的", "迁移差异", "none_declared", "independent")
    assert app.coding.detail(actor, wid)["sealed"]
    with pytest.raises(DomainError):
        app.coding.save(actor, wid, 2, "mine.py", "new")
    with pytest.raises(DomainError):
        app.coding.review_submission(
            actor,
            s["submission_id"],
            {"artifact_and_runtime": True, "explanation": True, "transfer": True},
            "REVIEW",
        )
    assert app.coding.submit(actor, wid, 2, "我写的", "迁移差异", "none_declared", "independent")[
        "replayed"
    ]


@pytest.mark.parametrize("argv", [["sh", "-c", "anything"], ["bash"], [], ["python", "bad\0path"]])
def test_no_shell_fallback(env, argv):
    ws = env["app"].coding.create_workspace(env["actor"])
    with pytest.raises(DomainError):
        env["app"].coding.propose_command(env["actor"], ws["workspace_id"], 1, argv)
