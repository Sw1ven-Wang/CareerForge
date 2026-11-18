"""Run the authorized native workflow on one persistent, isolated business database."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.models import AgentRun, ModelReceipt, RunEvent
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter

OUT = Path(os.getenv("CAREERFORGE_LIVE_OUTPUT", "/evidence/run"))
OWNER = Path("/data/full-loop-owner-" + OUT.name + ".json")


async def main():
    OUT.mkdir(exist_ok=True)
    settings = Settings(
        mode="development",
        database_url="sqlite+pysqlite:///" + str(OUT / "workflow.sqlite"),
        state_dir=OUT,
        workflow_engine="langgraph",
        session_graph_version="knowledge-session-v2",
        learning_graph_version="learning-session-practice-v1",
        evolution_graph_enabled=True,
        graph_checkpoint_url=os.environ["CAREERFORGE_TEST_GRAPH_POSTGRES_DSN"],
        model_base_url=os.environ["OPENAI_BASE_URL"],
        model_api_key=os.environ["OPENAI_API_KEY"],
        model_name="gpt-6.1-terra",
        model_timeout=180,
        github_token=os.getenv("GITHUB_TOKEN", ""),
    )
    app = Application(settings)
    app.graph_runtime = GraphRuntimeAdapter(app)
    async with app.graph_runtime.checkpointers.open() as saver:
        await saver.setup()
    report_path = OUT / "result.json"
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "model": "gpt-6.1-terra",
            "effort": "medium",
            "stages": {},
            "status": "running",
            "independent_gold": "NOT_RUN",
        }
    )
    if report.get("status") == "blocked":
        report.setdefault("resume_history", []).append(
            {"error_type": report.get("error_type"), "error": report.get("error")}
        )
    report["status"] = "running"
    report.pop("error", None)
    report.pop("error_type", None)
    if OWNER.exists():
        owner = json.loads(OWNER.read_text())
    else:
        owner = app.bootstrap("authorized-full-loop-learner")
        owner["funding_id"] = app.runtime.create_funding(
            owner["principal_id"], owner["namespace_id"], 200, hours=8
        )
        OWNER.write_text(json.dumps(owner))
        OWNER.chmod(0o600)

    def save(stage, value):
        report["stages"][stage] = value
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"stage": stage, "value": value}, ensure_ascii=False), flush=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://local/api/v1",
        headers={"Authorization": "Bearer " + owner["token"]},
    ) as api:

        async def call(method, path, value=None):
            response = await api.request(method, path, json=value)
            if response.is_error:
                raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
            return response.json()

        if not report.get("conversation_id"):
            conv = await call("POST", "/conversations", {})
            report["conversation_id"] = conv["conversation_id"]
        cid = report["conversation_id"]
        path = "/conversations/" + cid

        async def message(text, **kwargs):
            current = await call("GET", path)
            return await call(
                "POST",
                path + "/messages",
                {
                    "text": text,
                    "expected_focus_revision": current["revision"],
                    "idempotency_key": uuid4().hex,
                    **kwargs,
                },
            )

        try:
            if "jd_import" not in report["stages"]:
                imported = await message(
                    "岗位要求：了解 Tool Calling 工具调用的参数校验、执行和结果回填。Python 或 TypeScript 任选其一，Redis 加分，不要求模型微调。",
                    kind="jd",
                    authorization_ref=owner["funding_id"],
                )
                save("jd_import", imported)
            await app.drain(limit=30)
            progress = await call("GET", path + "/workflow-status")
            if "jd_extracted" not in report["stages"]:
                save("jd_extracted", progress)
            if not progress["goal_version_id"]:
                raise RuntimeError("JD extraction did not produce a goal")
            if "research_admission" not in report["stages"]:
                admitted = await message(
                    "确认目标",
                    authorization_ref=owner["funding_id"],
                    build_call_cap=48,
                    build_wall_time_contract="unlimited-build-time-v1",
                    research_question="请在 GitHub 检索公开仓库 youngyangyang04/KamaClaude，固定本次实际 commit，分析 Tool Calling 的参数校验、工具执行与结果回填。逐条给出已读源码和条件，独立审核后只发布有证据的局部事实。",
                )
                save("research_admission", admitted)
            await app.drain(limit=200)
            current = await call("GET", path + "/workflow-status")
            save("after_research", current)
            if (
                not current["publication_count"]
                and "evidence_retry_admission" not in report["stages"]
            ):
                prior_run = report["stages"]["research_admission"]["run_refs"][0]
                sources = (app.runtime.stage(prior_run, "sources") or {}).get("source_ids", [])
                if not sources:
                    raise RuntimeError("GitHub research produced no pinned source")
                retry = await message(
                    "补证重试",
                    authorization_ref=owner["funding_id"],
                    source_id=sources[0],
                    auto_evidence_retry=True,
                    build_call_cap=48,
                    build_wall_time_contract="unlimited-build-time-v1",
                )
                if retry["action_receipts"][0]["status"] != "applied":
                    raise RuntimeError(str(retry))
                save("evidence_retry_admission", retry)
            await app.drain(limit=200)
            after_retry = await call("GET", path + "/workflow-status")
            if (
                after_retry["goal_version_id"]
                != report["stages"]["jd_extracted"]["goal_version_id"]
            ):
                raise RuntimeError("Retry changed the original GoalVersion")
            save("after_evidence_retry", after_retry)
            from full_loop_business import continue_business

            final = await continue_business(app, owner, call, path, report, save)
            report["status"] = "business_loop_settled_without_promotion"
            report.pop("error", None)
            report.pop("error_type", None)
            save("business_cursor", final)
        except Exception as error:
            report.update(status="blocked", error_type=type(error).__name__, error=str(error))
        finally:
            with app.db.read() as db:
                report["runs"] = [
                    {
                        "id": r.id,
                        "workflow": r.workflow,
                        "status": r.status,
                        "calls": r.calls_consumed,
                        "result": app.runtime.stage(r.id, "result"),
                        "failure_codes": [
                            e.payload.get("code")
                            for e in db.scalars(
                                select(RunEvent).where(
                                    RunEvent.run_id == r.id, RunEvent.event_type == "run.finished"
                                )
                            )
                        ],
                    }
                    for r in db.scalars(select(AgentRun))
                ]
                report["receipts"] = [
                    {
                        "run_id": r.run_id,
                        "stage": r.stage_key,
                        "role": r.role,
                        "status": r.status,
                        "usage": r.usage,
                    }
                    for r in db.scalars(select(ModelReceipt))
                ]
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            app.db.engine.dispose()
    print(
        json.dumps({"status": report["status"], "error": report.get("error")}, ensure_ascii=False),
        flush=True,
    )
    return 0 if report["status"] == "business_loop_settled_without_promotion" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
