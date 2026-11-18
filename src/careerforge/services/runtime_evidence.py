"""Owner-bound execution evidence, shared by candidates and authorized human review.

Only retained server command/snapshot/submission records are authority. A submission
quote, nonempty receipt ID, or caller's ``synthetic=False`` is never that authority.
Historical environment identity is verified against its original authorization, not
against the deployment's current sandbox image.
"""

from datetime import datetime
from typing import Any

from ..db import aware
from ..errors import DomainError
from ..models import CommandAuthorization, ExecutionSnapshot, Submission, Workspace
from ..util import fingerprint, manifest
from .coding import CodingService
from .privacy import assert_personal_access


def runtime_evidence_in(db, attempt) -> list[dict[str, Any]]:
    """Read-only proof kernel; caller separately authorizes assessment or review."""
    actor = attempt.principal_id
    assert_personal_access(db, actor)
    ids = attempt.evidence.get("execution_receipt_ids", [])
    if (
        not isinstance(ids, list)
        or any(not isinstance(item, str) for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise DomainError("RUNTIME_EVIDENCE", "运行回执身份无效。", 409)
    if not ids:
        return []
    submission = db.get(Submission, attempt.submission_id) if attempt.submission_id else None
    workspace = db.get(Workspace, attempt.workspace_id)
    if (
        submission is None
        or workspace is None
        or submission.revoked
        or workspace.principal_id != actor
        or submission.principal_id != actor
        or submission.workspace_id != attempt.workspace_id
        or submission.code_hash != fingerprint(manifest(submission.files))
        or submission.workspace_revision != attempt.evidence.get("workspace_revision")
        or submission.code_hash != attempt.evidence.get("workspace_manifest")
        or not set(ids) <= set(submission.execution_receipt_ids)
    ):
        raise DomainError("RUNTIME_EVIDENCE", "运行回执不属于有效冻结提交。", 409)
    receipts = []
    for identifier in ids:
        command = db.get(CommandAuthorization, identifier, populate_existing=True)
        execution = db.get(ExecutionSnapshot, command.execution_snapshot_id) if command else None
        receipt = command.receipt if command else None
        if (
            command is None
            or execution is None
            or command.principal_id != actor
            or command.state != "completed"
            or not isinstance(receipt, dict)
            or not command.authorization_digest
            or execution.workspace_id != attempt.workspace_id
            or execution.workspace_revision != submission.workspace_revision
            or execution.manifest_hash != submission.code_hash
            or execution.manifest_hash != fingerprint(manifest(execution.files))
            or command.argv_hash != fingerprint(command.argv)
            or command.authorization_digest != CodingService._command_digest(command, execution)
            or receipt.get("authorization_digest") != command.authorization_digest
            or receipt.get("input_hash") != submission.code_hash
            or receipt.get("workspace_revision") != submission.workspace_revision
            or receipt.get("execution_snapshot_id") != execution.id
            or receipt.get("workspace_id") != execution.workspace_id
            or receipt.get("environment_version") != execution.environment_version
            or receipt.get("image_digest") != execution.image_digest
            or receipt.get("argv_hash") != command.argv_hash
            or command.started_at is None
            or aware(command.started_at) > aware(command.expires_at)
        ):
            raise DomainError("RUNTIME_EVIDENCE", "运行回执已不可核验。", 409)
        try:
            observed = datetime.fromisoformat(receipt["observed_at"])
            if aware(observed) < aware(command.started_at):
                raise ValueError("observation precedes execution")
            CodingService._argv(command.argv)
            CodingService._files(execution.files)
        except (KeyError, TypeError, ValueError, DomainError) as exc:
            raise DomainError("RUNTIME_EVIDENCE", "运行授权或观察时间已不可核验。", 409) from exc
        synthetic = receipt.get("synthetic") is not False
        # These are the fields produced by the trusted supervisor's real Docker
        # branch. Merely changing a fixture's synthetic flag cannot pass this gate.
        if not synthetic and (
            receipt.get("runtime") != "docker-network-none"
            or type(receipt.get("exit_code")) is not int
            or type(receipt.get("timed_out")) is not bool
            or not isinstance(receipt.get("stdout"), str)
            or not isinstance(receipt.get("stderr"), str)
            or not isinstance(receipt.get("files"), dict)
            or receipt.get("help_condition") != "system_observed_only"
        ):
            raise DomainError("RUNTIME_EVIDENCE", "真实执行缺少可信运行观察链。", 409)
        receipts.append(
            {
                "receipt_id": identifier,
                "exit_code": receipt.get("exit_code"),
                "execution_snapshot_id": execution.id,
                "environment_version": execution.environment_version,
                "synthetic": synthetic,
                "runtime_observed_not_mastery": not synthetic,
            }
        )
    return receipts
