#!/usr/bin/env python3
"""Exercise public session budgets/preferences against synthetic product publications.

This is an explicit fixture adapter, not a claim that real public KUs were re-
reviewed or that the plan's semantic quality passed. Only public step06/07 read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from conftest import env as fixture_environment  # noqa: E402
from conftest import publish_fixture  # noqa: E402
from sqlalchemy import select  # noqa: E402
from test_session_plans_v166 import planner  # noqa: E402

from careerforge.contracts_session_plan_v166 import SessionPlanInput, SessionPreferences  # noqa: E402
from careerforge.models import ModelReceipt  # noqa: E402
from careerforge.services.learning_intent import learning_intent  # noqa: E402
from careerforge.util import fingerprint  # noqa: E402


async def replay(args):
    intent_cases = [
        json.loads(line)
        for line in (args.input / "inputs/step06_intent.jsonl").read_text().splitlines()
    ]
    plans = [
        json.loads(line)
        for line in (args.input / "inputs/step07_plan.jsonl").read_text().splitlines()
    ]
    report = {
        "schema_version": "public-session-plan-evidence-1.6.6",
        "test_mode": "synthetic_product_adapter",
        "live_model_calls": 0,
        "semantic_quality_evaluated": False,
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="careerforge-session-") as folder:
        generator = fixture_environment.__wrapped__(Path(folder))
        env = next(generator)
        app, actor = env["app"], env["actor"]
        mapping = {}
        knowledge_ids = sorted(
            {
                identifier
                for case in plans
                for identifier in case.get("available_knowledge_units", [])
            }
        )
        for number, identifier in enumerate(knowledge_ids):
            source, publication, _ = await publish_fixture(
                env, f"session-fixture-{number}", f"fixtures/session-{number}", f"{number + 1:040x}"
            )
            mapping[identifier] = {
                "source_id": source,
                "publication_id": publication,
                "synthetic_echo_knowledge_not_original_KU": True,
            }
        report["explicit_fixture_mapping"] = mapping
        intents = {
            case["case_id"]: learning_intent(
                case["utterance"],
                {
                    "project_source_ids": ["synthetic-scope"]
                    if case["context"]["active_repo_id"]
                    else [],
                    "code_focus": bool(case["context"]["code_focus"]),
                },
            )
            for case in intent_cases
        }
        draft_aliases = {}
        for case in plans:
            calls_before = len(env["model"].calls)
            before_active = app.learning.profile(actor)["active_plan_id"]
            if case["task"] == "draft_plan":
                prefs = case["preferences"]
                style = (
                    "code_first"
                    if prefs.get("code_first")
                    else "architecture_first"
                    if prefs.get("architecture_first")
                    else "comparison_first"
                    if prefs.get("compare_projects")
                    else None
                )
                preferences = SessionPreferences(
                    practice=prefs.get("practice"),
                    check_understanding=False if prefs.get("no_quiz") else None,
                    requested_style=style,
                )
                publications = [
                    mapping[key]["publication_id"] for key in case["available_knowledge_units"]
                ]
                value = SessionPlanInput(
                    namespace_id=env["ns"],
                    funding_id=env["funding"] if publications else None,
                    publication_ids=publications,
                    learning_intent=intents[case["intent_ref"]]["intent"],
                    title=f"公开请求 {case['case_id']} 的单次学习草案",
                    total_minutes=case["time_minutes"],
                    preferences=preferences,
                    expected_profile_revision=app.learning.profile(actor)["revision"],
                    idempotency_key=case["case_id"],
                )
                planner(env, max_modules=max(1, case["time_minutes"] // 5))
                admission = app.learning.start_session_plan(actor, value)
                if admission.get("run_id"):
                    await app.execute(admission["run_id"])
                    result = app.runtime.stage(admission["run_id"], "result")
                    with app.db.read() as db:
                        receipt_ids = list(
                            db.scalars(
                                select(ModelReceipt.id).where(
                                    ModelReceipt.run_id == admission["run_id"]
                                )
                            )
                        )
                    detail = app.learning.detail(actor, result["plan_id"]) if result else None
                else:
                    result, detail, receipt_ids = admission, None, []
                # The public confirmation case names this external draft alias.
                # It is a test-fixture identity map, never a production parser rule.
                if case["case_id"] == "C07-02" and result:
                    draft_aliases["PLAN-DRAFT-02"] = {
                        "plan_id": result["plan_id"],
                        "version_id": result["version_id"],
                    }
                report["results"].append(
                    {
                        "case_id": case["case_id"],
                        "stage": "step07_plan",
                        "status": "exercised_with_synthetic_source_substitution",
                        "input_hash": fingerprint(case),
                        "actual_request": value.model_dump(mode="json"),
                        "admission": admission,
                        "output": result,
                        "plan_detail": detail,
                        "model_receipt_ids": receipt_ids,
                        "synthetic_model_calls_for_case": len(env["model"].calls) - calls_before,
                        "active_plan_before": before_active,
                        "active_plan_after": app.learning.profile(actor)["active_plan_id"],
                        "limitations": [
                            "Budget, opt-out, prerequisite, persistence and acceptance mechanics exercised; original public source-unit semantics not evaluated."
                        ],
                    }
                )
            else:
                target = draft_aliases[case["plan_version"]]
                accepted = app.learning.accept_plan(
                    actor, target["plan_id"], target["version_id"], case["expected_revision"]
                )
                report["results"].append(
                    {
                        "case_id": case["case_id"],
                        "stage": "step07_plan",
                        "status": "exercised_with_synthetic_source_substitution",
                        "input_hash": fingerprint(case),
                        "explicit_external_alias_resolution": target,
                        "output": accepted,
                        "synthetic_model_calls_for_case": len(env["model"].calls) - calls_before,
                        "active_plan_before": before_active,
                        "active_plan_after": app.learning.profile(actor)["active_plan_id"],
                        "limitations": [
                            "Confirmation activates the exact generated version and relative module schedule; no calendar event or mastery state is created."
                        ],
                    }
                )
        report["draft_aliases"] = draft_aliases
        try:
            next(generator)
        except StopIteration:
            pass
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"output": str(args.output), "cases": len(report["results"]), "live_model_calls": 0}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(replay(parser.parse_args()))
