"""Narrow presentation repair and zero-call recovery of metered Pi proposals."""

from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, fingerprint
from careerforge.application.run_service import artifact, event, owned_run
from careerforge.application.studio_service import (
    VERSION,
    detail,
    guard_workspace_run,
    save_reply,
    validate_context,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import ModelCallReceipt, StageArtifact
from careerforge.schemas.model_io import ModelResult, Usage
from careerforge.schemas.studio import AgentReply

RECOVERY_VERSION = "pi-reply-format-recovery-1.0"


def normalize_reply_arguments(arguments: Json) -> tuple[Json, list[Json]]:
    """Move one unambiguous edit reason; never change code, paths or command arguments."""
    edits = arguments.get("edits")
    if (
        set(arguments) == {"answer", "citation_ids", "edits", "commands", "reason"}
        and isinstance(arguments["reason"], str)
        and bool(arguments["reason"].strip())
        and isinstance(edits, list)
        and len(edits) == 1
        and isinstance(edits[0], dict)
        and set(edits[0]) == {"path", "before", "after"}
    ):
        normalized = deepcopy(arguments)
        normalized["edits"][0]["reason"] = normalized.pop("reason")
        return normalized, [{"operation": "move", "from": "/reason", "to": "/edits/0/reason"}]
    return deepcopy(arguments), []


def recover_saved_reply(db: Session, owner: UUID, run_id: UUID, expected_revision: int) -> Json:
    """Explicit operator recovery; keep terminal status, raw responses and call counts intact.

    Publication still passes the original source/base/ownership gates. This never
    applies a patch, executes a command, requeues a task or calls a model.
    """
    from careerforge.application import assistance_service as assistance
    from careerforge.application.maintenance_service import authorized

    run = owned_run(db, owner, run_id)
    if run.workflow_version != VERSION or run.status not in {"failed", "budget_stopped"}:
        raise BusinessError(409, "PI_RECOVERY_UNAVAILABLE", "只能恢复已停止的 Pi 编码结果。")
    authorized(db, run)
    previous = artifact(db, run, "pi_reply_recovery", RECOVERY_VERSION)
    if previous:
        from careerforge.application.studio_service import workspace

        return detail(workspace(db, owner, UUID(run.input_manifest["workspace_id"])), db)
    row = validate_context(db, run)
    check_revision(row.revision, expected_revision)
    assistance.guard_agent(db, owner, row)
    guard_workspace_run(db, owner, row)
    if any(item.get("run_id") == str(run.id) for item in row.payload["actions"]):
        raise BusinessError(409, "PI_REPLY_ALREADY_PUBLISHED", "此运行已有输出，不能再次发布。")
    receipts = list(
        db.scalars(
            select(ModelCallReceipt)
            .where(ModelCallReceipt.run_id == run.id)
            .order_by(ModelCallReceipt.created_at, ModelCallReceipt.id)
        )
    )
    if not receipts or len(receipts) != run.calls_reserved or run.calls_reserved > run.call_budget:
        raise BusinessError(409, "PI_RECOVERY_UNCERTAIN", "模型调用收据不完整。")
    results = []
    saved = None
    for index, receipt in enumerate(receipts, 1):
        saved = artifact(db, run, f"model_result:{index}", VERSION)
        result = ModelResult.model_validate(saved.payload) if saved else None
        if (
            result is None
            or receipt.status != "completed"
            or result.finish_reason not in {"stop", "tool_calls"}
            or result.usage.status != "known"
            or result.usage.output_tokens is None
            or result.usage.output_tokens > run.input_manifest["max_output_tokens"]
            or receipt.usage != result.usage.model_dump()
        ):
            raise BusinessError(409, "PI_RECOVERY_UNCERTAIN", "未知、超限或未计量的结果不能恢复。")
        results.append(result)
    final = results[-1]
    if (
        final.finish_reason != "tool_calls"
        or len(final.tool_calls) != 1
        or final.tool_calls[0].name != "submit_reply"
    ):
        raise BusinessError(409, "PI_RECOVERY_NO_REPLY", "最后的已保存响应没有唯一的最终建议。")
    original = final.tool_calls[0].arguments
    normalized, repairs = normalize_reply_arguments(original)
    if not repairs:
        raise BusinessError(409, "PI_RECOVERY_NO_SAFE_REPAIR", "没有可确定修复的展示字段。")
    reply = AgentReply.model_validate(normalized)
    if len(reply.edits) > 2:
        raise BusinessError(422, "PI_EDIT_LIMIT", "一次审阅最多两份小文件修改。")
    usage = Usage(
        status="known",
        input_tokens=sum(item.usage.input_tokens or 0 for item in results),
        output_tokens=sum(item.usage.output_tokens or 0 for item in results),
    )
    record = StageArtifact(
        id=uuid4(),
        run_id=run.id,
        stage_key="pi_reply_recovery",
        stage_version=RECOVERY_VERSION,
        input_fingerprint=run.input_manifest_hash,
        payload={
            "source_stage_artifact_id": str(saved.id) if saved else None,
            "original_status": run.status,
            "repairs": repairs,
            "raw_arguments_hash": fingerprint(original),
            "normalized_arguments_hash": fingerprint(normalized),
            "extra_model_calls": 0,
            "review_required": True,
        },
    )
    reply.answer += (
        "\n（已保存响应仅修复修改原因的字段层级；未追加模型调用。"
        "原始预算停止记录保留，补丁待审、命令待确认。）"
    )
    first = len(row.payload["actions"])
    save_reply(
        db,
        run,
        ModelResult(
            provider="pi-sdk",
            finish_reason="stop",
            structured=reply.model_dump(),
            usage=usage,
            synthetic=all(item.synthetic for item in results),
        ),
    )
    row.payload = {
        **row.payload,
        "actions": [
            *row.payload["actions"][:first],
            *[
                {
                    **item,
                    "run_id": str(run.id),
                    "recovery": {
                        "artifact_id": str(record.id),
                        "extra_model_calls": 0,
                        "original_status": run.status,
                    },
                }
                for item in row.payload["actions"][first:]
            ],
        ],
    }
    db.add(record)
    event(
        db,
        run,
        "coding.reply_recovered",
        {"artifact_id": str(record.id), "extra_model_calls": 0, "original_status": run.status},
    )
    return detail(row, db)
