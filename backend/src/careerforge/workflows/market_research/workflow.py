import asyncio
import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from careerforge.application.market_report_service import VERSION, build_report, inputs
from careerforge.application.model_call_service import invoke_model
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.jobs import offline_extract
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.fake import FakeModel
from careerforge.infrastructure.llm.provider import ModelError
from careerforge.schemas.job_extraction import JobExtraction
from careerforge.schemas.model_io import ModelRequest


def validate_inputs(db: Any, run: Any) -> Any:
    snapshots = inputs(db, run.owner_id, run.input_manifest["source_snapshot_ids"])
    for row in snapshots:
        if [row.content_hash, row.metadata_hash] != run.input_manifest["snapshot_hashes"][
            str(row.id)
        ]:
            raise BusinessError(409, "INPUT_CHANGED", "冻结输入发生变化。")
    return snapshots


def execute(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    try:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            snapshots = validate_inputs(db, run)
            if artifact(db, run, "validate_inputs", VERSION) is None:
                commit_stage(
                    db,
                    run_id,
                    epoch,
                    worker,
                    "validate_inputs",
                    VERSION,
                    {"source_snapshot_ids": [str(r.id) for r in snapshots]},
                )
            frozen = [
                (str(r.id), r.content, r.metadata_values)
                for r in snapshots
                if r.metadata_values.get("source_type") == "job_posting"
            ]
        extracted: dict[str, dict[str, Any]] = {}
        for identifier, text, metadata in frozen:
            key = f"extract:{identifier}"
            with factory.begin() as db:
                run = leased(db, run_id, epoch, worker)
                validate_inputs(db, run)
                saved = artifact(db, run, key, VERSION)
                if saved is not None:
                    extracted[identifier] = saved.payload
                    continue
            candidate = offline_extract(text, metadata.get("title"))
            provider = FakeModel(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(candidate, ensure_ascii=False)},
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                }
            )
            try:
                result = asyncio.run(
                    invoke_model(
                        run_id,
                        epoch,
                        worker,
                        provider,
                        ModelRequest(
                            model="offline-fake",
                            prompt="Validate offline extraction fixture",
                            response_schema=JobExtraction.model_json_schema(),
                        ),
                        max_retries=0,
                    )
                )
                if result is None:
                    return
                if result.finish_reason != "stop":
                    raise ModelError("invalid_response", retryable=False)
                value = JobExtraction.model_validate(result.structured)
                # Validate every span before publishing even the extraction stage.
                from careerforge.application.market_report_service import validate_quote

                with factory.begin() as db:
                    run = leased(db, run_id, epoch, worker)
                    rows = validate_inputs(db, run)
                    snapshot = next(r for r in rows if str(r.id) == identifier)
                    for requirement in value.requirements:
                        validate_quote(snapshot, requirement)
                payload = {"extraction": value.model_dump(), "synthetic_model": True}
            except (ModelError, ValidationError, BusinessError) as error:
                payload = {"error": getattr(error, "code", "invalid_schema")}
            with factory.begin() as db:
                run = leased(db, run_id, epoch, worker)
                validate_inputs(db, run)
                commit_stage(db, run_id, epoch, worker, key, VERSION, payload)
                if payload.get("error"):
                    event(db, run, "stage.failed", {"stage": key, "error": payload["error"]})
            extracted[identifier] = payload
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            validate_inputs(db, run)
            report = build_report(db, run.owner_id, run.id, run.input_manifest, extracted)
            commit_stage(
                db,
                run_id,
                epoch,
                worker,
                "result",
                VERSION,
                {"market_report_id": str(report.id), "synthetic_model": True},
            )
            counts = report.payload["counts"]
            status = "succeeded"
            if not report.payload["eligible_posting_version_ids"]:
                status = "failed"
            elif (
                counts["extraction_completed"] < counts["job_postings"]
                or counts["unresolved_duplicates"]
            ):
                status = "partially_succeeded"
            event(db, run, "tool.summary", {"report_id": str(report.id), "counts": counts})
            transition(db, run, status)
    except BusinessError as error:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            event(
                db,
                run,
                "input.revoked" if error.status == 404 else "stage.failed",
                {"error": error.code},
            )
            transition(db, run, "failed")
