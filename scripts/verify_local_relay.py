"""One resumable native HTTP -> real provider -> published evidence smoke test."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.models import AgentRun, AnswerBlock, ModelReceipt, Turn


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="initial", choices=["initial", "scoped", "rag"])
    case = parser.parse_args().case
    settings = Settings.from_env()
    state_dir = settings.state_dir
    report_path = state_dir / ("relay-smoke.json" if case == "initial" else f"relay-smoke-{case}.json")
    state = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "model": settings.model_name,
        "base_url": settings.model_base_url,
        "question": "根据已发布知识解释：create_progress_ledger 对未知 next_speaker 的重试耗尽后，会保证返回有效执行者吗？编排器为何还要再次校验？请引用源码依据。" if case == "initial" else "根据已发布知识解释 microsoft/agent-framework 项目中 create_progress_ledger 的 next_speaker 重试耗尽行为及编排器的再次校验逻辑。",
    }
    if case == "rag" and "run_id" not in state:
        state["question"] = "请解释RRF排名融合和候选裁剪各自的作用"

    def save():
        report_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if state["status"] in {"completed", "failed"}:
        print(json.dumps({k: state[k] for k in ["status", "run_id", "run_status", "turn_status", "calls"]}, ensure_ascii=False))
        return
    save()
    client_config = json.loads((state_dir / "client.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=settings.api_url, headers={"Authorization": "Bearer " + client_config["token"]}, timeout=60, trust_env=False) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        state["health"] = request("GET", "/health/ready")
        me = request("GET", "/api/v1/me")
        state["authenticated"] = me["principal_id"] == client_config["principal_id"]
        if "funding_id" not in state:
            state.update(request("POST", "/api/v1/funding", {
                "namespace_id": client_config["namespace_id"], "call_cap": 8,
                "allowed_roles": ["planner", "coordinator", "tutor", "answer_checker", "query_rewriter", "coverage", "evaluation", "extract", "reflector"],
                "hours": 1, "data_classes": ["public_source", "private_source", "private_jd", "learning_state"],
                "confirm": "AUTHORIZE",
            }))
            save()
        if "conversation_id" not in state:
            state.update(request("POST", "/api/v1/conversations", {"auto_build": False}))
            save()
        if "run_id" not in state:
            conversation = request("GET", "/api/v1/conversations/" + state["conversation_id"])
            state.update(request("POST", f"/api/v1/conversations/{state['conversation_id']}/turns", {
                "question": state["question"], "expected_revision": conversation["revision"],
                "funding_id": state["funding_id"], "idempotency_key": "local-relay-smoke-20261116-" + case,
            }))
            save()
        print(json.dumps({"status": "running", "run_id": state["run_id"]}), flush=True)
        application = Application(settings)
        started = time.monotonic()
        # Execute only this newly authorized run, never historical pending jobs.
        await application.execute(state["run_id"])
        with application.db.read() as db:
            run = db.get(AgentRun, state["run_id"])
            turn = db.get(Turn, state["turn_id"])
            blocks = [{"id": block.id, "kind": block.kind, "content": block.content} for block in db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn.id).order_by(AnswerBlock.ordinal))]
            receipts = [{"id": receipt.id, "role": receipt.role, "status": receipt.status, "usage": receipt.usage} for receipt in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))]
            state.update(run_status=run.status, turn_status=turn.status, calls=run.calls_consumed, receipts=receipts, blocks=blocks, result=application.runtime.stage(run.id, "result"))
        state["elapsed_seconds"] = round(time.monotonic() - started, 2)
        state["tutor_called"] = any(receipt["role"] == "tutor" and receipt["status"] == "completed" for receipt in receipts)
        state["status"] = "completed" if state["run_status"] == "succeeded" and state["turn_status"] == "completed" else "failed"
        state["finished_at"] = datetime.now(UTC).isoformat()
        save()
        request("DELETE", "/api/v1/funding/" + state["funding_id"])
        state["funding_revoked_after_test"] = True
        save()
        application.db.engine.dispose()
        print(json.dumps({k: state[k] for k in ["status", "run_id", "run_status", "turn_status", "calls", "elapsed_seconds"]}, ensure_ascii=False), flush=True)
        if state["status"] != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
