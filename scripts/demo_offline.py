"""Executable fixture-only product walkthrough; NOT real model/semantic acceptance.

The script creates and destroys an isolated SQLite database, explicitly injects test
providers, runs actual service workflows, and exports only redacted result summaries.
It cannot select production mode or write synthetic knowledge into a production DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from support import FixtureModel, FixtureRepositories  # explicitly synthetic test adapters

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import FeedbackInput, KnowledgeBuildInput, TurnInput
from careerforge.models import AgentRun


async def main(engine: str = "legacy") -> None:
    with tempfile.TemporaryDirectory(prefix="cf-fixture-demo-") as tmp:
        settings = Settings(
            mode="test",
            workflow_engine=engine,
            database_url="sqlite+pysqlite:///" + tmp + "/demo.db",
            allow_fixture_publication=True,
            embedding_dimensions=8,
            state_dir=Path(tmp),
        )
        model = FixtureModel()
        app = Application(settings, model, FixtureRepositories())
        owner = app.bootstrap("fixture-demo-user")
        actor, namespace = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, namespace, 100)
        run = app.build.start(
            actor,
            KnowledgeBuildInput(
                namespace_id=namespace,
                funding_id=funding,
                question="寻找并学习 Tool Calling 的真实实现位置",
                jd_text="熟悉 Tool Calling 工具调用。",
                topic_keys=["agent.tool_calling"],
                max_projects=1,
                max_units_per_project=1,
                call_cap=20,
                idempotency_key="offline-demo-build",
            ),
        )
        await app.drain()
        with app.db.read() as db:
            status = db.get(AgentRun, run).status
        result = app.runtime.stage(run, "result")
        if (
            status not in {"succeeded", "partially_succeeded"}
            or not result
            or not result.get("publication_ids")
        ):
            raise RuntimeError(
                f"No reviewed publication was produced: status={status}, result={result}"
            )
        research = app.runtime.stage(run, "research.result")
        conversation = app.conversations.create(actor)
        response = app.conversations.submit(
            actor,
            conversation,
            TurnInput(
                question="讲解 Tool Calling 的实现",
                expected_revision=1,
                funding_id=funding,
                idempotency_key="offline-demo-question",
            ),
        )
        await app.drain()
        answer = app.answers.events(actor, response["turn_id"])
        assert answer["status"] == "completed"
        blocks = [
            e["payload"]["content"]
            for e in answer["events"]
            if e["type"] == "answer.block.validated"
        ]
        assert any(b.get("publication_ids") for b in blocks)
        feedback = app.evolution.feedback(
            actor,
            FeedbackInput(
                turn_id=response["turn_id"],
                signal="explicit_feedback",
                content="太抽象，请先看代码再讲概念",
                consent=True,
            ),
        )
        report = {
            "mode": "explicit_fixture_test",
            "real_model_calls": 0,
            "real_github_requests": 0,
            "real_docker_commands": 0,
            "database": "isolated_temporary_sqlite",
            "workflow_engine": engine,
            "workflow_status": status,
            "coverage": result.get("task_coverage", {}),
            "gaps": result.get("gaps", []),
            "research_node_count": len(research["tree"]),
            "publication_count": len(result["publication_ids"]),
            "answer_status": answer["status"],
            "validated_blocks": blocks,
            "feedback_diagnosis": feedback["diagnosis"],
            "candidate_automatically_promoted": False,
            "knowledge_quality_or_production_acceptance": "NOT_ESTABLISHED",
        }
        target = ROOT / "validation" / "current" / "offline_demo.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        app.db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("legacy", "langgraph"), default="legacy")
    asyncio.run(main(parser.parse_args().engine))
