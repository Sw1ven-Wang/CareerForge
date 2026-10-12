"""Create bounded collection runs and store public evidence using the existing V1.1 contract."""

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from careerforge.application.common import digest, fingerprint, idempotency
from careerforge.application.job_contract_service import add_capture
from careerforge.application.run_service import create_run, owned_run
from careerforge.application.source_library_service import assign, lock_library
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.job_normalization import adapt
from careerforge.infrastructure.collection.extractors import ADAPTER_VERSION
from careerforge.infrastructure.collection.portals import PORTALS, catalog
from careerforge.infrastructure.db.models import (
    AgentRun,
    RunEvent,
    Source,
    SourceSnapshot,
    StageArtifact,
)
from careerforge.schemas.job_contract import CaptureImport

VERSION = "job-collection-1.0"
SKILL_PATH = Path(__file__).resolve().parents[1] / "data/skills/official-recruitment/SKILL.md"


def configuration():
    s = get_settings()
    return {
        "channels": catalog(),
        "model": s.model_name,
        "agent_available": s.real_agent_enabled and bool(s.openai_api_key.get_secret_value()),
        "search_api_configured": bool(s.tavily_api_key.get_secret_value()),
        "skill": "official-recruitment",
        "adapter_version": ADAPTER_VERSION,
    }


def start(db, owner, body, key):
    record = idempotency(db, owner, "collection.start", key, body.model_dump(mode="json"))
    if record.response_summary:
        return record.response_summary
    settings = get_settings()
    if body.mode == "agent" and not configuration()["agent_available"]:
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未启用或未配置模型。")
    if body.allow_search_api and not settings.tavily_api_key.get_secret_value():
        raise BusinessError(
            409,
            "SEARCH_API_UNCONFIGURED",
            "跨站搜索服务未配置；可采集已有详情链接，或查询腾讯官网。",
        )
    manifest = {
        **body.model_dump(mode="json"),
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "skill_hash": digest(SKILL_PATH.read_text(encoding="utf-8")),
        "adapter_version": ADAPTER_VERSION,
        "no_automatic_retry": True,
    }
    run = create_run(
        db,
        owner,
        "market_research",
        VERSION,
        manifest,
        call_budget=body.model_call_limit if body.mode == "agent" else 0,
    )
    record.response_summary = {"run_id": str(run.id), "status": run.status, "version": run.version}
    return record.response_summary


def existing_source(db, owner, url):
    return db.scalar(
        select(Source)
        .where(Source.owner_id == owner, Source.source_url == url, Source.revoked_at.is_(None))
        .order_by(Source.created_at.desc())
        .limit(1)
    )


def store_job(db, owner, job, page, job_category, trace):
    if not 80 <= len(job.body.strip()) <= 100000 or not job.title.strip():
        raise BusinessError(422, "INCOMPLETE_JOB", "详情正文不足，未作为完整 JD 保存。")
    lock_library(db, owner)
    source = existing_source(db, owner, job.url)
    previous = None
    if source:
        previous = db.scalar(
            select(SourceSnapshot)
            .where(SourceSnapshot.source_id == source.id)
            .order_by(SourceSnapshot.version_no.desc())
            .limit(1)
        )
        source.revision += 1
        source.title = job.title
    else:
        source = Source(
            owner_id=owner,
            identity_hash=digest(job.url),
            source_url=job.url,
            source_type="job_posting",
            title=job.title,
            import_mode="text",
        )
        db.add(source)
        db.flush()
    metadata = {
        "source_type": "job_posting",
        "title": job.title,
        "job_category": job_category,
        "source_category": PORTALS[job.channel][0],
        "declared_provenance": "公开岗位采集；不包含搜索摘要",
        "collection": {
            "channel": job.channel,
            "adapter_version": ADAPTER_VERSION,
            "availability": job.availability,
            "effective_url": page.url,
        },
    }
    snapshot = SourceSnapshot(
        owner_id=owner,
        source_id=source.id,
        version_no=previous.version_no + 1 if previous else 1,
        content=job.body,
        content_hash=digest(job.body),
        metadata_hash=fingerprint(metadata),
        metadata_values=metadata,
    )
    db.add(snapshot)
    db.flush()
    value = CaptureImport(
        provider="manual_import",
        source_url=job.url,
        board_scope=job.channel,
        title=job.title,
        employer_name=job.employer,
        raw_payload=job.body,
        http_status=page.status,
        usage_policy_status="unknown",
    )
    meta, text, _, _, _ = adapt("manual_import", job.body, job.title, job.employer)
    meta.update(locations=job.locations, salary_raw=job.salary, employment_type=job.employment_type)
    # Preserve timezone-less source update values as metadata rather than inventing UTC.
    capture_metadata = {
        "availability": job.availability,
        "original_updated_at": job.provider_updated_at,
        "request_trace": trace,
        "parser": ADAPTER_VERSION,
    }
    collected = {
        "channel": job.channel,
        "raw_bytes": page.raw,
        "adapted": (meta, text, job.external_id, job.url, capture_metadata),
        "ingest_method": "official_api" if job.channel == "tencent" else "public_page",
        "effective_url": page.url,
        "raw_format": "json" if "json" in page.content_type else "html",
        "adapter_version": ADAPTER_VERSION,
    }
    row = add_capture(db, owner, source, snapshot, job.body, value, collected=collected)
    assign(db, owner, source, job_category, PORTALS[job.channel][0])
    from careerforge.application.import_service import materialize_job

    materialize_job(db, snapshot)
    return {
        "status": "collected",
        "channel": job.channel,
        "title": job.title,
        "url": job.url,
        "source_id": str(source.id),
        "source_snapshot_id": str(snapshot.id),
        "job_snapshot_id": str(row.id),
        "availability": job.availability,
        "raw_sha256": hashlib.sha256(page.raw).hexdigest(),
    }


def history(db, owner):
    rows = db.scalars(
        select(AgentRun)
        .where(AgentRun.owner_id == owner, AgentRun.workflow_version == VERSION)
        .order_by(AgentRun.created_at.desc())
        .limit(20)
    )
    return [
        {
            "run_id": str(row.id),
            "status": row.status,
            "query": row.input_manifest["query"],
            "mode": row.input_manifest["mode"],
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def result(db, owner, identifier):
    run = owned_run(db, owner, identifier)
    if run.workflow_version != VERSION:
        raise BusinessError(404, "NOT_FOUND", "不是岗位采集运行。")
    stages = db.scalars(
        select(StageArtifact)
        .where(StageArtifact.run_id == run.id)
        .order_by(StageArtifact.created_at, StageArtifact.id)
    ).all()
    observations = []
    for stage in stages:
        if not stage.stage_key.startswith("tool:"):
            continue
        item = dict(stage.payload)
        if item.get("source_id"):
            source = db.get(Source, UUID(item["source_id"]))
            if not source or source.owner_id != owner or source.revoked_at is not None:
                item = {"status": "revoked", "message": "此来源已撤销"}
        observations.append(item)
    requests = db.scalar(
        select(func.count())
        .select_from(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.type == "collection.http_reserved")
    )
    return {
        "run_id": str(run.id),
        "status": run.status,
        "query": run.input_manifest["query"],
        "mode": run.input_manifest["mode"],
        "observations": observations,
        "http_requests": requests,
        "request_limit": run.input_manifest["request_limit"],
        "model_calls": run.calls_reserved,
        "model_call_limit": run.call_budget,
        "cost_status": "not_priced",
        "skill": "official-recruitment",
    }
