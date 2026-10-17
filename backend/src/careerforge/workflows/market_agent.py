"""Accounted real calls with per-JD checkpoints and a citation-backed report."""

import asyncio
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from careerforge.application.job_agent_service import request_for, save_facts, validated_facts
from careerforge.application.job_contract_service import produce_report
from careerforge.application.market_agent_service import VERSION, snapshots_for_run
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.db.models import MarketReport
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.provider import ModelError
from careerforge.workflows.job_agent import call


def execute(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    try:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            snapshots = snapshots_for_run(db, run)
            manifest = dict(run.input_manifest)
            if artifact(db, run, "result", VERSION):
                transition(db, run, "succeeded")
                return
        outputs: list[Json] = []
        output: Json
        for snapshot in snapshots:
            key = "extract:" + snapshot.job_snapshot_id
            with factory.begin() as db:
                run = leased(db, run_id, epoch, worker)
                snapshots_for_run(db, run)
                prior = artifact(db, run, key, VERSION)
                if prior:
                    outputs.append(prior.payload)
                    continue
                attempted = artifact(db, run, key + ":attempted", VERSION)
                if attempted:
                    output = {
                        "job_snapshot_id": snapshot.job_snapshot_id,
                        "title": snapshot.job.title_raw,
                        "facts": [],
                        "requirement_ids": [],
                        "unresolved": [],
                        "error": "interrupted_call_not_retried",
                    }
                    commit_stage(db, run_id, epoch, worker, key, VERSION, output)
                    outputs.append(output)
                    continue
                commit_stage(
                    db,
                    run_id,
                    epoch,
                    worker,
                    key + ":attempted",
                    VERSION,
                    {"automatic_retry": False},
                )
            try:
                result = asyncio.run(
                    call(
                        run_id, epoch, worker, request_for(snapshot, manifest), manifest["base_url"]
                    )
                )
                if result is None:
                    return
                if result.finish_reason != "stop":
                    raise ModelError("invalid_response", retryable=False)
                facts, unresolved = validated_facts(snapshot, result)
                with factory.begin() as db:
                    run = leased(db, run_id, epoch, worker)
                    snapshots_for_run(db, run)
                    output = save_facts(
                        db, run, facts, unresolved, result, snapshot.job_snapshot_id
                    )
                    output["facts"] = [f.model_dump(mode="json") for f in facts]
                    output["title"] = snapshot.job.title_raw
                    commit_stage(db, run_id, epoch, worker, key, VERSION, output)
                    outputs.append(output)
            except (ModelError, ValidationError, ValueError) as error:
                with factory.begin() as db:
                    run = leased(db, run_id, epoch, worker)
                    output = {
                        "job_snapshot_id": snapshot.job_snapshot_id,
                        "title": snapshot.job.title_raw,
                        "facts": [],
                        "requirement_ids": [],
                        "unresolved": [],
                        "error": getattr(error, "code", "invalid_candidate"),
                    }
                    commit_stage(db, run_id, epoch, worker, key, VERSION, output)
                    event(
                        db,
                        run,
                        "stage.failed",
                        {"error": output["error"], "automatic_retry": False},
                    )
                    outputs.append(output)
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            snapshots_for_run(db, run)
            output = produce_report(
                db, run, [i for item in outputs for i in item["requirement_ids"]]
            )
            report = db.get(MarketReport, UUID(output["market_report_id"]))
            if report is None:
                raise BusinessError(404, "NOT_FOUND", "分析报告不存在。")
            report.payload = {
                **report.payload,
                "agent_analysis": {
                    "model": manifest["model"],
                    "reasoning_effort": "low",
                    "synthetic": False,
                    "review_required": True,
                    "jobs": outputs,
                    "candidate_count": sum(len(i["facts"]) for i in outputs),
                    "failed_jobs": sum("error" in i for i in outputs),
                },
                "warnings": ["真实模型候选已校验原文位置；尚未人工确认的要求不进入正式统计。"],
            }
            if report is None:
                raise BusinessError(404, "NOT_FOUND", "分析报告不存在。")
            report.payload = {
                **report.payload,
                "counts": {
                    **report.payload["counts"],
                    "extraction_completed": sum("error" not in i for i in outputs),
                },
            }
            status = (
                "failed"
                if all("error" in i for i in outputs)
                else "partially_succeeded"
                if any("error" in i or not i["facts"] for i in outputs)
                else "succeeded"
            )
            output.pop("_run_status", None)
            commit_stage(db, run_id, epoch, worker, "result", VERSION, output)
            transition(db, run, status)
    except LeaseLost:
        return
    except BusinessError as error:
        with factory.begin() as db:
            try:
                run = leased(db, run_id, epoch, worker)
                event(db, run, "stage.failed", {"error": error.code, "automatic_retry": False})
                transition(db, run, "failed")
            except LeaseLost:
                return
