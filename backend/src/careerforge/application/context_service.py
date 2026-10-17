"""Owner-scoped context references. Summaries cannot extend scope or establish competence."""

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.application.ownership import owned
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.context.learning_skills import SKILLS
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt, StageArtifact
from careerforge.infrastructure.db.study_models import StudyArtifact
from careerforge.schemas.context import ContextReadArgs, ContextTaskContract, EvidencePointer

TOOLS = ["search_code", "read_code", "related_code", "read_context_evidence", "load_learning_skill"]
TUTOR_VERSION = "agent-study-2.3"


def attempt_data(db: Session, owner: UUID, item: StudyArtifact) -> Json:
    from careerforge.application import studio_delivery_service as delivery
    from careerforge.application.study_practice_service import spec_current

    spec = spec_current(db, owner, item.payload["exercise_spec_id"])
    data = delivery.detail(
        db, owner, UUID(item.payload["workspace_id"]), UUID(item.payload["practice_id"])
    )
    saved = data["submission"]
    return {
        "attempt_id": str(item.id),
        "exercise_spec_id": str(spec.id),
        "exercise_version": spec.payload["version"],
        "exercise_hash": spec.payload["content_hash"],
        "learning_evidence": data["learning_evidence"],
        "submission": {
            k: saved.get(k)
            for k in (
                "submission_id",
                "version_no",
                "summary",
                "code_version",
                "explanation_answers",
                "transfer_notes",
                "execution_records",
                "study_evidence",
            )
        }
        if saved
        else None,
        "assessment": data["assessment"],
        "source": "same_account_review_and_declarations",
        "competence_verified": False,
    }


def source_data(db: Session, owner: UUID, manifest: Json, pointer: Json) -> Json:
    from careerforge.application import study_service as study

    if pointer["kind"] == "skill":
        skill = SKILLS.get(pointer["source_id"])
        if skill is None:
            raise BusinessError(404, "NOT_FOUND", "学习方法不存在。")
        return skill
    item = study.artifact_row(db, owner, UUID(manifest["track_id"]), pointer["source_id"])
    if item.kind == "tutor" and pointer["kind"] == "tutor_turn":
        if (
            item.payload["plan_id"] != manifest["plan_id"]
            or item.payload["module_key"] != manifest["module"]["key"]
            or item.payload["requirements_id"] != manifest["requirements_id"]
            or not set(item.payload["review_ids"]) <= set(manifest["review_ids"])
        ):
            raise BusinessError(404, "NOT_FOUND", "历史回答不在当前模块范围。")
        return dict(item.payload)
    if item.kind == "attempt" and pointer["kind"] == "attempt":
        from careerforge.application.study_practice_service import spec_current

        spec = spec_current(db, owner, item.payload["exercise_spec_id"])
        if spec.payload["module_hash"] != fingerprint(manifest["module"]):
            raise BusinessError(404, "NOT_FOUND", "练习不在当前模块版本范围。")
        return attempt_data(db, owner, item)
    raise BusinessError(404, "NOT_FOUND", "证据类型不匹配。")


def capture(db: Session, owner: UUID, manifest: Json) -> Json:
    catalog: list[Json] = []
    learning: list[Json] = []
    recent = db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.owner_id == owner,
            StudyArtifact.track_id == UUID(manifest["track_id"]),
            StudyArtifact.kind == "tutor",
            StudyArtifact.payload["plan_id"].astext == manifest["plan_id"],
            StudyArtifact.payload["module_key"].astext == manifest["module"]["key"],
        )
        .order_by(StudyArtifact.created_at.desc(), StudyArtifact.id.desc())
        .limit(8)
    ).all()
    for item in reversed(recent):
        if item.payload.get("requirements_id") != manifest["requirements_id"]:
            continue
        summary = (
            item.payload["question"][:180]
            + " | "
            + " ".join(b.get("text", "") for b in item.payload["blocks"])[:180]
        )
        catalog.append(
            EvidencePointer(
                evidence_id="turn:" + str(item.id),
                kind="tutor_turn",
                source_id=str(item.id),
                version=manifest["plan_id"],
                content_hash=fingerprint(item.payload),
                summary=summary,
                verification="prior_model_answer_unverified",
            ).model_dump(mode="json")
        )
    candidates = db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.owner_id == owner,
            StudyArtifact.track_id == UUID(manifest["track_id"]),
            StudyArtifact.kind == "attempt",
            StudyArtifact.payload["module_key"].astext == manifest["module"]["key"],
        )
        .order_by(StudyArtifact.created_at.desc(), StudyArtifact.id.desc())
        .limit(5)
    ).all()
    for item in reversed(candidates):
        pointer = {"kind": "attempt", "source_id": str(item.id)}
        try:
            value = source_data(db, owner, manifest, pointer)
        except BusinessError:
            continue
        if not value["submission"]:
            continue
        summary = (
            "练习 v"
            + str(value["exercise_version"])
            + "；"
            + value["learning_evidence"]["learning_state"]
        )
        ref = EvidencePointer(
            evidence_id="attempt:" + str(item.id),
            kind="attempt",
            source_id=str(item.id),
            version=value["exercise_hash"],
            content_hash=fingerprint(value),
            summary=summary,
            verification="same_account_observation_not_competence",
        ).model_dump(mode="json")
        catalog.append(ref)
        learning.append(
            {
                "evidence_id": ref["evidence_id"],
                "summary": summary,
                "competence_verified": False,
                "source": ref["verification"],
            }
        )
    for name, skill in SKILLS.items():
        catalog.append(
            EvidencePointer(
                evidence_id="skill:" + name,
                kind="skill",
                source_id=name,
                version=skill["version"],
                content_hash=fingerprint(skill),
                summary=skill["title"],
                verification="procedure_not_evidence",
            ).model_dump(mode="json")
        )
    task = ContextTaskContract(
        task_id=uuid4(),
        requirements_id=manifest["requirements_id"],
        plan_id=manifest["plan_id"],
        module_key=manifest["module"]["key"],
        module_hash=fingerprint(manifest["module"]),
        scope=manifest["scope"],
        allowed_tools=TOOLS,
        model_call_limit=manifest["model_call_limit"],
        max_output_tokens=manifest["max_output_tokens"],
        stop_conditions=[
            "user_cancel",
            "lease_lost",
            "source_changed",
            "budget_exhausted",
            "no_evidence",
        ],
    ).model_dump(mode="json")
    return {"task": task, "catalog": catalog, "learning": learning}


def validate_catalog(db: Session, owner: UUID, manifest: Json) -> None:
    frozen = manifest.get("context_scope")
    if not frozen:
        return
    task = ContextTaskContract.model_validate(frozen["task"])
    if (
        task.module_hash != fingerprint(manifest["module"])
        or str(task.plan_id) != manifest["plan_id"]
        or str(task.requirements_id) != manifest["requirements_id"]
        or task.module_key != manifest["module"]["key"]
        or task.model_call_limit != manifest["model_call_limit"]
        or task.max_output_tokens != manifest["max_output_tokens"]
        or task.scope != manifest["scope"]
        or task.allowed_tools != TOOLS
    ):
        raise BusinessError(409, "CONTEXT_SCOPE_CHANGED", "上下文任务范围已变化。")
    for ref in frozen["catalog"]:
        pointer = EvidencePointer.model_validate(ref).model_dump(mode="json")
        value = source_data(db, owner, manifest, pointer)
        if fingerprint(value) != pointer["content_hash"]:
            raise BusinessError(409, "CONTEXT_EVIDENCE_CHANGED", "上下文证据已变更，请开始新问题。")


def recall(db: Session, owner: UUID, manifest: Json, args: ContextReadArgs) -> Json:
    frozen = manifest.get("context_scope", {})
    ref = next((r for r in frozen.get("catalog", []) if r["evidence_id"] == args.evidence_id), None)
    if not ref:
        raise BusinessError(404, "NOT_FOUND", "证据未列入本轮授权目录。")
    value = source_data(db, owner, manifest, ref)
    if fingerprint(value) != ref["content_hash"]:
        raise BusinessError(409, "CONTEXT_EVIDENCE_CHANGED", "证据版本或观察已变化，请重新提问。")
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    if args.offset >= len(text) and args.offset:
        raise BusinessError(422, "EVIDENCE_OFFSET_INVALID", "补读位置超出证据范围。")
    end = min(len(text), args.offset + args.limit)
    links: list[Json] = []
    if ref["kind"] == "tutor_turn":
        from careerforge.application import knowledge_service as knowledge
        from careerforge.application import repository_service as repositories
        from careerforge.application.studio_learning_service import brief

        for source in value.get("sources", [])[:4]:
            if source["repository_snapshot_id"] not in manifest["scope"]:
                continue
            actual = repositories.citation_detail(db, owner, UUID(source["citation_id"]))
            if actual["content_hash"] != source["content_hash"]:
                raise BusinessError(409, "CONTEXT_EVIDENCE_CHANGED", "历史代码引用已变化。")
            if source.get("chunk_id"):
                chunk = knowledge.chunk_detail(db, owner, UUID(source["chunk_id"]))
                links.append({**brief(chunk), "citation_id": source["citation_id"]})
    return {
        "evidence_id": args.evidence_id,
        "source_id": ref["source_id"],
        "kind": ref["kind"],
        "version": ref["version"],
        "content_hash": ref["content_hash"],
        "verification": ref["verification"],
        "offset": args.offset,
        "next_offset": end,
        "total_chars": len(text),
        "truncated": end < len(text),
        "content": text[args.offset : end],
        "code_candidates": links,
        "competence_verified": False,
    }


def context_run(db: Session, owner: UUID, track_id: UUID, run_id: UUID) -> AgentRun:
    from careerforge.application import study_service as study

    run = owned(db, AgentRun, owner, run_id)
    if (
        run.input_manifest.get("track_id") != str(track_id)
        or run.input_manifest.get("role") != "tutor"
        or not run.input_manifest.get("context_scope")
    ):
        raise BusinessError(404, "NOT_FOUND", "此运行没有可回溯上下文。")
    study.validate_run(db, run)
    return run


def detail(db: Session, owner: UUID, track_id: UUID, run_id: UUID) -> Json:
    run = context_run(db, owner, track_id, run_id)
    stages = db.scalars(
        select(StageArtifact)
        .where(
            StageArtifact.run_id == run_id,
            StageArtifact.stage_version == run.workflow_version,
            StageArtifact.input_fingerprint == run.input_manifest_hash,
            StageArtifact.stage_key.like("context:%"),
        )
        .order_by(StageArtifact.created_at, StageArtifact.id)
    ).all()
    receipts = db.scalars(select(ModelCallReceipt).where(ModelCallReceipt.run_id == run_id)).all()
    return {
        "run_id": str(run.id),
        "status": run.status,
        "task": run.input_manifest["context_scope"]["task"],
        "catalog": run.input_manifest["context_scope"]["catalog"],
        "packets": [
            {
                "stage_key": s.stage_key,
                **{
                    k: s.payload[k]
                    for k in (
                        "manifest",
                        "request_hash",
                        "prompt_hash",
                        "prompt_chars",
                        "estimated_input_tokens",
                    )
                },
            }
            for s in stages
        ],
        "provider_usage": [
            {"status": r.status, "request_hash": r.request_hash, "usage": r.usage} for r in receipts
        ],
        "measurement_note": "UTF-8字节保守估计与供应商真实计量分别记录；不保证供应商强制遵守上限。",
        "extra_model_calls": 0,
    }
