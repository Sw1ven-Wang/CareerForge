"""One explicit real-provider coverage probe; never publishes or alters old runs."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from time import monotonic

from sqlalchemy import select

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_scoped_review_v166 import ScopedReviewOutput
from careerforge.models import AgentRun, ModelReceipt, UnitVersion
from careerforge.services.coverage_issues import (
    COVERAGE_ISSUES_INSTRUCTIONS,
    coverage_issue_catalog,
    validate_local_issue_indices,
)
from careerforge.services.scope_binding import coverage_identity_schema
from careerforge.services.scoped_review import SCOPED_COVERAGE_INSTRUCTIONS
from careerforge.util import fingerprint


async def main():
    path = Path("private/local-validation/typed-coverage-live-20261117.json")
    if path.exists():
        raise SystemExit("Existing probe preserved; inspect its result before any new invocation.")
    settings = replace(Settings.from_env(), mode="development", model_name="gpt-6.1-terra",
        model_streaming=True, model_timeout=300,
        database_url="sqlite:///private/local-runtime/development.db",
        state_dir=Path("private/local-runtime"))
    app = Application(settings)
    version_id = "44c0bb52-c3f3-4ff4-a892-e04215ff549f"
    with app.db.read() as db:
        version = db.get(UnitVersion, version_id)
        receipt = db.scalar(select(ModelReceipt).where(
            ModelReceipt.stage_key == "review.final:" + version_id + ":quote-correction:1"))
        run = db.get(AgentRun, receipt.run_id)
        review = ScopedReviewOutput.model_validate({k:v for k,v in receipt.response["structured"].items()
            if k not in {"task_coverage", "evidence_read_requests"}})
        bundle = app.knowledge._scoped_bundle_in(db, run.principal_id, run, version)
        subject = app.knowledge._scope_subject_in(db, run.principal_id, version)
        coverage_type = app.knowledge._coverage_type(run)
        old = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run.id,
            ModelReceipt.stage_key == "coverage:" + version_id))
        old_coverage = coverage_type.model_validate(old.response["structured"])
        try:
            validate_local_issue_indices(review, old_coverage.relation_audit.confirmed_local_issue_indices)
            raise AssertionError("The captured failure must reproduce")
        except Exception as exc:
            if isinstance(exc, AssertionError):
                raise
            old_failure = str(exc)
    expanded = app.knowledge.review_subject(run.principal_id, version_id, True, scoped=True)
    catalog = coverage_issue_catalog(review)
    context = {"subject": expanded, "review": review.model_dump(mode="json"),
        "independent_reads": bundle["reads"], "coverage_issue_catalog": catalog,
        "scope_binding": {**expanded["scope_identity"], "reviewer_output_hash": fingerprint(review.model_dump(mode="json"))}}
    system = ("检查原候选的事实、条件与主题/维度关联。独立判断，不能强行批准。"
        + SCOPED_COVERAGE_INSTRUCTIONS + COVERAGE_ISSUES_INSTRUCTIONS
        + "关联只可选择 Reviewer 已接受且未被局部阻断的对象，必须有对应断言的独立读取支持。")
    schema = coverage_identity_schema(coverage_type, expanded, context["scope_binding"]["reviewer_output_hash"], review, bundle["reads"])
    request = app.gateway.request_payload("coverage", system, context, coverage_type, output_schema=schema)
    record = {"status": "running", "probe_only": True, "publication_written": False,
        "original_version_id": version_id, "old_failure": old_failure,
        "eligible_indices": [r["original_issue_index"] for r in catalog["eligible_local_issues"]],
        "original_indices": old_coverage.relation_audit.confirmed_local_issue_indices,
        "request_hash": fingerprint(request)}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    start = monotonic()
    try:
        result = await app.provider.complete(request)
        record["response"] = result.model_dump(mode="json")
        app.gateway.validate_result(result, coverage_type)
        output = coverage_type.model_validate(result.structured)
        validate_local_issue_indices(review, output.relation_audit.confirmed_local_issue_indices)
        record["issue_binding_passed"] = True
        with app.db.read() as db:
            partition = app.knowledge._review_partition(db, run.principal_id, run, version, subject, review, output)
        record.update(status="completed", partition_status=partition["status"],
            confirmed_indices=output.relation_audit.confirmed_local_issue_indices)
    except Exception as exc:
        record.update(status="failed", error_code=getattr(exc, "code", type(exc).__name__), error=str(exc))
    finally:
        record["elapsed_seconds"] = round(monotonic()-start, 2)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k:v for k,v in record.items() if k != "response"}, ensure_ascii=False))
        app.db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
