"""Import the explicitly synthetic research into an existing demo account via HTTP APIs."""

import argparse
import json
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--email", default="v1-demo-20261012@example.test")
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT / "private/mock_research/agent_career_20261012/agent_career_mock.json",
    )
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    if data.get("is_synthetic") is not True:
        raise SystemExit("Only explicitly synthetic research is allowed")
    receipt_path = args.input.with_name("import_receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    identity = {
        "dataset_id": data["dataset_id"],
        "email": args.email,
        "base_url": args.base_url,
    }
    if receipt and receipt["identity"] != identity:
        raise SystemExit("Receipt belongs to a different import target")
    receipt.setdefault("identity", identity)
    receipt.setdefault("steps", {})

    def save():
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(
        base_url=args.base_url + "/api/v1",
        timeout=30,
        headers={"Origin": args.base_url},
    ) as client:
        response = client.post(
            "/auth/login",
            json={"email": args.email, "password": "SyntheticOfflineDemo2026"},
        )
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = client.get("/auth/csrf").json()["data"]["csrf_token"]

        def get(path):
            r = client.get(path)
            r.raise_for_status()
            return r.json()["data"]

        def post(label, path, body):
            if label in receipt["steps"]:
                return receipt["steps"][label]
            key = str(uuid5(NAMESPACE_URL, json.dumps([identity, label], sort_keys=True)))
            r = client.post(path, json=body, headers={"Idempotency-Key": key})
            if r.status_code not in {200, 201, 202}:
                raise RuntimeError(f"{label}: {r.status_code} {r.text[:800]}")
            value = r.json()["data"]
            receipt["steps"][label] = value
            save()
            return value

        def run(label, path, body):
            created = post(label, path, body)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                saved = get("/runs/" + created["run_id"])
                if saved["status"] in {"succeeded", "partially_succeeded"}:
                    return saved["output_refs"][-1]
                if saved["status"] in {
                    "failed",
                    "cancelled",
                    "budget_stopped",
                    "waiting_user",
                }:
                    raise RuntimeError(f"{label} stopped: {saved['status']}")
                time.sleep(0.5)
            raise RuntimeError(f"{label} queue timeout; rerun with the same receipt to resume")

        goal = post(
            "goal",
            "/career-goals",
            {
                "role_direction": "Agent 开发 · 五岗位模拟调研与六周计划",
                "job_type": "unspecified",
                "experience_constraints": "实习或初级应用岗仅为模拟假设；本人经验待确认",
                "time_budget": {"weekly_hours": None},
                "user_objectives": [
                    "查看模拟岗位分析与六周计划",
                    "只读使用 joblearn，不认定本人能力",
                    "每周 12 小时仅为排期假设，计划暂不采纳",
                ],
            },
        )
        goal_id = goal["goal_version_id"]
        sources = []
        for job in data["jobs"]:
            value = post(
                job["id"],
                "/sources/import",
                {
                    "source_type": "job_posting",
                    "title": "【模拟】" + job["extraction"]["title"],
                    "content": job["raw_text"],
                    "declared_provenance": "人工构造的虚构 JD，无真实企业、链接和市场代表性；"
                    + data["dataset_id"],
                },
            )
            sources.append({"mock_id": job["id"], "title": job["extraction"]["title"], **value})
        evidences = []
        for e in data["personal_evidence"]:
            value = post(
                e["id"],
                "/evidence",
                {
                    "kind": "project_description",
                    "title": e["project"] + " · 文档材料，本人贡献待验证",
                    "description": json.dumps(e, ensure_ascii=False, indent=2),
                    "claimed_skills": [],
                    "declared_verification": "self_reported",
                },
            )
            evidences.append(value)
        market = run(
            "market_run",
            "/market-research-runs",
            {
                "goal_version_id": goal_id,
                "source_snapshot_ids": [s["source_snapshot_id"] for s in sources],
            },
        )
        aligned = run(
            "alignment_run",
            "/alignment-runs",
            {
                "goal_version_id": goal_id,
                "market_report_id": market["market_report_id"],
                "personal_evidence_version_ids": [e["evidence_version_id"] for e in evidences],
            },
        )
        objectives = {}
        for w in data["plan"]["weeks"]:
            criteria = [
                {
                    "dimension": c["dimension"],
                    "scenario": c["scenario"],
                    "expected_behavior": c["pass_rule"],
                    "pass_rule": c["pass_rule"],
                    "fail_rule": c["fail_rule"],
                    "unknown_rule": c["unknown_rule"],
                    "critical": True,
                    "allowed_methods": ["human_review", "user_reported"],
                    "required_artifact_types": [
                        "explanation_answer" if c["dimension"] == "explanation" else "code_artifact"
                    ],
                    "limitations": ["模拟计划；材料和本人讲解均尚未提交，不代表已通过"],
                }
                for c in w["criteria"]
            ]
            objectives[w["id"]] = post(
                w["id"],
                "/learning-objectives",
                {
                    "goal_version_id": goal_id,
                    "title": "【模拟计划】" + w["id"] + " " + w["title"],
                    "target_behavior": "；".join(w["activities"]),
                    "scope": "离线、只读岗位助手学习；每周 12 小时为假设，尚未采纳",
                    "prerequisite_objective_ids": [
                        objectives[i]["objective_id"] for i in w["prerequisites"]
                    ],
                    "effort_hours": w["effort_hours"],
                    "deliverables": w["deliverables"],
                    "criteria": criteria,
                    "scope_confirmed": True,
                },
            )
        base = get("/plans/" + aligned["plan_id"])
        plan = post(
            "six_week_plan",
            "/plans/" + base["plan_id"] + "/versions",
            {
                "expected_version": base["version"],
                "base_plan_version_id": base["plan_version_id"],
                "objective_version_ids": [x["objective_version_id"] for x in objectives.values()],
                "adjustment_reason": (
                    "导入用户请求的六周模拟计划；"
                    "替换规则生成的草案目标，不认定本人能力"
                ),
                "deferred": ["真实模型调用与效果验证", "真实岗位采样", "本人能力验收"],
                "user_constraints": {
                    "available_hours": 72,
                    "notes": "6 周 × 每周 12 小时仅为模拟假设，需本人确认后采纳",
                },
            },
        )
        report_body = (
            args.input.with_name("agent_research_report.txt")
            .read_text(encoding="utf-8")
            .replace(
                "本轮未写入数据库。",
                "原始生成阶段未写入数据库；本次已通过各模块 API 完成导入。",
            )
        )
        view_data = {
            "schema_version": "1.0",
            "dataset_id": data["dataset_id"],
            "is_synthetic": True,
            "title": "Agent 开发方向 · 个人模拟调研",
            "created_at": data["created_at"],
            "provenance": data["provenance"],
            "report_text": report_body,
            "skill_frequency": data["market_analysis"]["skill_frequency"],
            "personal_evidence": data["personal_evidence"],
            "alignment": data["alignment"],
            "evaluation": data["evaluation"],
            "practice_mock": data["practice_mock"],
            "weeks": [
                {
                    **w,
                    "objective_id": objectives[w["id"]]["objective_id"],
                    "objective_version_id": objectives[w["id"]]["objective_version_id"],
                }
                for w in data["plan"]["weeks"]
            ],
            "sources": sources,
            "goal_version_id": goal_id,
            "market_report_id": market["market_report_id"],
            "alignment_report_id": aligned["alignment_report_id"],
            "plan_id": plan["plan_id"],
            "plan_version_id": plan["plan_version_id"],
        }
        description = json.dumps(view_data, ensure_ascii=False, separators=(",", ":"))
        if len(description) > 20000:
            raise RuntimeError("Dashboard document exceeds evidence payload limit")
        dashboard = post(
            "dashboard",
            "/evidence",
            {
                "kind": "mock_research_summary",
                "title": "【模拟调研】Agent 开发 · 五岗位分析与六周学习计划",
                "description": description,
                "claimed_skills": [],
                "declared_verification": "self_reported",
            },
        )
        # Minimal import verification: read the persisted summary and plan once.
        saved = get("/evidence/" + dashboard["evidence_id"])
        saved_plan = get("/plans/" + plan["plan_id"])
        if (
            json.loads(saved["versions"][-1]["description"])["dataset_id"] != data["dataset_id"]
            or len(saved_plan["objectives"]) != 6
        ):
            raise RuntimeError("Import result mismatch")
        receipt["result"] = {
            "dashboard_url": args.base_url + "/mock-research?report=" + dashboard["evidence_id"],
            "email": args.email,
            "evidence_id": dashboard["evidence_id"],
            "goal_version_id": goal_id,
            "plan_id": plan["plan_id"],
            "sources": len(sources),
            "objectives": len(objectives),
            "plan_status": "draft",
            "real_model_calls": 0,
            "personal_ability": "unknown",
        }
        save()
        print(json.dumps(receipt["result"], ensure_ascii=False))


if __name__ == "__main__":
    main()
