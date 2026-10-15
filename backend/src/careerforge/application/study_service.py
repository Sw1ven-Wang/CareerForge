"""Owner-scoped, versioned Agent study pipeline; runtime I/O lives in workflows."""

from uuid import UUID

from sqlalchemy import select

from careerforge.application import knowledge_service as knowledge
from careerforge.application import repository_service as repositories
from careerforge.application.common import check_revision, idempotency
from careerforge.application.ownership import owned
from careerforge.application.run_service import create_run
from careerforge.application.studio_learning_service import read_requirements, validate_requirements
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.study_models import StudyArtifact, StudyTrack

VERSION = "agent-study-2.0"
ROLE_TYPES = {
    "extract": "market_research",
    "discover": "project_research",
    "review": "project_research",
    "plan": "learning_path",
    "tutor": "learning_path",
}


def track(db, owner, identifier, lock=False):
    row = owned(db, StudyTrack, owner, identifier, lock=lock)
    read_requirements(db, owner, row.payload["source_snapshot_ids"])
    return row


def artifact_row(db, owner, track_id, identifier, kind=None):
    row = owned(db, StudyArtifact, owner, UUID(str(identifier)))
    if row.track_id != track_id or (kind and row.kind != kind):
        raise BusinessError(404, "NOT_FOUND", "学习产物不存在。")
    return row


def put(db, row, kind, payload, run_id=None):
    item = StudyArtifact(
        owner_id=row.owner_id, track_id=row.id, kind=kind, payload=payload, run_id=run_id
    )
    db.add(item)
    db.flush()
    return item


def packed(row):
    return {
        "id": str(row.id),
        "kind": row.kind,
        "run_id": str(row.run_id) if row.run_id else None,
        "created_at": row.created_at.isoformat(),
        **row.payload,
    }


def current_requirements(db, row):
    identifier = row.payload.get("requirements_id")
    if not identifier:
        raise BusinessError(409, "EXTRACT_REQUIRED", "请先提取技术需求。")
    return artifact_row(db, row.owner_id, row.id, identifier, "requirements")


def current_reviews(db, row):
    requirements_id = row.payload.get("requirements_id")
    rows = db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.track_id == row.id,
            StudyArtifact.owner_id == row.owner_id,
            StudyArtifact.kind == "review",
        )
        .order_by(StudyArtifact.created_at.desc(), StudyArtifact.id)
    ).all()
    seen, values = set(), []
    for item in rows:
        value = item.payload
        if value.get("requirements_id") != requirements_id:
            continue
        url = value["candidate"]["github_url"]
        if url in seen:
            continue
        seen.add(url)
        values.append(item)
    return values


def approved(db, row):
    values = []
    for item in current_reviews(db, row):
        if not item.payload.get("admitted"):
            continue
        try:
            repositories.snapshot(db, row.owner_id, UUID(item.payload["repository_snapshot_id"]))
            for point in item.payload["highlights"]:
                repositories.citation_detail(db, row.owner_id, UUID(point["citation_id"]))
        except BusinessError:
            continue
        values.append(item)
    return values


def idle(db, row):
    identifier = row.payload.get("active_run_id")
    run = db.get(AgentRun, UUID(identifier)) if identifier else None
    if run and run.status in {"queued", "running", "waiting_user"}:
        raise BusinessError(409, "STUDY_RUN_ACTIVE", "此方向已有 Agent 运行，请等待或取消。")


def create(db, owner, body, key):
    sources = read_requirements(db, owner, body.source_snapshot_ids)
    record = idempotency(db, owner, "study.create", key, body.model_dump(mode="json"))
    if record.response_summary:
        return detail(db, track(db, owner, UUID(record.response_summary["track_id"])))
    row = StudyTrack(
        owner_id=owner,
        name=body.name,
        payload={
            "source_snapshot_ids": [s["source_snapshot_id"] for s in sources],
            "target_count": body.target_count,
            "progress": {},
        },
    )
    db.add(row)
    db.flush()
    record.response_summary = {"track_id": str(row.id)}
    return detail(db, row)


def listing(db, owner):
    values = []
    for row in db.scalars(
        select(StudyTrack)
        .where(StudyTrack.owner_id == owner)
        .order_by(StudyTrack.created_at.desc())
        .limit(100)
    ):
        try:
            track(db, owner, row.id)
            values.append(
                {
                    "track_id": str(row.id),
                    "name": row.name,
                    "revision": row.revision,
                    "available": True,
                }
            )
        except BusinessError:
            values.append(
                {"track_id": str(row.id), "name": "来源已撤销的学习方向", "available": False}
            )
    return values


def visible_review(db, owner, item, accepted_ids):
    payload = item.payload
    identifier = payload.get("repository_snapshot_id") or payload["candidate"].get(
        "repository_snapshot_id"
    )
    available = not payload.get("admitted") or str(item.id) in accepted_ids
    if identifier:
        try:
            repositories.snapshot(db, owner, UUID(identifier))
        except BusinessError:
            available = False
    if available:
        return {**packed(item), "available": True}
    return {
        "id": str(item.id),
        "candidate_id": payload["candidate_id"],
        "available": False,
        "candidate": {"full_name": "项目已撤销"},
        "verdict": "uncertain",
        "admitted": False,
        "summary": "项目依据不可用，已隐藏派生内容。",
        "technology_stack": [],
        "quality_notes": [],
        "highlights": [],
        "limitations": [],
    }


def detail(db, row):
    # Reauthorize before returning derived private text, even on idempotent replay.
    sources = read_requirements(db, row.owner_id, row.payload["source_snapshot_ids"])
    items = list(
        db.scalars(
            select(StudyArtifact)
            .where(StudyArtifact.track_id == row.id, StudyArtifact.owner_id == row.owner_id)
            .order_by(StudyArtifact.created_at, StudyArtifact.id)
        )
    )
    reviews = current_reviews(db, row)
    accepted = approved(db, row)
    accepted_ids = {str(i.id) for i in accepted}
    current_id = row.payload.get("requirements_id")
    candidates = [
        packed(i)
        for i in items
        if i.kind == "candidate" and i.payload.get("requirements_id") == current_id
    ]
    requirements = next((packed(i) for i in items if str(i.id) == current_id), None)
    plans, turns = [], []
    for item in items:
        if item.kind not in {"plan", "tutor"}:
            continue
        if item.payload.get("requirements_id") != current_id:
            continue
        if not set(item.payload.get("review_ids", [])) <= accepted_ids:
            continue
        (plans if item.kind == "plan" else turns).append(packed(item))
    run = (
        db.get(AgentRun, UUID(row.payload["active_run_id"]))
        if row.payload.get("active_run_id")
        else None
    )
    return {
        "track_id": str(row.id),
        "name": row.name,
        "revision": row.revision,
        "sources": [{k: v for k, v in s.items() if k != "content"} for s in sources],
        "target_count": row.payload["target_count"],
        "approved_count": len(accepted),
        "requirements": requirements,
        "candidates": candidates,
        "reviews": [visible_review(db, row.owner_id, i, accepted_ids) for i in reviews],
        "approved_projects": [packed(i) for i in accepted],
        "plans": plans,
        "turns": turns,
        "progress": row.payload.get("progress", {}),
        "active_run": {
            "run_id": str(run.id),
            "status": run.status,
            "role": run.input_manifest["role"],
            "version": run.version,
        }
        if run
        else None,
    }


def configure(db, owner, identifier, body):
    row = track(db, owner, identifier, True)
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    row.payload = {**row.payload, "target_count": body.target_count}
    row.revision += 1
    return detail(db, row)


def add_candidate(db, owner, identifier, body, key):
    row = track(db, owner, identifier, True)
    record = idempotency(
        db, owner, f"study.candidate:{identifier}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return detail(db, row)
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    requirements = current_requirements(db, row)
    project = knowledge.project_detail(db, owner, body.repository_snapshot_id)
    existing = db.scalar(
        select(StudyArtifact.id)
        .where(
            StudyArtifact.track_id == row.id,
            StudyArtifact.kind == "candidate",
            StudyArtifact.payload["requirements_id"].astext == str(requirements.id),
            StudyArtifact.payload["github_url"].astext == project["github_url"],
        )
        .limit(1)
    )
    if existing:
        raise BusinessError(409, "CANDIDATE_EXISTS", "此仓库已在当前候选中，无需重复加入。")
    payload = {
        "requirements_id": str(requirements.id),
        "repository_snapshot_id": str(body.repository_snapshot_id),
        "full_name": project["github_url"].removeprefix("https://github.com/"),
        "github_url": project["github_url"],
        "commit": project["commit"],
        "name": project["name"],
        "origin": "user_import",
        "description": project.get("description", ""),
        "license": project["license_status"],
        "archived": None,
        "fork": None,
    }
    item = put(db, row, "candidate", payload)
    row.revision += 1
    record.response_summary = {"study_artifact_id": str(item.id)}
    return detail(db, row)


def validate_plan_scope(db, row, plan):
    if plan.payload["requirements_id"] != row.payload.get("requirements_id"):
        raise BusinessError(409, "STUDY_PLAN_STALE", "技术需求已更新，请重新制定计划。")
    valid_ids = {str(i.id) for i in approved(db, row)}
    if not set(plan.payload["review_ids"]) <= valid_ids:
        raise BusinessError(409, "STUDY_PROJECT_UNAVAILABLE", "计划引用的审核项目已不可用。")


def start(db, owner, identifier, body, key):
    row = track(db, owner, identifier, True)
    record = idempotency(db, owner, f"study.run:{identifier}", key, body.model_dump(mode="json"))
    if record.response_summary:
        run = owned(db, AgentRun, owner, UUID(record.response_summary["run_id"]))
        validate_run(db, run)
        return record.response_summary
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    source_values = read_requirements(db, owner, row.payload["source_snapshot_ids"])
    manifest = {
        "track_id": str(row.id),
        "role": body.role,
        "source_snapshot_ids": [s["source_snapshot_id"] for s in source_values],
        "sources": source_values,
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": min(body.max_output_tokens, settings.model_max_output_tokens),
        "model_call_limit": (
            body.model_call_limit
            if body.role == "tutor"
            else 1 + body.auto_review_count
            if body.role == "discover"
            else 1
        ),
        "auto_review_count": body.auto_review_count if body.role == "discover" else 0,
        "synthetic": False,
    }
    if body.role != "extract":
        requirements = current_requirements(db, row)
        manifest.update(
            requirements_id=str(requirements.id), requirements=requirements.payload["requirements"]
        )
    if body.role == "discover":
        manifest["research_feedback"] = [
            {
                "repository": i.payload["candidate"]["github_url"],
                "verdict": i.payload["verdict"],
                "summary": i.payload["summary"][:400],
                "admitted": i.payload["admitted"],
            }
            for i in current_reviews(db, row)[:15]
            if visible_review(db, owner, i, {str(r.id) for r in approved(db, row)})["available"]
        ]
    if body.role == "review":
        candidate = (
            artifact_row(db, owner, row.id, body.candidate_id, "candidate")
            if body.candidate_id
            else None
        )
        if not candidate or candidate.payload["requirements_id"] != str(requirements.id):
            raise BusinessError(422, "CANDIDATE_REQUIRED", "请选择当前需求版本的候选项目。")
        manifest["candidate_id"] = str(candidate.id)
        manifest["candidate"] = candidate.payload
    if body.role == "plan":
        accepted = approved(db, row)
        if len(accepted) < row.payload["target_count"]:
            raise BusinessError(
                409,
                "PROJECT_TARGET_NOT_MET",
                f"已审核入库 {len(accepted)} 个，目标 {row.payload['target_count']} 个。"
                "请继续研究或调整目标。",
            )
        if not body.intent:
            raise BusinessError(422, "LEARNING_INTENT_REQUIRED", "请选择学习意图、技术需求与项目。")
        selected = {str(i) for i in body.intent.project_review_ids}
        selected_requirements = set(body.intent.requirement_keys)
        if not selected <= {str(i.id) for i in accepted} or not selected_requirements <= {
            i["key"] for i in manifest["requirements"]
        }:
            raise BusinessError(422, "INVALID_PLAN_SCOPE", "所选技术或项目不属于当前审核版本。")
        manifest["intent"] = body.intent.model_dump(mode="json")
        manifest["projects"] = [packed(i) for i in accepted if str(i.id) in selected]
        if body.intent.focus_chunk_id:
            from pathlib import PurePosixPath

            from careerforge.application.study_validation import CODE_SUFFIXES

            chunk = knowledge.chunk_detail(db, owner, body.intent.focus_chunk_id)
            snapshots = {p["repository_snapshot_id"] for p in manifest["projects"]}
            if (
                chunk["repository_snapshot_id"] not in snapshots
                or PurePosixPath(chunk["path"]).suffix.lower() not in CODE_SUFFIXES
            ):
                raise BusinessError(
                    422, "FOCUS_OUTSIDE_PROJECT", "请选择所选审核项目中的源码片段。"
                )
            manifest["focus_chunk"] = {
                k: chunk[k] for k in ("chunk_id", "repository_snapshot_id", "content_hash")
            }
        # Every chosen technical goal needs at least one actual code match.
        covered = {h["requirement_key"] for p in manifest["projects"] for h in p["highlights"]}
        if not selected_requirements <= covered:
            raise BusinessError(
                422, "REQUIREMENT_COVERAGE_MISSING", "部分所选技术尚无审核通过的代码依据。"
            )
    if body.role == "tutor":
        if not body.plan_id or not body.module_key or not body.question.strip():
            raise BusinessError(422, "TUTOR_INPUT_REQUIRED", "请选择计划模块并输入问题。")
        plan = artifact_row(db, owner, row.id, body.plan_id, "plan")
        validate_plan_scope(db, row, plan)
        module = next((m for m in plan.payload["modules"] if m["key"] == body.module_key), None)
        if not module:
            raise BusinessError(422, "MODULE_NOT_FOUND", "计划模块不存在。")
        reviews = [
            artifact_row(db, owner, row.id, i, "review") for i in module["project_review_ids"]
        ]
        history = db.scalars(
            select(StudyArtifact)
            .where(
                StudyArtifact.track_id == row.id,
                StudyArtifact.kind == "tutor",
                StudyArtifact.payload["plan_id"].astext == str(plan.id),
                StudyArtifact.payload["module_key"].astext == body.module_key,
            )
            .order_by(StudyArtifact.created_at.desc())
            .limit(3)
        ).all()
        manifest.update(
            plan_id=str(plan.id),
            module=module,
            question=body.question,
            review_ids=[str(i.id) for i in reviews],
            scope=[i.payload["repository_snapshot_id"] for i in reviews],
            history=[
                {
                    "question": i.payload["question"][:1000],
                    "answer_excerpt": " ".join(b.get("text", "") for b in i.payload["blocks"])[
                        :2500
                    ],
                    "previous_points": [
                        {"title": p["title"], "explanation": p["explanation"][:500]}
                        for p in i.payload["points"][:3]
                    ],
                    "history_truncated": True,
                }
                for i in reversed(history)
            ],
        )
    run = create_run(
        db,
        owner,
        ROLE_TYPES[body.role],
        VERSION,
        manifest,
        call_budget=manifest["model_call_limit"],
    )
    row.payload = {**row.payload, "active_run_id": str(run.id)}
    row.revision += 1
    response = {
        "run_id": str(run.id),
        "status": run.status,
        "role": body.role,
        "model_call_limit": manifest["model_call_limit"],
        "max_output_tokens": manifest["max_output_tokens"],
        "track_revision": row.revision,
    }
    record.response_summary = response
    return response


def validate_run(db, run):
    row = track(db, run.owner_id, UUID(run.input_manifest["track_id"]))
    manifest = run.input_manifest
    validate_requirements(db, run.owner_id, manifest["sources"])
    if (
        manifest.get("requirements_id")
        and row.payload.get("requirements_id") != manifest["requirements_id"]
    ):
        raise BusinessError(409, "STUDY_REQUIREMENTS_CHANGED", "需求版本已变化。")
    if manifest.get("focus_chunk"):
        chunk = knowledge.chunk_detail(db, run.owner_id, UUID(manifest["focus_chunk"]["chunk_id"]))
        if chunk["content_hash"] != manifest["focus_chunk"]["content_hash"]:
            raise BusinessError(409, "FOCUS_CHANGED", "所选实现索引已变化。")
    if manifest.get("candidate", {}).get("repository_snapshot_id"):
        repositories.snapshot(
            db, run.owner_id, UUID(manifest["candidate"]["repository_snapshot_id"])
        )
    if manifest["role"] in {"plan", "tutor"}:
        valid = {str(i.id) for i in approved(db, row)}
        identifiers = (
            [p["id"] for p in manifest["projects"]]
            if manifest["role"] == "plan"
            else manifest["review_ids"]
        )
        if not set(identifiers) <= valid:
            raise BusinessError(409, "STUDY_PROJECT_UNAVAILABLE", "审核项目已不可用。")
    return row


def progress(db, owner, identifier, body):
    row = track(db, owner, identifier, True)
    check_revision(row.revision, body.expected_revision)
    plan = artifact_row(db, owner, row.id, body.plan_id, "plan")
    validate_plan_scope(db, row, plan)
    module = next((m for m in plan.payload["modules"] if m["key"] == body.module_key), None)
    if not module:
        raise BusinessError(422, "MODULE_NOT_FOUND", "模块不存在。")
    values = dict(row.payload.get("progress", {}))
    key = f"{plan.id}:{body.module_key}"
    if body.state == "completed":
        if any(
            values.get(f"{plan.id}:{k}", {}).get("state") != "completed"
            for k in module["prerequisite_keys"]
        ):
            raise BusinessError(409, "PREREQUISITE_PENDING", "请先完成此模块的前置模块。")
    values[key] = {
        "state": body.state,
        "note": body.note,
        "reported_by": "user",
        "competence_verified": False,
    }
    row.payload = {**row.payload, "progress": values}
    row.revision += 1
    return detail(db, row)
