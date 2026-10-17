"""Autonomous proposals become executable only after owner, scope and budget checks."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import assistance_service as assistance
from careerforge.application import delegation_service as delegation
from careerforge.application import repository_service as repositories
from careerforge.application import review_batch_service as batches
from careerforge.application import study_service as study
from careerforge.application.common import check_revision, digest, fingerprint, idempotency
from careerforge.application.coverage_service import evidence_verification
from careerforge.application.run_service import artifact, create_run, db_now, owned_run
from careerforge.application.studio_learning_service import read_requirements, validate_requirements
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.domain.runs import TERMINAL
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt
from careerforge.infrastructure.db.study_models import StudyTrack
from careerforge.schemas.delegation import (
    DelegateArgs,
    DelegatedTaskContract,
    DelegationInput,
    TaskOutput,
)

VERSION = "agent-delegation-1.0"
CHILD_VERSION = "agent-delegation-task-1.0"
ROLE_TOOLS = {
    "requirement_analysis": [],
    "code_investigation": batches.READ_TOOLS,
    "evidence_review": batches.READ_TOOLS,
    "learning_design": batches.READ_TOOLS,
}


def source_readings(sources: list[Json], requirements: list[Json]) -> list[Json]:
    windows: dict[str, Json] = {}
    scoped_ids = {e["source_snapshot_id"] for r in requirements for e in r.get("evidence", [])}
    for source in sources:
        if scoped_ids and source["source_snapshot_id"] not in scoped_ids:
            continue
        content = source["content"]
        quotes = [
            e["quote"]
            for r in requirements
            for e in r.get("evidence", [])
            if e["source_snapshot_id"] == source["source_snapshot_id"]
            and content.count(e["quote"]) == 1
        ]
        spans = [
            (max(0, content.index(q) - 120), min(len(content), content.index(q) + len(q) + 120))
            for q in quotes[:8]
        ] or [(0, min(1600, len(content)))]
        for first, last in spans:
            identifier = (
                "source-"
                + fingerprint(
                    {"source": source["source_snapshot_id"], "first": first, "last": last}
                )[:24]
            )
            windows[identifier] = {
                "kind": "source",
                "ref_id": identifier,
                "source_snapshot_id": source["source_snapshot_id"],
                "source_title": source["title"],
                "source_hash": source["content_hash"],
                "offset": first,
                "content": content[first:last],
                "content_hash": digest(content[first:last]),
                "truncated": first > 0 or last < len(content),
            }
    return list(windows.values())


def start(
    db: Session, owner: UUID, identifier: UUID, body: DelegationInput, key: str | None
) -> Json:
    record = idempotency(
        db, owner, f"study.delegation:{identifier}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        validate_run(db, owned_run(db, owner, UUID(record.response_summary["run_id"])))
        return record.response_summary
    row = study.track(db, owner, identifier, True)
    check_revision(row.revision, body.expected_revision)
    assistance.guard_agent(db, owner)
    study.idle(db, row)
    config = get_settings()
    if not config.real_agent_enabled or not config.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    requirement = study.current_requirements(db, row)
    selected = [r for r in requirement.payload["requirements"] if r["key"] in body.requirement_keys]
    if set(body.requirement_keys) != {r["key"] for r in selected}:
        raise BusinessError(422, "TASK_SCOPE_FORBIDDEN", "请选择当前需求版本中的技术目标。")
    candidates = [batches.frozen_candidate(db, owner, row.id, c) for c in body.candidate_ids]
    if any(c["candidate"]["requirements_id"] != str(requirement.id) for c in candidates):
        raise BusinessError(409, "STUDY_REQUIREMENTS_CHANGED", "候选不属于当前需求版本。")
    sources = read_requirements(db, owner, row.payload["source_snapshot_ids"])
    manifest = {
        "role": "delegate",
        "track_id": str(row.id),
        "requirements_id": str(requirement.id),
        "requirements_hash": fingerprint(requirement.payload),
        "requirements": selected,
        "sources": sources,
        "source_readings": source_readings(sources, selected),
        "objective": body.objective,
        "candidates": candidates,
        "model": config.model_name,
        "base_url": config.openai_base_url,
        "max_output_tokens": min(body.max_output_tokens, config.study_max_output_tokens),
        "parent_call_limit": body.parent_call_limit,
        "delegation_scope": {
            "allowed_roles": body.allowed_roles,
            "max_children": 2,
            "max_depth": 1,
            "child_call_limit": 3,
            "tools": ["delegate_task"],
        },
        "automatic_retry": False,
    }
    run = create_run(
        db,
        owner,
        "project_research",
        VERSION,
        manifest,
        call_budget=body.call_budget,
        deadline_at=db_now(db) + timedelta(minutes=15),
    )
    row.payload = {**row.payload, "active_run_id": str(run.id)}
    row.revision += 1
    result = {
        "run_id": str(run.id),
        "status": run.status,
        "role": "delegate",
        "call_limit": run.call_budget,
        "parent_call_limit": body.parent_call_limit,
        "max_output_tokens": manifest["max_output_tokens"],
        "track_revision": row.revision,
    }
    record.response_summary = result
    return result


def child_manifest(parent: AgentRun, proposal: DelegateArgs) -> Json:
    m = parent.input_manifest
    selected = [r for r in m["requirements"] if r["key"] in proposal.requirement_keys]
    if (
        proposal.role not in m["delegation_scope"]["allowed_roles"]
        or not selected
        or set(proposal.requirement_keys) != {r["key"] for r in selected}
        or len(set(proposal.requirement_keys)) != len(proposal.requirement_keys)
    ):
        raise BusinessError(422, "TASK_SCOPE_FORBIDDEN", "子任务角色或技术目标不在用户授权范围内。")
    frozen = next(
        (c for c in m["candidates"] if c["candidate_id"] == str(proposal.candidate_id)), None
    )
    if proposal.candidate_id and not frozen:
        raise BusinessError(422, "TASK_SCOPE_FORBIDDEN", "子任务不能读取授权范围之外的项目。")
    if proposal.role == "code_investigation" and not frozen:
        raise BusinessError(422, "TASK_SCOPE_FORBIDDEN", "代码调查须选择已授权的固定快照。")
    if proposal.role == "requirement_analysis" and proposal.candidate_id:
        raise BusinessError(422, "TASK_SCOPE_FORBIDDEN", "需求分析角色不授予源码工具。")
    if not parent.deadline_at:
        raise ValueError("A delegated task needs a fixed deadline")
    result = {
        "role": "delegated_task",
        "task_role": proposal.role,
        "track_id": m["track_id"],
        "requirements_id": m["requirements_id"],
        "requirements": selected,
        "objective": proposal.objective,
        "source_readings": source_readings(m["sources"], selected),
        "candidate": frozen["candidate"] if frozen else None,
        "model": m["model"],
        "base_url": m["base_url"],
        "max_output_tokens": m["max_output_tokens"],
        "model_call_limit": proposal.call_limit,
        "delegation_scope": {
            "parent_run_id": str(parent.id),
            "proposal": proposal.model_dump(mode="json"),
            "requirement_version": m["requirements_id"],
            "snapshot_hash": frozen["snapshot_hash"] if frozen else None,
            "source_refs": [r["ref_id"] for r in source_readings(m["sources"], selected)],
            "max_output_tokens": m["max_output_tokens"],
            "allowed_tools": ROLE_TOOLS[proposal.role] if frozen else [],
            "context_refs": [m["requirements_id"], *([frozen["candidate_id"]] if frozen else [])],
            "deadline": parent.deadline_at.isoformat(),
            "max_depth": 0,
            "call_limit": proposal.call_limit,
            "output_schema": "TaskOutput",
            "stop_conditions": [
                "parent_stopped",
                "deadline",
                "budget",
                "source_changed",
                "no_new_evidence",
            ],
        },
    }

    result["delegation_scope"] = DelegatedTaskContract.model_validate(
        result["delegation_scope"]
    ).model_dump(mode="json")
    return result


def validate_run(db: Session, run: AgentRun) -> StudyTrack:
    if run.input_manifest.get("_revoked"):
        raise BusinessError(404, "NOT_FOUND", "输入已撤销。")
    parent = db.get(AgentRun, run.parent_run_id) if run.parent_run_id else run
    if not parent or parent.owner_id != run.owner_id or parent.workflow_version != VERSION:
        raise BusinessError(404, "NOT_FOUND", "委派范围不可用。")
    m = parent.input_manifest
    if m.get("_revoked"):
        raise BusinessError(404, "NOT_FOUND", "父任务输入已撤销。")
    row = study.track(db, run.owner_id, UUID(m["track_id"]))
    validate_requirements(db, run.owner_id, m["sources"])
    requirement = study.current_requirements(db, row)
    if (
        str(requirement.id) != m["requirements_id"]
        or fingerprint(requirement.payload) != m["requirements_hash"]
    ):
        raise BusinessError(409, "STUDY_REQUIREMENTS_CHANGED", "需求版本已变化。")
    if parent.status not in TERMINAL:
        batches.validate_parent_scope(db, parent)
        if assistance.active_independent(db, run.owner_id):
            raise BusinessError(409, "INDEPENDENT_ATTEMPT_ACTIVE", "独立尝试期间暂停自主委派。")
    else:
        # History also rechecks the fixed snapshots, but a past deadline does not erase a report.
        for frozen in m["candidates"]:
            current = batches.frozen_candidate(
                db, run.owner_id, row.id, UUID(frozen["candidate_id"])
            )
            if fingerprint(current) != fingerprint(frozen):
                raise BusinessError(409, "CHILD_SCOPE_CHANGED", "固定源码或候选已变化。")
    if run.parent_run_id:
        if run.status not in TERMINAL:
            delegation.parent_guard(db, run)
        proposal = DelegateArgs.model_validate(run.input_manifest["delegation_scope"]["proposal"])
        if fingerprint(child_manifest(parent, proposal)) != run.input_manifest_hash:
            raise BusinessError(409, "CHILD_SCOPE_CHANGED", "子任务权限或范围与合同不一致。")
    return row


def safe_to_continue(db: Session, root: AgentRun) -> None:
    identifiers = [
        root.id,
        *db.scalars(select(AgentRun.id).where(AgentRun.parent_run_id == root.id)).all(),
    ]
    for receipt in db.scalars(
        select(ModelCallReceipt).where(ModelCallReceipt.run_id.in_(identifiers))
    ):
        if receipt.status == "unknown":
            raise BusinessError(409, "MODEL_RESULT_UNCERTAIN", "存在未知模型请求，停止后续调用。")
        value = receipt.usage or {}
        if (value.get("output_tokens") or 0) > root.input_manifest["max_output_tokens"]:
            raise BusinessError(
                422, "PROVIDER_OUTPUT_OVER_LIMIT", "供应商报告超限，停止整个委派任务。"
            )


def checked_output(run: AgentRun, output: TaskOutput, readings: list[Json]) -> Json:
    allowed = {r["ref_id"]: r for r in readings}
    findings, evidence = [], {}
    for finding in output.findings:
        refs = []
        for pointer in finding.evidence:
            source = allowed.get(pointer.ref_id)
            if not source or source["content"].count(pointer.quote) != 1:
                raise ValueError("Task evidence must uniquely quote an actually read source")
            offset = source["content"].index(pointer.quote)
            identifier = (
                "evidence-"
                + fingerprint({"run": str(run.id), "ref": pointer.ref_id, "quote": pointer.quote})[
                    :24
                ]
            )
            item = {k: v for k, v in source.items() if k not in {"content", "offset"}}
            item.update(
                {
                    "evidence_id": identifier,
                    "quote": pointer.quote,
                    "verification": evidence_verification(),
                }
            )
            if source["kind"] == "source":
                item["quote_start"] = source["offset"] + offset
                item["quote_end"] = item["quote_start"] + len(pointer.quote)
            else:
                item["start_line"] = source["start_line"] + source["content"][:offset].count("\n")
                item["end_line"] = item["start_line"] + pointer.quote.count("\n")
            evidence[identifier] = item
            refs.append(identifier)
        findings.append({"statement": finding.statement, "evidence_ids": refs})
    return {
        "summary": output.summary,
        "findings": findings,
        "evidence": list(evidence.values()),
        "unresolved": output.unresolved,
        "counterevidence": output.counterevidence,
        "stop_reason": output.stop_reason,
        "semantic_support": "not_independently_verified",
    }


def recheck_evidence(db: Session, root: AgentRun, evidence: list[Json]) -> None:
    source_map = {s["source_snapshot_id"]: s for s in root.input_manifest["sources"]}
    snapshots = {
        c["candidate"]["repository_snapshot_id"] for c in root.input_manifest["candidates"]
    }
    for point in evidence:
        if point["kind"] == "source":
            source = source_map.get(point["source_snapshot_id"])
            if (
                not source
                or source["content_hash"] != point["source_hash"]
                or source["content"][point["quote_start"] : point["quote_end"]] != point["quote"]
            ):
                raise ValueError("Source evidence changed")
        else:
            if point["repository_snapshot_id"] not in snapshots:
                raise ValueError("Code evidence is outside the root scope")
            span = repositories.read_lines(
                db,
                root.owner_id,
                UUID(point["repository_snapshot_id"]),
                point["path"],
                point["start_line"],
                point["end_line"],
            )
            if (
                span["file_hash"] != point["file_hash"]
                or span["content"].count(point["quote"]) != 1
            ):
                raise ValueError("Code evidence changed")


def handoffs(db: Session, root: AgentRun) -> list[Json]:
    values = []
    children = db.scalars(
        select(AgentRun).where(AgentRun.parent_run_id == root.id).order_by(AgentRun.id)
    )
    for child in children:
        if child.status not in TERMINAL:
            continue
        saved = artifact(db, child, "result", CHILD_VERSION)
        result: Json = (
            saved.payload
            if saved
            else {
                "summary": "子任务无可用结果",
                "findings": [],
                "evidence": [],
                "unresolved": ["子任务未完成"],
                "counterevidence": [],
                "stop_reason": child.status,
            }
        )
        recheck_evidence(db, root, result["evidence"])
        values.append(
            {
                "run_id": str(child.id),
                "task_key": child.input_manifest["delegation_scope"]["proposal"]["task_key"],
                "task_role": child.input_manifest["task_role"],
                "status": child.status,
                **result,
                "consumed_calls": child.calls_reserved,
            }
        )
    return values


def reports(db: Session, row: StudyTrack) -> list[Json]:
    from careerforge.infrastructure.db.study_models import StudyArtifact

    results = []
    items = db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.track_id == row.id,
            StudyArtifact.owner_id == row.owner_id,
            StudyArtifact.kind == "delegation",
        )
        .order_by(StudyArtifact.created_at.desc())
        .limit(10)
    )
    for item in items:
        run = db.get(AgentRun, item.run_id)
        if not run or item.payload.get("requirements_id") != row.payload.get("requirements_id"):
            continue
        try:
            validate_run(db, run)
            recheck_evidence(db, run, item.payload["evidence"])
        except (BusinessError, ValueError):
            continue
        results.append(study.packed(item))
    return results
