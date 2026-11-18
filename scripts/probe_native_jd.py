"""Probe three fresh JD Graphs using explicitly configured real provider credentials.

Requires OPENAI_BASE_URL, OPENAI_API_KEY and CAREERFORGE_TEST_GRAPH_POSTGRES_DSN.
Makes up to nine authorized model requests across three synthetic JD inputs.
Checkpoint setup runs serially as a deployment operation. Outputs exclude credentials.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_jd_v166 import JDExtractionInput, JDImportInput
from careerforge.models import AgentRun, ModelReceipt
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter


async def run(index):
    with tempfile.TemporaryDirectory(prefix="cf550-native-jd-") as path:
        app = Application(
            Settings(
                mode="development",
                database_url="sqlite+pysqlite:///" + path + "/test.sqlite",
                state_dir=Path(path),
                workflow_engine="langgraph",
                graph_checkpoint_url=os.environ["CAREERFORGE_TEST_GRAPH_POSTGRES_DSN"],
                model_base_url=os.environ["OPENAI_BASE_URL"],
                model_api_key=os.environ["OPENAI_API_KEY"],
                model_name="gpt-6.1-terra",
                model_timeout=180,
            )
        )
        owner = app.bootstrap("native-jd-probe")
        app.graph_runtime = GraphRuntimeAdapter(app)
        async with app.graph_runtime.checkpointers.open() as saver:
            await saver.setup()
        actor, ns = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, ns, 3)
        text = "岗位要求：熟悉 Python 或 Java，了解 Tool Calling；Redis 优先，不要求模型微调。"
        document = app.jd.import_jd(
            actor, JDImportInput(namespace_id=ns, jd_id="native-jd", raw_text=text)
        )
        admitted = app.jd.start_extraction(
            actor,
            JDExtractionInput(
                namespace_id=ns,
                funding_id=funding,
                jd_revision_id=document["jd_revision_id"],
                idempotency_key="extract",
            ),
        )
        await app.drain()
        with app.db.read() as db:
            root = db.get(AgentRun, admitted["run_id"])
            receipts = list(db.scalars(select(ModelReceipt)))
            result = {
                "attempt": index,
                "engine": root.workflow_engine,
                "status": root.status,
                "calls": len(receipts),
                "receipt_statuses": [r.status for r in receipts],
                "usage": [r.usage for r in receipts],
                "output": app.runtime.stage(root.id, "result"),
            }
        app.db.engine.dispose()
        return result


async def main():
    async def guarded(i):
        try:
            return await run(i)
        except Exception as exc:
            return {
                "attempt": i,
                "error_type": type(exc).__name__,
                "code": getattr(exc, "code", None),
                "detail": str(exc) if isinstance(exc, TypeError) else None,
            }

    results = [await guarded(i) for i in range(1, 4)]
    print(
        json.dumps(
            {
                "scope": "three_cold_JD_graphs_only",
                "provider": "local_subscription_terra_medium",
                "independent_gold": "NOT_RUN",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return int(any(row.get("status") != "succeeded" for row in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
