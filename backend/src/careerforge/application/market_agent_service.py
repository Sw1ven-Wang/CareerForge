"""Frozen, bounded real JD analysis; proposals do not become confirmed statistics."""

from uuid import UUID

from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.application.job_agent_service import PROMPT_VERSION, configuration
from careerforge.application.job_contract_service import read
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import AgentRun, JobContractArtifact
from careerforge.schemas.job_contract import JobPostingSnapshotV1

VERSION = "market-agent-1.0"


def freeze(jobs: list[JobContractArtifact]) -> Json:
    config = configuration()
    if not config["enabled"] or not config["configured"]:
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实模型未启用或未配置。")
    if len(jobs) > 5:
        raise BusinessError(
            422, "AGENT_INPUT_LIMIT", "真实分析每次最多 5 份岗位正文，每份最多调用一次。"
        )
    for row in jobs:
        snapshot = JobPostingSnapshotV1.model_validate(row.payload["snapshot"])
        if (
            snapshot.source.read_status != "full_text"
            or snapshot.quality.normalization_status != "ready"
        ):
            raise BusinessError(422, "FULL_TEXT_REQUIRED", "请先补齐完整、可读的岗位正文。")
        if len(snapshot.document.canonical_text) > 12000:
            raise BusinessError(413, "AGENT_INPUT_TOO_LARGE", "单份岗位正文最多 12,000 字符。")
    settings = get_settings()
    return {
        "analysis_mode": "real_agent",
        "model": settings.model_name,
        "reasoning_effort": "low",
        "max_output_tokens": settings.model_max_output_tokens,
        "base_url": settings.openai_base_url,
        "prompt_version": PROMPT_VERSION,
        "snapshot_hashes": {str(j.id): fingerprint(j.payload) for j in jobs},
        "no_automatic_retry": True,
    }


def snapshots_for_run(db: Session, run: AgentRun) -> list[JobPostingSnapshotV1]:
    snapshots = []
    for identifier in run.input_manifest["job_snapshot_ids"]:
        row = read(db, run.owner_id, UUID(identifier), "snapshot")
        if fingerprint(row.payload) != run.input_manifest["snapshot_hashes"][identifier]:
            raise BusinessError(409, "INPUT_CHANGED", "冻结岗位正文发生变化，请重新创建分析。")
        snapshots.append(JobPostingSnapshotV1.model_validate(row.payload["snapshot"]))
    return snapshots
