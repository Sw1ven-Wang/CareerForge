"""Owner-scoped protocol storage and frozen M1 -> M2 -> M3 handoff."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from careerforge.application.common import digest, fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.domain.errors import BusinessError, not_found
from careerforge.domain.job_normalization import NORMALIZER_VERSION, adapt, sections
from careerforge.domain.job_requirements import (
    aggregate,
    constraints,
    extract,
    validate_expression,
)
from careerforge.infrastructure.db.models import (
    CareerGoalVersion,
    MarketReport,
    Source,
    SourceSnapshot,
)
from careerforge.infrastructure.db.models import JobContractArtifact as Artifact
from careerforge.schemas.job_contract import (
    CaptureImport,
    JobPostingSnapshotV1,
    PlanningContextV1,
    RequirementFactV1,
    validate_requirement_against_snapshot,
)

VERSION = "job-contract-1.1"


def read(db: Session, owner: UUID, identifier: UUID, kind=None):
    row = db.get(Artifact, identifier)
    if (
        not row
        or row.owner_id != owner
        or (kind and row.kind != kind)
        or row.payload.get("_revoked")
    ):
        raise not_found()
    if row.source_id:
        from careerforge.application.source_service import get_snapshot, get_source

        get_source(db, owner, row.source_id)
        if row.source_snapshot_id:
            get_snapshot(db, owner, row.source_id, row.source_snapshot_id)
    if row.parent_id:
        read(db, owner, row.parent_id)
    if row.kind == "report":
        for i in row.payload["input_manifest"]["job_snapshot_ids"]:
            read(db, owner, UUID(i), "snapshot")
        for i in row.payload["input_manifest"]["fact_ids"]:
            read(db, owner, UUID(i), "fact")
    if row.kind == "context":
        from careerforge.application.planning_service import evidence_version

        for i in row.payload["context"]["personal_evidence_version_ids"]:
            evidence_version(db, owner, UUID(i))
        goal = owned(db, CareerGoalVersion, owner, UUID(row.payload["context"]["goal_version_id"]))
        context_without_hash = {
            k: v for k, v in row.payload["context"].items() if k != "input_fingerprint_sha256"
        }
        frozen = row.payload["fingerprint_manifest"]
        if frozen["context"] != context_without_hash or frozen["goal_hash"] != goal.content_hash:
            raise BusinessError(409, "CONTEXT_CHANGED", "上下文与冻结输入不一致。")
        if frozen["report_hash"] != fingerprint(read(db, owner, row.parent_id, "report").payload):
            raise BusinessError(409, "CONTEXT_CHANGED", "报告与冻结输入不一致。")
        for i in row.payload["context"]["personal_evidence_version_ids"]:
            if frozen["evidence_hashes"][i] != fingerprint(
                evidence_version(db, owner, UUID(i)).payload
            ):
                raise BusinessError(409, "CONTEXT_CHANGED", "个人材料与冻结输入不一致。")
        if row.payload["context"]["input_fingerprint_sha256"] != fingerprint(
            row.payload["fingerprint_manifest"]
        ):
            raise BusinessError(409, "CONTEXT_CHANGED", "规划上下文摘要不一致。")
    return row


def result(row):
    return {"contract_artifact_id": str(row.id), "kind": row.kind, **row.payload}


def add_capture(db, owner, source, snapshot, raw, value, legacy=False, collected=None):
    capture_id = uuid4()
    job_id = uuid4()
    provider = collected["channel"] if collected else value.provider
    try:
        job, canonical, external, url, metadata = (
            collected["adapted"]
            if collected
            else adapt(provider, raw, value.title, value.employer_name)
        )
    except (ValueError, TypeError, KeyError) as error:
        raise BusinessError(
            422, "INVALID_PROVIDER_PAYLOAD", "来源字段格式不合法，请选择单个岗位对象。"
        ) from error
    now = datetime.now(UTC).isoformat()
    status = value.read_status
    if value.http_status and value.http_status >= 400:
        status = "unavailable"
    if not canonical and status == "full_text":
        status = "metadata_only"
    if value.usage_policy_status == "restricted":
        status = "unavailable"
    if status in {"unavailable", "metadata_only"}:
        canonical = ""
    raw_bytes = collected["raw_bytes"] if collected else raw.encode("utf-8")
    capture = {
        "schema_version": "careerforge.source_capture.v1",
        "source_capture_id": str(capture_id),
        "raw_snapshot_id": str(capture_id),
        "provider": provider,
        "board_scope": value.board_scope,
        "ingest_method": "manual_import"
        if provider == "manual_import"
        else "json_ld"
        if provider == "json_ld"
        else "official_api",
        "source_url": value.source_url or url,
        "effective_url": None,
        "external_posting_id": external,
        "retrieved_at": now,
        "read_status": status,
        "payload_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_blob_key": str(capture_id),
        "raw_format": "text"
        if provider == "manual_import"
        else "html"
        if provider == "json_ld" and raw.lstrip().startswith("<")
        else "json",
        "http_status": value.http_status,
        "usage_policy_status": value.usage_policy_status,
        "adapter_version": NORMALIZER_VERSION,
        "capture_method": "legacy_canonical_backfill" if legacy else "user_supplied_saved_payload",
        "original_raw_recoverable": not legacy,
        "provider_metadata": metadata,
        "limits": {"max_raw_bytes": 1000000, "network_access": False},
        "error_kind": "source_unavailable" if status == "unavailable" else None,
    }
    if collected:
        capture.update(
            ingest_method=collected["ingest_method"],
            effective_url=collected["effective_url"],
            raw_format=collected["raw_format"],
            adapter_version=collected["adapter_version"],
            capture_method="anonymous_public_fetch",
            limits={"max_raw_bytes": 1000000, "network_access": True},
        )
    db.add(
        Artifact(
            id=capture_id,
            owner_id=owner,
            kind="capture",
            identity_key=str(capture_id),
            source_id=source.id,
            source_snapshot_id=snapshot.id if snapshot else None,
            payload=capture,
            raw_bytes=raw_bytes,
        )
    )
    db.flush()
    identity = (
        (provider + ":" + value.board_scope + ":" + external)
        if external and value.board_scope
        else "url:" + str(value.source_url or url)
        if value.source_url or url
        else "manual:" + str(source.id)
    )
    posting_id = str(uuid5(NAMESPACE_URL, str(owner) + ":" + identity))
    quality = {
        "normalization_status": "ready"
        if status == "full_text" and canonical
        else "partial"
        if canonical
        else "blocked",
        "normalizer_version": NORMALIZER_VERSION,
        "dedupe_status": "unique",
        "dedupe_group_id": None,
        "issues": ["原始字节不可恢复；当前仅保留历史规范文本，不进入正式分母"]
        if legacy
        else ["公开岗位原文采集；在招状态以来源当次显示为准，要求仍需人工确认"]
        if collected
        else ["离线适配器；字段为用户提交的已保存响应，不构成官方采集证明"],
    }
    normalized = JobPostingSnapshotV1.model_validate(
        {
            "schema_version": "careerforge.job_snapshot.v1",
            "posting_id": posting_id,
            "job_snapshot_id": str(job_id),
            "source": {
                "source_capture_id": str(capture_id),
                "raw_snapshot_id": str(capture_id),
                "provider": provider,
                "ingest_method": capture["ingest_method"],
                "source_url": value.source_url or url,
                "external_posting_id": external,
                "retrieved_at": now,
                "read_status": status,
            },
            "job": job,
            "document": {"canonical_text": canonical, "sections": sections(canonical)},
            "quality": quality,
        }
    )
    source_key = (str(snapshot.id) if snapshot else str(source.id)) + ":" + NORMALIZER_VERSION
    row = Artifact(
        id=job_id,
        owner_id=owner,
        kind="snapshot",
        identity_key=source_key,
        parent_id=capture_id,
        source_id=source.id,
        source_snapshot_id=snapshot.id if snapshot else None,
        payload={
            "snapshot": normalized.model_dump(mode="json"),
            "content_hash": digest(canonical),
            "source_id": str(source.id),
            "source_snapshot_id": str(snapshot.id) if snapshot else None,
        },
    )
    db.add(row)
    db.flush()
    if normalized.quality.normalization_status == "ready":
        for fact in extract(normalized):
            db.add(
                Artifact(
                    id=UUID(fact.requirement_id),
                    owner_id=owner,
                    kind="fact",
                    identity_key=fact.requirement_id,
                    parent_id=job_id,
                    source_id=source.id,
                    source_snapshot_id=snapshot.id if snapshot else None,
                    payload={"fact": fact.model_dump(mode="json"), "review": None},
                )
            )
        db.flush()
    return row


def from_legacy(db, owner, source, snapshot, raw=None):
    existing = db.scalar(
        select(Artifact).where(
            Artifact.owner_id == owner,
            Artifact.kind == "snapshot",
            Artifact.identity_key == str(snapshot.id) + ":" + NORMALIZER_VERSION,
        )
    )
    if existing:
        return existing
    payload = raw if raw is not None else snapshot.content
    value = CaptureImport(
        provider="manual_import",
        title=snapshot.metadata_values.get("title"),
        source_url=source.source_url,
        raw_payload=payload,
        read_status="full_text" if raw is not None else "partial_text",
    )
    return add_capture(db, owner, source, snapshot, payload, value, legacy=raw is None)


def import_capture(db, owner, value, key):
    record = idempotency(db, owner, "job-contract.import", key, value.model_dump(mode="json"))
    if record.response_summary:
        read(db, owner, UUID(record.response_summary["contract_artifact_id"]))
        return record.response_summary
    from careerforge.application.source_library_service import assign, lock_library

    lock_library(db, owner)
    try:
        job, body, external, url, _ = adapt(
            value.provider, value.raw_payload, value.title, value.employer_name
        )
    except (ValueError, KeyError, TypeError) as error:
        raise BusinessError(
            422, "INVALID_PROVIDER_PAYLOAD", "不能解析来源，请导入单个岗位对象。"
        ) from error
    if len(value.raw_payload.encode("utf-8")) > 1000000 or len(body) > 100000:
        raise BusinessError(413, "SOURCE_TOO_LARGE", "原始数据或规范正文超出限制。")
    source_url = value.source_url or url
    if source_url and not str(source_url).startswith(("https://", "http://")):
        raise BusinessError(
            422, "INVALID_SOURCE_URL", "来源地址必须为 HTTP(S)，本接口不会访问该地址。"
        )
    identity = (
        (value.provider + ":" + value.board_scope + ":" + external)
        if external and value.board_scope
        else "url:" + source_url
        if source_url
        else "manual:" + str(uuid4())
    )
    hashed = digest(identity)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int(digest(str(owner) + hashed)[:15], 16)},
    )
    source = db.scalar(
        select(Source)
        .where(
            Source.owner_id == owner, Source.identity_hash == hashed, Source.revoked_at.is_(None)
        )
        .with_for_update()
    )
    if source is None:
        source = Source(
            owner_id=owner,
            identity_hash=hashed,
            source_url=source_url,
            source_type="job_posting",
            title=value.title or job["title_raw"],
            import_mode="text",
        )
        db.add(source)
        db.flush()
    else:
        source.revision += 1
    if (
        value.read_status in {"unavailable", "metadata_only"}
        or value.usage_policy_status == "restricted"
        or (value.http_status and value.http_status >= 400)
    ):
        body = ""
    snapshot = SourceSnapshot(
        owner_id=owner,
        source_id=source.id,
        version_no=source.revision,
        content=body,
        content_hash=digest(body),
        metadata_hash=fingerprint(value.model_dump(mode="json", exclude={"raw_payload"})),
        metadata_values={
            "source_type": "job_posting",
            "title": source.title,
            "job_category": value.job_category,
            "source_category": value.source_category,
            "declared_provenance": "用户提供的离线来源响应；不是平台联网采集",
        },
    )
    db.add(snapshot)
    db.flush()
    from careerforge.application.import_service import materialize_job

    materialize_job(db, snapshot)
    row = add_capture(db, owner, source, snapshot, value.raw_payload, value)
    assign(db, owner, source, value.job_category, value.source_category)
    record.response_summary = result(row)
    return record.response_summary


def facts_for(db, owner, job_id, current=True):
    read(db, owner, job_id, "snapshot")
    rows = list(
        db.scalars(
            select(Artifact)
            .where(
                Artifact.owner_id == owner, Artifact.kind == "fact", Artifact.parent_id == job_id
            )
            .order_by(Artifact.id)
        )
    )
    superseded = {r.supersedes_id for r in rows if r.supersedes_id}
    return [r for r in rows if not current or r.id not in superseded]


def revise_fact(db, owner, identifier, value, key):
    record = idempotency(
        db, owner, f"job-contract.fact.{identifier}", key, value.model_dump(mode="json")
    )
    previous = read(db, owner, identifier, "fact")
    db.scalar(select(Artifact).where(Artifact.id == previous.parent_id).with_for_update())
    if record.response_summary:
        read(db, owner, UUID(record.response_summary["contract_artifact_id"]), "fact")
        return record.response_summary
    if value.expected_fact_id != str(previous.id) or db.scalar(
        select(Artifact.id).where(Artifact.supersedes_id == previous.id)
    ):
        raise BusinessError(409, "VERSION_CONFLICT", "此要求已有更新，请重新读取。")
    corrected = value.correction
    if corrected.requirement_id != str(previous.id):
        raise BusinessError(422, "FACT_ID_MISMATCH", "修订必须引用当前要求身份。")
    parent = read(db, owner, previous.parent_id, "snapshot")
    snapshot = JobPostingSnapshotV1.model_validate(parent.payload["snapshot"])
    try:
        validate_expression(corrected.expression.model_dump(mode="json"))
        validate_requirement_against_snapshot(corrected, snapshot)
    except ValueError as error:
        raise BusinessError(422, "INVALID_REQUIREMENT", str(error)) from error
    from careerforge.domain.job_requirements import concept_leaves, parse_concepts

    supported = {c for c, _ in concept_leaves(parse_concepts(corrected.evidence.quote) or {})}
    if corrected.origin == "explicit" and any(
        c not in supported for c, _ in concept_leaves(corrected.expression.model_dump(mode="json"))
    ):
        raise BusinessError(422, "UNSUPPORTED_CONCEPT", "明确要求中的概念必须出现在引用原文中。")
    original = RequirementFactV1.model_validate(previous.payload["fact"])
    if (
        corrected.taxonomy_version != original.taxonomy_version
        or corrected.extractor_version != original.extractor_version
    ):
        raise BusinessError(
            422, "EXTRACTION_VERSION_MISMATCH", "人工修订不能改写自动抽取器或字典版本。"
        )
    if corrected.origin == "explicit" and original.origin == "inferred":
        raise BusinessError(422, "ORIGIN_REVIEW_REQUIRED", "不能直接将推断项转换成明确原文。")
    identifier = uuid4()
    payload = corrected.model_dump(mode="json")
    payload["requirement_id"] = str(identifier)
    row = Artifact(
        id=identifier,
        owner_id=owner,
        kind="fact",
        identity_key=str(identifier),
        parent_id=parent.id,
        supersedes_id=previous.id,
        source_id=previous.source_id,
        source_snapshot_id=previous.source_snapshot_id,
        payload={
            "fact": payload,
            "review": {
                "old_fact_id": str(previous.id),
                "new_fact_id": str(identifier),
                "reviewer_id": str(owner),
                "reason": value.reason,
                "reviewed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    db.add(row)
    db.flush()
    record.response_summary = result(row)
    return record.response_summary


def job_for_source(db, owner, snapshot_id):
    row = db.scalar(
        select(Artifact).where(
            Artifact.owner_id == owner,
            Artifact.kind == "snapshot",
            Artifact.source_snapshot_id == snapshot_id,
        )
    )
    return read(db, owner, row.id, "snapshot") if row else None


def start_market(db, owner, body, key):
    record = idempotency(db, owner, "job-contract.market", key, body.model_dump(mode="json"))
    target = owned(db, CareerGoalVersion, owner, body.goal_version_id)
    from careerforge.application.market_report_service import inputs

    rows = inputs(db, owner, [str(i) for i in body.source_snapshot_ids])
    if len(set(body.source_snapshot_ids)) != len(body.source_snapshot_ids):
        raise BusinessError(422, "DUPLICATE_INPUT", "输入快照不可重复。")
    jobs = []
    for row in rows:
        if row.metadata_values.get("source_type") == "job_posting":
            job = job_for_source(db, owner, row.id)
            if not job:
                source = db.get(Source, row.source_id)
                job = from_legacy(db, owner, source, row)
            jobs.append(job)
    if not jobs:
        raise BusinessError(422, "NO_READABLE_JD", "至少选择一个岗位快照。")
    if record.response_summary:
        return record.response_summary
    from careerforge.application.run_service import create_run

    manifest = {
        "goal_version_id": str(target.id),
        "source_snapshot_ids": [str(r.id) for r in rows],
        "job_snapshot_ids": [str(r.id) for r in jobs],
        "fact_ids": [str(f.id) for j in jobs for f in facts_for(db, owner, j.id)],
        "dedup_decisions": [],
    }
    from careerforge.infrastructure.db.models import (
        DedupDecision,
        DuplicateCandidate,
        JobPostingVersion,
    )

    seen = set()
    for i in body.dedup_decision_ids:
        decision = db.get(DedupDecision, i)
        candidate = db.get(DuplicateCandidate, decision.candidate_id) if decision else None
        if (
            candidate is None
            or candidate.owner_id != owner
            or candidate.goal_version_id != target.id
        ):
            raise not_found()
        if candidate.id in seen:
            raise BusinessError(422, "AMBIGUOUS_DECISIONS", "同一候选对只能选择一个决定版本。")
        seen.add(candidate.id)
        links = []
        for posting_id in [candidate.left_id, candidate.right_id]:
            posting = db.get(JobPostingVersion, posting_id)
            mapped = job_for_source(db, owner, posting.snapshot_id)
            if not mapped or mapped.id not in {j.id for j in jobs}:
                raise BusinessError(422, "DECISION_INPUT_MISMATCH", "去重决定不属于所选快照。")
            links.append(str(mapped.id))
        manifest["dedup_decisions"].append(
            {
                "left": links[0],
                "right": links[1],
                "decision": decision.decision,
                "decision_id": str(decision.id),
            }
        )
    version, budget = VERSION, 0
    if body.analysis_mode == "real_agent":
        from careerforge.application.market_agent_service import VERSION as agent_version
        from careerforge.application.market_agent_service import freeze

        manifest.update(freeze(jobs))
        version, budget = agent_version, len(jobs)
    run = create_run(
        db,
        owner,
        "market_research",
        version,
        manifest,
        call_budget=budget,
        goal_version_id=target.id,
    )
    record.response_summary = {
        "run_id": str(run.id),
        "status": run.status,
        "version": run.version,
        "run_type": "market_research",
    }
    return record.response_summary


def produce_report(db, run, extra_fact_ids=()):
    manifest = {
        **run.input_manifest,
        "fact_ids": list(dict.fromkeys([*run.input_manifest["fact_ids"], *extra_fact_ids])),
    }
    owner = run.owner_id
    existing = db.scalar(select(MarketReport).where(MarketReport.run_id == run.id))
    if existing:
        read(db, owner, UUID(existing.payload["demand_report_id"]), "report")
        return {
            "market_report_id": str(existing.id),
            "demand_report_id": existing.payload["demand_report_id"],
            "_run_status": "succeeded"
            if existing.payload["counts"]["deduplicated"]
            else "partially_succeeded",
        }
    snapshots = [
        JobPostingSnapshotV1.model_validate(
            read(db, owner, UUID(i), "snapshot").payload["snapshot"]
        )
        for i in manifest["job_snapshot_ids"]
    ]
    facts = [
        RequirementFactV1.model_validate(read(db, owner, UUID(i), "fact").payload["fact"])
        for i in manifest["fact_ids"]
    ]
    target = owned(db, CareerGoalVersion, owner, UUID(manifest["goal_version_id"]))
    requested_type = target.payload.get("job_type", "unspecified")
    regions = target.payload.get("regions", [])
    scope = {
        "role_families": [],
        "employment_types": ["internship"] if requested_type == "internship" else [],
        "locations": regions,
    }
    role = (
        "agent_post_training"
        if "后训练" in target.payload.get("role_direction", "")
        else "agent_application"
        if "agent" in target.payload.get("role_direction", "").casefold()
        or "智能体" in target.payload.get("role_direction", "")
        else None
    )
    scope["role_families"] = [role] if role else []
    if requested_type in {"entry_level", "experienced"}:
        scope["employment_types"] = ["full_time"]
    exclusions = {}
    for s in snapshots:
        if role and s.job.role_family != role:
            exclusions[s.job_snapshot_id] = "outside_or_unknown_role_scope"
        if requested_type == "internship" and s.job.employment_type != "internship":
            exclusions[s.job_snapshot_id] = "outside_or_unknown_employment_scope"
        if (
            requested_type in {"entry_level", "experienced"}
            and s.job.employment_type != "full_time"
        ):
            exclusions[s.job_snapshot_id] = "outside_or_unknown_employment_scope"
        if regions and not set(regions).intersection(s.job.locations):
            exclusions[s.job_snapshot_id] = "outside_or_unknown_location_scope"
        confirmed = [
            f
            for f in facts
            if f.job_snapshot_id == s.job_snapshot_id
            and f.review_status == "confirmed"
            and f.origin == "explicit"
        ]
        if not confirmed:
            exclusions[s.job_snapshot_id] = "requirements_need_review"
    # One explicit selected version per posting; deterministic selection among frozen inputs.
    by_posting = {}
    for s in snapshots:
        if s.job_snapshot_id in exclusions:
            continue
        prior = by_posting.get(s.posting_id)
        if prior:
            newer = max([prior, s], key=lambda j: (j.source.retrieved_at, j.job_snapshot_id))
            older = s if newer is prior else prior
            exclusions[older.job_snapshot_id] = "older_selected_posting_version"
            by_posting[s.posting_id] = newer
        else:
            by_posting[s.posting_id] = s
    scopes = {
        s.job_snapshot_id: read(db, owner, UUID(s.source.source_capture_id), "capture").payload.get(
            "board_scope"
        )
        for s in snapshots
    }
    pairs = {
        frozenset([d["left"], d["right"]]): d["decision"]
        for d in manifest.get("dedup_decisions", [])
    }
    from careerforge.application.market_report_service import text_similarity
    from careerforge.infrastructure.db.models import DuplicateCandidate, JobPostingVersion

    pending = []
    active = [s for s in snapshots if s.job_snapshot_id not in exclusions]
    parent = {s.job_snapshot_id: s.job_snapshot_id for s in active}

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for n, left in enumerate(active):
        for right in active[n + 1 :]:
            if left.posting_id == right.posting_id:
                continue
            pair = frozenset([left.job_snapshot_id, right.job_snapshot_id])
            decision = pairs.get(pair)
            # Explicitly distinct real postings are never collapsed merely for identical text.
            same_scope = (
                left.source.provider == right.source.provider
                and bool(scopes[left.job_snapshot_id])
                and scopes[left.job_snapshot_id] == scopes[right.job_snapshot_id]
                and left.job.employer_name == right.job.employer_name
            )
            definite_distinct = bool(
                left.source.external_posting_id
                and right.source.external_posting_id
                and same_scope
                and left.source.external_posting_id != right.source.external_posting_id
            ) or bool(
                left.job.employer_name
                and right.job.employer_name
                and left.job.employer_name != right.job.employer_name
            )
            similar = not definite_distinct and (
                left.job.title_raw == right.job.title_raw
                or text_similarity(left.document.canonical_text, right.document.canonical_text)
                >= 0.88
            )
            if decision == "duplicate" and definite_distinct:
                raise BusinessError(
                    409,
                    "DEDUP_CONFLICT",
                    "同一来源范围内不同的外部岗位 ID 或明确不同公司不能合并。",
                )
            if decision == "duplicate":
                parent[find(right.job_snapshot_id)] = find(left.job_snapshot_id)
            elif decision == "distinct":
                continue
            elif similar:
                pending.append(pair)
    for d in manifest.get("dedup_decisions", []):
        if (
            d["decision"] == "distinct"
            and d["left"] in parent
            and d["right"] in parent
            and find(d["left"]) == find(d["right"])
        ):
            raise BusinessError(409, "DEDUP_CONFLICT", "去重裁决相互矛盾。")
    pending_roots = {find(i) for pair in pending for i in pair}
    for i in parent:
        if find(i) in pending_roots:
            exclusions[i] = "duplicate_pending_review"
    for pair in pending:
        a, b = [read(db, owner, UUID(i), "snapshot") for i in sorted(pair)]
        pa = db.scalar(
            select(JobPostingVersion).where(JobPostingVersion.snapshot_id == a.source_snapshot_id)
        )
        pb = db.scalar(
            select(JobPostingVersion).where(JobPostingVersion.snapshot_id == b.source_snapshot_id)
        )
        if pa and pb:
            left_id, right_id = sorted([pa.id, pb.id])
            old = db.scalar(
                select(DuplicateCandidate).where(
                    DuplicateCandidate.goal_version_id == target.id,
                    DuplicateCandidate.left_id == left_id,
                    DuplicateCandidate.right_id == right_id,
                )
            )
            if old is None:
                db.add(
                    DuplicateCandidate(
                        owner_id=owner,
                        goal_version_id=target.id,
                        run_id=run.id,
                        left_id=left_id,
                        right_id=right_id,
                        reason="contract_cross_source_pending",
                    )
                )
    for s in active:
        if s.job_snapshot_id not in exclusions and find(s.job_snapshot_id) != s.job_snapshot_id:
            exclusions[s.job_snapshot_id] = "confirmed_duplicate"
    identifier = uuid4()
    demand = aggregate(str(identifier), str(target.id), snapshots, facts, scope, exclusions)
    row = Artifact(
        id=identifier,
        owner_id=owner,
        kind="report",
        identity_key=str(run.id),
        run_id=run.id,
        payload={
            "report": demand.model_dump(mode="json"),
            "input_manifest": manifest,
            "facts": [f.model_dump(mode="json") for f in facts],
            "snapshots": [s.model_dump(mode="json") for s in snapshots],
            "warnings": [
                "仅统计已确认的明确要求；模型候选覆盖有限，不能推断真实市场质量"
                if manifest.get("analysis_mode") == "real_agent"
                else "仅统计已确认的明确要求；规则覆盖有限，不能推断真实市场质量"
            ],
        },
    )
    db.add(row)
    db.flush()
    postings = list(
        db.scalars(
            select(JobPostingVersion).where(
                JobPostingVersion.owner_id == owner,
                JobPostingVersion.snapshot_id.in_(
                    [UUID(i) for i in manifest["source_snapshot_ids"]]
                ),
            )
        )
    )
    source_to_job = {
        read(db, owner, UUID(j), "snapshot").source_snapshot_id: j
        for j in manifest["job_snapshot_ids"]
    }
    by_source = {
        read(db, owner, UUID(j), "snapshot").source_snapshot_id: read(
            db, owner, UUID(j), "snapshot"
        ).payload["snapshot"]["job"]
        for j in manifest["job_snapshot_ids"]
    }
    summaries = [
        {
            "posting_version_id": str(p.id),
            "source_snapshot_id": str(p.snapshot_id),
            "source_id": str(p.source_id),
            "title": by_source.get(p.snapshot_id, {}).get("title_raw"),
            "job_type": by_source.get(p.snapshot_id, {}).get("employment_type"),
            "regions": by_source.get(p.snapshot_id, {}).get("locations", []),
        }
        for p in postings
    ]
    eligible_postings = [
        str(p.id)
        for p in postings
        if source_to_job.get(p.snapshot_id) in demand.included_job_snapshot_ids
    ]
    legacy_id = uuid4()
    legacy = MarketReport(
        id=legacy_id,
        owner_id=owner,
        goal_version_id=target.id,
        run_id=run.id,
        payload={
            "generated_by_run_id": str(run.id),
            "source_snapshot_ids": manifest["source_snapshot_ids"],
            "input_summaries": [],
            "eligible_posting_version_ids": eligible_postings,
            "counts": {
                "raw": len(snapshots),
                "readable": sum(s.source.read_status == "full_text" for s in snapshots),
                "job_postings": len(snapshots),
                "extraction_completed": len(snapshots),
                "deduplicated": demand.denominator,
                "unresolved_duplicates": len(pending),
            },
            "warnings": row.payload["warnings"],
            "requirements": [],
            "filters": {"job_type": requested_type, "regions": regions},
            "skill_statistics": [],
            "unresolved_sources": [],
            "posting_summaries": summaries,
            "scope_exclusions": [],
            "dedup_groups": [],
            "regional_posting_groups": {},
            "job_contract": row.payload,
            "demand_report_id": str(identifier),
            "synthetic_model": False,
        },
    )
    db.add(legacy)
    db.flush()
    return {
        "market_report_id": str(legacy.id),
        "demand_report_id": str(identifier),
        "_run_status": "succeeded" if demand.denominator else "partially_succeeded",
    }


def create_context(db, owner, value, key):
    record = idempotency(db, owner, "job-contract.context", key, value.model_dump(mode="json"))
    report = read(db, owner, UUID(value.demand_report_id), "report")
    goal = owned(db, CareerGoalVersion, owner, UUID(value.goal_version_id))
    if report.payload["report"]["goal_version_id"] != str(goal.id):
        raise BusinessError(409, "REPORT_GOAL_MISMATCH", "需求报告与职业目标版本不一致。")
    if not report.payload["report"]["denominator"]:
        raise BusinessError(
            409, "REPORT_NEEDS_REVIEW", "正式分母为零，请先确认要求与去重，再生成报告。"
        )
    available = {d["demand_id"]: d for d in report.payload["report"]["demands"]}
    selected = (
        value.selected_demand_ids
        or [d["demand_id"] for d in available.values() if d["demand_unit"] == "expression_group"][
            :20
        ]
    )
    if not selected:
        raise BusinessError(422, "NO_LEARNABLE_DEMAND", "报告没有已确认的可学习要求。")
    if len(set(selected)) != len(selected) or not set(selected).issubset(available):
        raise BusinessError(422, "INVALID_DEMAND_SELECTION", "所选需求不属于指定报告或有重复。")
    from careerforge.application.planning_service import evidence_version

    evidences = [evidence_version(db, owner, UUID(i)) for i in value.personal_evidence_version_ids]
    if len({e.id for e in evidences}) != len(evidences):
        raise BusinessError(422, "DUPLICATE_EVIDENCE", "个人材料版本不可重复。")
    for i in report.payload["input_manifest"]["fact_ids"]:
        if db.scalar(select(Artifact.id).where(Artifact.supersedes_id == UUID(i))):
            raise BusinessError(409, "REPORT_STALE", "报告中的要求已改版，请重新生成报告。")
    if record.response_summary:
        read(db, owner, UUID(record.response_summary["contract_artifact_id"]), "context")
        return record.response_summary
    eligibility = []
    for fact in report.payload["facts"]:
        if fact["requirement_id"] in report.payload["report"]["eligibility_requirement_ids"]:
            for condition in constraints(fact["expression"]):
                eligibility.append(
                    {
                        "field": condition["field"],
                        "status": "unknown",
                        "source_requirement_ids": [fact["requirement_id"]],
                        "action": "ask_user",
                    }
                )
    identifier = uuid4()
    context = {
        "schema_version": "careerforge.planning_context.v1",
        "planning_context_id": str(identifier),
        "goal_version_id": str(goal.id),
        "demand_report_id": str(report.id),
        "selected_demand_ids": selected,
        "personal_evidence_version_ids": value.personal_evidence_version_ids,
        "eligibility_checks": eligibility,
        "user_constraints": value.user_constraints.model_dump(mode="json"),
        "planning_policy": {
            "preserve_unknown": True,
            "explicit_requirements_only": True,
            "require_source_evidence": True,
        },
    }
    manifest = {
        "context": json.loads(json.dumps(context)),
        "goal_hash": goal.content_hash,
        "report_hash": fingerprint(report.payload),
        "evidence_hashes": {str(e.id): fingerprint(e.payload) for e in evidences},
    }
    context["input_fingerprint_sha256"] = fingerprint(manifest)
    parsed = PlanningContextV1.model_validate(context)
    row = Artifact(
        id=identifier,
        owner_id=owner,
        kind="context",
        identity_key=str(identifier),
        parent_id=report.id,
        payload={"context": parsed.model_dump(mode="json"), "fingerprint_manifest": manifest},
    )
    db.add(row)
    db.flush()
    record.response_summary = result(row)
    return record.response_summary
