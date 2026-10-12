"""Six-module engineering demo. Synthetic submissions are NOT evidence of learning."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

from careerforge.api.app import app
from careerforge.infrastructure.queue.outbox_dispatcher import dispatch_once
from careerforge.workflows.registry import execute_registered
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--create-synthetic-account", action="store_true")
    parser.add_argument("--joblearn", type=Path)
    parser.add_argument("--queue", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "private/v1_demo.json")
    args = parser.parse_args()
    client = TestClient(app)
    client.headers["Origin"] = "http://127.0.0.1:5173"
    password = "SyntheticOfflineDemo2026"
    if not args.create_synthetic_account:
        raise SystemExit("Use a dedicated synthetic demo account via --create-synthetic-account")
    credentials = {"email": args.email, "password": password}
    response = client.post("/api/v1/auth/register", json=credentials)
    if response.status_code not in {201, 409}:
        raise RuntimeError("Could not create synthetic account")
    assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
    client.headers["X-CSRF-Token"] = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
    trace = []

    def post(path, body):
        response = client.post(
            "/api/v1" + path, json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(f"{path}: {response.status_code} {response.text[:600]}")
        return response.json()["data"]

    def get(path):
        response = client.get("/api/v1" + path)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def run(path, body):
        created = post(path, body)
        identifier = UUID(created["run_id"])
        if args.queue:
            dispatch_once(only_run_id=identifier)
        else:
            execute_registered(identifier, "offline-demo")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            saved = get("/runs/" + str(identifier))
            if saved["status"] in {"succeeded", "partially_succeeded"}:
                trace.append(
                    {
                        "run_id": str(identifier),
                        "type": saved["run_type"],
                        "status": saved["status"],
                        "output_refs": saved["output_refs"],
                    }
                )
                return saved["output_refs"][-1]
            if saved["status"] in {"failed", "cancelled", "budget_stopped", "waiting_user"}:
                raise RuntimeError(f"Demo run stopped: {saved['status']}")
            time.sleep(0.25)
        raise RuntimeError("Demo queue deadline exceeded")

    goal = post(
        "/career-goals",
        {
            "role_direction": "Agent开发 · 合成流程演示",
            "job_type": "internship",
            "user_objectives": ["仅用于工程闭环演示，不代表本人掌握"],
        },
    )
    target = goal["goal_version_id"]
    base = ROOT / "evals/datasets/manual_case"
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    sources = []
    for record in manifest["job_sources"]:
        body = (base / record["content_path"]).read_text(encoding="utf-8")
        assert hashlib.sha256(body.encode()).hexdigest() == record["sha256"]
        item = post(
            "/sources/import",
            {
                "source_type": "job_posting",
                "title": "S00保存的官方JD",
                "source_url": record["source_url"],
                "content": body,
                "declared_provenance": "已保存正文；未重新联网",
            },
        )
        sources.append(item["source_snapshot_id"])
    material = {
        "kind": "project_description",
        "title": "joblearn 静态材料，贡献与运行待确认",
        "description": "用户指定的个人项目。只读取授权清单中的文件；不执行；不据此认定能力。",
        "attachment_ids": [],
    }
    if args.joblearn:
        allowed = manifest["personal_evidence"]["files"]
        readme = next(r for r in allowed if r["source_path"].lower().endswith("readme.md"))
        raw = (args.joblearn / readme["source_path"]).read_bytes()
        upload = client.post(
            "/api/v1/attachments",
            data={"purpose": "personal_evidence"},
            files={"file": ("joblearn-readme.md", raw, "text/markdown")},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert upload.status_code == 201, upload.text
        material["attachment_ids"] = [upload.json()["data"]["attachment_id"]]
    evidence = post("/evidence", material)
    market = run(
        "/market-research-runs", {"goal_version_id": target, "source_snapshot_ids": sources}
    )
    alignment = run(
        "/alignment-runs",
        {
            "goal_version_id": target,
            "market_report_id": market["market_report_id"],
            "personal_evidence_version_ids": [evidence["evidence_version_id"]],
        },
    )
    plan = get("/plans/" + alignment["plan_id"])
    obj = plan["objectives"][0]
    adopted = post(
        "/plans/" + plan["plan_id"] + "/adopt",
        {
            "expected_version": plan["version"],
            "plan_version_id": plan["plan_version_id"],
            "expected_goal_version_id": target,
            "selected_objective_version_ids": [obj["objective_version_id"]],
        },
    )
    catalog = get("/repository-catalog")[0]
    repository = post(
        "/repositories",
        {"github_url": catalog["github_url"], "requested_ref": catalog["requested_ref"]},
    )
    researched = run(
        "/project-research-runs",
        {
            "objective_version_ids": [obj["objective_version_id"]],
            "candidate_repository_ids": [repository["repository_id"]],
        },
    )
    selection = post(
        "/project-selections",
        {
            "recommendation_id": researched["recommendation_ids"][0],
            "objective_version_ids": [obj["objective_version_id"]],
            "expected_goal_version_id": target,
        },
    )
    learned = run(
        "/learning-path-runs",
        {
            "selection_id": selection["selection_id"],
            "objective_version_ids": [obj["objective_version_id"]],
            "criterion_version_ids": obj["criterion_version_ids"],
        },
    )
    unit = get("/learning-units/" + learned["learning_unit_id"])
    draft = unit["task_drafts"][0]
    project = post(
        "/build-projects",
        {
            "title": "合成工程演示，不是 joblearn 实验",
            "problem_statement": "只验证返修、补交和版本门槛；不能作为本人学习成果",
        },
    )
    task = post(
        "/build-projects/" + project["build_project_id"] + "/tasks",
        {
            "title": "合成迁移任务",
            "description": "所有执行材料均为合成fixture，未实际运行或证明理解",
            "objective_version_id": obj["objective_version_id"],
            "criterion_version_ids": obj["criterion_version_ids"],
            "deliverables": draft["deliverables"],
            "learning_unit_id": unit["learning_unit_id"],
            "task_draft_id": draft["task_draft_id"],
        },
    )

    def submit(complete):
        attachments = []
        records = []
        answers = []
        transfer = []
        if complete:
            upload = client.post(
                "/api/v1/attachments",
                data={"purpose": "submission"},
                files={
                    "file": (
                        "synthetic.log",
                        b"SYNTHETIC FIXTURE ONLY. No command executed. No learning claim.",
                        "text/plain",
                    )
                },
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert upload.status_code == 201, upload.text
            attachments = [upload.json()["data"]["attachment_id"]]
            records = [
                {
                    "command_text": "SYNTHETIC NOT EXECUTED",
                    "reported_exit_code": 0,
                    "output_attachment_id": attachments[0],
                    "environment_refs": ["synthetic"],
                    "reported_at": "2026-10-12",
                    "execution_provenance": "user_reported",
                }
            ]
            answers = [
                {
                    "question": "合成演示",
                    "answer": "此内容只用于验证schema与事务，不证明理解",
                    "criterion_version_ids": task["criterion_version_ids"],
                }
            ]
            transfer = ["合成变体说明，不是本人真实迁移"]
        return post(
            "/build-tasks/" + task["task_id"] + "/submissions",
            {
                "expected_version": task["version"],
                "task_version_id": task["current_task_version_id"],
                "criterion_version_ids": task["criterion_version_ids"],
                "summary": "合成完整材料" if complete else "合成缺材料提交",
                "code_commit": "a" * 40,
                "attachment_ids": attachments,
                "environment": {"description": "synthetic"},
                "execution_records": records,
                "explanation_answers": answers,
                "transfer_notes": transfer,
            },
        )

    for complete in [False, True]:
        saved = submit(complete)
        task = get("/build-tasks/" + task["task_id"])
        result = run(
            "/submissions/" + saved["submission_id"] + "/assessment-runs",
            {"criterion_version_ids": task["criterion_version_ids"]},
        )
        proposal = get("/assessments/" + result["assessment_id"])
        decisions = [
            {
                "criterion_version_id": i,
                "verdict": "pass" if complete else "unknown",
                "method": "human_review",
                "reason": "SYNTHETIC 工程流程测试，不认定本人能力",
                "evidence_refs": [],
                "self_review_record": {
                    "procedure": "synthetic fixture",
                    "observed": "synthetic observation",
                    "environment": "synthetic",
                    "code_version": "a" * 40,
                }
                if complete
                else None,
            }
            for i in task["criterion_version_ids"]
        ]
        confirmed = post(
            "/assessments/" + proposal["assessment_id"] + "/confirm",
            {
                "expected_version": proposal["version"],
                "expected_task_version": task["version"],
                "decision": "accept" if complete else "request_revision",
                "criterion_decisions": decisions,
                "scope_acknowledged": True,
            },
        )
        task = get("/build-tasks/" + task["task_id"])
        trace.append(
            {
                "submission_id": saved["submission_id"],
                "assessment_id": proposal["assessment_id"],
                "task_status": confirmed["task_status"],
                "synthetic": True,
            }
        )
    replanned = run(
        "/alignment-runs",
        {
            "goal_version_id": target,
            "market_report_id": market["market_report_id"],
            "personal_evidence_version_ids": [evidence["evidence_version_id"]],
            "previous_plan_version_id": adopted["active_plan_version_id"],
            "assessment_ids": [proposal["assessment_id"]],
        },
    )
    plan2 = get("/plans/" + replanned["plan_id"])
    post(
        "/plans/" + plan2["plan_id"] + "/adopt",
        {
            "expected_version": plan2["version"],
            "plan_version_id": plan2["plan_version_id"],
            "expected_goal_version_id": target,
            "selected_objective_version_ids": [obj["objective_version_id"]],
        },
    )
    assert get("/build-tasks/" + task["task_id"])["status"] == "accepted"
    output = {
        "mode": "offline_synthetic_engineering_demo",
        "queue": args.queue,
        "model_calls_live": 0,
        "authorship_verified": False,
        "learning_verified": False,
        "personal_material": "joblearn" if args.joblearn else "not supplied",
        "trace": trace,
        "task_id": task["task_id"],
        "learning_unit_id": unit["learning_unit_id"],
        "goal_version_id": target,
        "limits": ["合成任务验收不构成真实本人能力证明", "真实模型与私有试用效果未验证"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "workflow_runs": len([r for r in trace if "run_id" in r]),
                "task_status": task["status"],
                "synthetic": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
