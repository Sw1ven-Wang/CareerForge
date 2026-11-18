"""Adversarial service contracts with explicit model/sandbox fixtures, not Docker QA."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_coding import FixtureSandbox

from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    CommandAuthorization,
    ExecutionSnapshot,
    PatchProposal,
    Principal,
    Submission,
)
from careerforge.services.security import create_principal
from careerforge.util import fingerprint, manifest


def command(env):
    app, actor = env["app"], env["actor"]
    ws = app.coding.create_workspace(actor, {"main.py": "print(1)\n"})
    return ws, app.coding.propose_command(actor, ws["workspace_id"], 1, ["python", "-B", "main.py"])


def confirm(env, cmd):
    return env["app"].coding.confirm_command(
        env["actor"], cmd["command_id"], cmd["approval_nonce"], "RUN"
    )


@pytest.mark.parametrize("change", ["edit", "same_content", "sealed", "inactive"])
def test_queued_command_revalidates_workspace_before_execution(env, change):
    app, actor = env["app"], env["actor"]
    ws, cmd = command(env)
    confirm(env, cmd)
    if change in {"edit", "same_content"}:
        app.coding.save(
            actor,
            ws["workspace_id"],
            1,
            "main.py",
            "print(2)\n" if change == "edit" else "print(1)\n",
        )
    elif change == "sealed":
        app.coding.close_attempt(actor, ws["workspace_id"])
    else:
        with app.db.transaction() as db:
            db.get(Principal, actor).active = False
    box = FixtureSandbox()
    assert app.coding.process_commands(box) == 1
    assert box.calls == []
    with app.db.read() as db:
        row = db.get(CommandAuthorization, cmd["command_id"])
        assert row.state == "rejected" and row.receipt["executed"] is False


@pytest.mark.parametrize("field", ["image", "environment", "argv", "files", "expiry", "binding"])
@pytest.mark.parametrize("queued", [False, True])
def test_command_full_approval_binding_rejects_tampering(env, field, queued):
    app = env["app"]
    _, cmd = command(env)
    if queued:
        confirm(env, cmd)
    with app.db.transaction() as db:
        row = db.get(CommandAuthorization, cmd["command_id"])
        snap = db.get(ExecutionSnapshot, row.execution_snapshot_id)
        if field == "image":
            snap.image_digest = "sha256:" + "2" * 64
        elif field == "environment":
            snap.environment_version = "network-enabled"
        elif field == "argv":
            row.argv = ["python", "-c", 'print("changed")']
            row.argv_hash = fingerprint(row.argv)
        elif field == "files":
            snap.files = {"main.py": 'print("changed")'}
            snap.manifest_hash = fingerprint(manifest(snap.files))
        elif field == "expiry":
            row.expires_at = clock(db) + timedelta(days=1)
        else:
            row.authorization_digest = None  # Legacy rows never gain invented consent.
    if queued:
        box = FixtureSandbox()
        app.coding.process_commands(box)
        assert box.calls == []
        assert app.coding.command_result(env["actor"], cmd["command_id"])["state"] == "rejected"
    else:
        with pytest.raises(DomainError) as error:
            confirm(env, cmd)
        assert error.value.code in {"APPROVAL_CHANGED", "APPROVAL_UNBOUND"}


@pytest.mark.parametrize("queued", [False, True])
def test_environment_rollout_requires_new_approval(env, queued):
    _, cmd = command(env)
    if queued:
        confirm(env, cmd)
    env["app"].db.settings = replace(env["app"].db.settings, sandbox_image="sha256:" + "3" * 64)
    if queued:
        box = FixtureSandbox()
        env["app"].coding.process_commands(box)
        assert box.calls == []
    else:
        with pytest.raises(DomainError) as error:
            confirm(env, cmd)
        assert error.value.code == "ENVIRONMENT_CHANGED"


def test_old_identical_content_revision_cannot_supply_submission_receipt(env):
    app, actor = env["app"], env["actor"]
    ws, cmd = command(env)
    confirm(env, cmd)
    app.coding.process_commands(FixtureSandbox())
    app.coding.save(actor, ws["workspace_id"], 1, "main.py", "print(2)\n")
    app.coding.save(actor, ws["workspace_id"], 2, "main.py", "print(1)\n")
    submission = app.coding.submit(
        actor,
        ws["workspace_id"],
        3,
        "My explanation",
        "Transfer differences",
        "none_declared",
        "same-content",
    )
    with app.db.read() as db:
        assert db.get(Submission, submission["submission_id"]).execution_receipt_ids == []
    with pytest.raises(DomainError) as error:
        app.coding.review_submission(
            actor,
            submission["submission_id"],
            {"artifact_and_runtime": True, "explanation": True, "transfer": True},
            "REVIEW",
        )
    assert error.value.code == "RUNTIME_EVIDENCE"


def test_receipt_envelope_is_bound_and_does_not_apply_output_changes(env):
    app, actor = env["app"], env["actor"]
    ws, cmd = command(env)

    class ChangedFixture(FixtureSandbox):
        def execute(self, execution, argv):
            value = super().execute(execution, argv)
            value.update(
                files={"main.py": "print(2)\n"},
                input_hash="forged",
                workspace_revision=99,
                authorization_digest="forged",
                argv_hash="forged",
                synthetic=False,
            )
            # The supervisor retains its own copy even if the adapter mutates input.
            execution["files"]["main.py"] = "tampered by fixture"
            return value

    confirm(env, cmd)
    app.coding.process_commands(ChangedFixture())
    result = app.coding.command_result(actor, cmd["command_id"])["receipt"]
    assert result["input_hash"] == ws["manifest_hash"] and result["workspace_revision"] == 1
    assert result["argv_hash"] == fingerprint(cmd["argv"]) and result["synthetic"] is True
    assert result["authorization_digest"] == cmd["authorization_digest"]
    assert result["image_digest"] == cmd["image_digest"] and result["observed_at"]
    assert result["actual_changes"][0]["path"] == "main.py"
    assert app.coding.detail(actor, ws["workspace_id"])["files"] == {"main.py": "print(1)\n"}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"exit_code": True, "stdout": "", "stderr": "", "files": {}},
        {"exit_code": 0, "stdout": "", "stderr": "", "files": {"../escape": "x"}},
    ],
)
def test_invalid_sandbox_receipts_are_unknown_not_completed(env, bad):
    _, cmd = command(env)

    class BadFixture(FixtureSandbox):
        def execute(self, execution, argv):
            return deepcopy(bad)

    confirm(env, cmd)
    env["app"].coding.process_commands(BadFixture())
    result = env["app"].coding.command_result(env["actor"], cmd["command_id"])
    assert result["state"] == "unknown" and result["receipt"]["automatic_retry"] is False


def test_another_owner_cannot_inspect_or_confirm_or_apply(env):
    app = env["app"]
    ws, cmd = command(env)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "other-coding-owner")
        other_id = other.id
    for action in [
        lambda: app.coding.detail(other_id, ws["workspace_id"]),
        lambda: app.coding.save(other_id, ws["workspace_id"], 1, "main.py", "changed"),
        lambda: app.coding.confirm_command(
            other_id, cmd["command_id"], cmd["approval_nonce"], "RUN"
        ),
        lambda: app.coding.command_result(other_id, cmd["command_id"]),
        lambda: app.coding.apply(other_id, ws["workspace_id"], "unknown", 1, "APPLY"),
    ]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 404


async def test_patch_from_failed_run_cannot_be_applied(env):
    app, actor = env["app"], env["actor"]
    ws = app.coding.create_workspace(actor)
    run_id = app.coding.start(
        actor, ws["workspace_id"], 1, [], "Fixture coding", env["funding"], 1, "failed-patch"
    )
    await app.drain()
    with app.db.transaction() as db:
        p = db.scalar(select(PatchProposal).where(PatchProposal.run_id == run_id))
        pid = p.id
        db.get(AgentRun, run_id).status = "failed"
    with pytest.raises(DomainError) as error:
        app.coding.apply(actor, ws["workspace_id"], pid, 1, "APPLY")
    assert error.value.code == "PATCH_UNVERIFIED"


async def test_modified_patch_row_is_not_the_reviewed_reply(env):
    app, actor = env["app"], env["actor"]
    ws = app.coding.create_workspace(actor)
    run_id = app.coding.start(
        actor, ws["workspace_id"], 1, [], "Fixture coding", env["funding"], 1, "mutated-patch"
    )
    await app.drain()
    with app.db.transaction() as db:
        p = db.scalar(select(PatchProposal).where(PatchProposal.run_id == run_id))
        pid = p.id
        p.edits = [{"path": "other.py", "before": None, "after": 'print("not reviewed")'}]
    with pytest.raises(DomainError) as error:
        app.coding.apply(actor, ws["workspace_id"], pid, 1, "APPLY")
    assert error.value.code == "PATCH_UNVERIFIED"


@pytest.mark.parametrize("argv", [None, "python", {}, [None], [["python"]], ["python", 1]])
def test_non_argv_input_is_a_domain_error(env, argv):
    ws = env["app"].coding.create_workspace(env["actor"])
    with pytest.raises(DomainError) as error:
        env["app"].coding.propose_command(env["actor"], ws["workspace_id"], 1, argv)
    assert error.value.code == "COMMAND_SCOPE"


def test_submission_idempotency_cannot_hide_a_different_revision(env):
    app, actor = env["app"], env["actor"]
    ws = app.coding.create_workspace(actor, {"main.py": "print(1)"})
    app.coding.submit(
        actor, ws["workspace_id"], 1, "Explanation", "Transfer", "none_declared", "frozen"
    )
    app.coding.save(actor, ws["workspace_id"], 1, "main.py", "print(2)")
    with pytest.raises(DomainError) as error:
        app.coding.submit(
            actor, ws["workspace_id"], 2, "Explanation", "Transfer", "none_declared", "frozen"
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_revoked_reference_prevents_new_patch_application(env):
    from conftest import publish_fixture

    from careerforge.models import ObjectRow

    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    ws = app.coding.create_workspace(actor, publication_ids=[pub])
    run_id = app.coding.start(
        actor, ws["workspace_id"], 1, [], "Fixture coding", env["funding"], 1, "revoked-source"
    )
    await app.drain()
    with app.db.transaction() as db:
        p = db.scalar(select(PatchProposal).where(PatchProposal.run_id == run_id))
        pid = p.id
        db.get(ObjectRow, pub).state = "revoked"
    with pytest.raises(DomainError):
        app.coding.apply(actor, ws["workspace_id"], pid, 1, "APPLY")
    assert app.coding.detail(actor, ws["workspace_id"])["revision"] == 1


@pytest.mark.parametrize(
    "files", [[], {"bad.py": None}, {"bad.py": "\ud800"}, {"a": "file", "a/b.py": "nested"}]
)
def test_workspace_rejects_non_utf8_or_unmaterializable_files(env, files):
    with pytest.raises(DomainError):
        env["app"].coding.create_workspace(env["actor"], files)
