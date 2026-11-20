"""Real relay probe against failed retry's frozen evidence; no publication writes."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from time import monotonic

from jsonschema import Draft202012Validator
from sqlalchemy import select

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_project_analysis_v166 import MechanismAnalysisOutput
from careerforge.models import AgentRun, ModelReceipt


async def main():
    path = Path("private/local-validation/registered-facets-live-20261117.json")
    if path.exists():
        raise SystemExit("Existing probe preserved")
    app = Application(
        replace(
            Settings.from_env(),
            mode="development",
            model_name="gpt-6.1-terra",
            model_streaming=True,
            model_timeout=300,
            database_url="sqlite:///private/local-runtime/development.db",
            state_dir=Path("private/local-runtime"),
        )
    )
    with app.db.read() as db:
        run = db.get(AgentRun, "b4c2c71d-481a-48b5-97b8-d6ba801934a8")
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id,
                ModelReceipt.stage_key == "analysis.final:fa7193fb-c8f7-4a92-a35c-7a8de4400c7d",
            )
        )
        old = receipt.response["structured"]
    source = run.input_manifest["source_ids"][0]
    topics = app.build._topics(run.principal_id, run.input_manifest["taxonomy_versions"])
    schema = app.build._analysis_schema(
        topics, MechanismAnalysisOutput, registered_pairs=True, max_units=1
    )
    facet_schema = {"$ref": "#/$defs/FacetDraft", "$defs": schema["$defs"]}
    validator = Draft202012Validator(facet_schema)
    invalid = [
        {"topic_key": f["topic_key"], "facet_key": f["facet_key"]}
        for u in old["units"]
        for f in u["facets"]
        if not validator.is_valid(f)
    ]
    assert invalid
    reads = app.runtime.stage(run.id, "reading:" + source)
    context = {
        "question": run.input_manifest["question"],
        "reading": reads,
        "registered_topics": topics,
        "max_units": 1,
        "prior_review_findings": run.input_manifest.get("retry_lineage", {}).get("findings", {}),
    }
    request = app.gateway.request_payload(
        "analysis",
        app.build._analysis_system(run, False)
        + "\n最多输出一个单元。仅使用 registered_topics 中已注册的主题和维度配对；没有注册维度的主题仍可用 claims 表达有证据的事实，不得创造维度。",
        context,
        MechanismAnalysisOutput,
        output_schema=schema,
    )
    record = {
        "status": "running",
        "probe_only": True,
        "publication_written": False,
        "original_invalid_pairs": invalid,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    start = monotonic()
    try:
        result = await app.provider.complete(request)
        record["response"] = result.model_dump(mode="json")
        app.gateway.validate_result(result, MechanismAnalysisOutput)
        Draft202012Validator(schema).validate(result.structured)
        record.update(
            status="completed",
            registered_pairs_valid=True,
            facets=[
                {"topic_key": f["topic_key"], "facet_key": f["facet_key"]}
                for u in result.structured["units"]
                for f in u["facets"]
            ],
        )
    except Exception as exc:
        record.update(
            status="failed", error=str(exc), error_code=getattr(exc, "code", type(exc).__name__)
        )
    finally:
        record["elapsed_seconds"] = round(monotonic() - start, 2)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in record.items() if k != "response"}, ensure_ascii=False))


asyncio.run(main())
