"""Reassess frozen plans within explicitly reserved parent calls. No command execution."""

from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import UUID

from careerforge.application import coverage_service as coverage
from careerforge.application import repository_service as repositories
from careerforge.application import study_service as study
from careerforge.application import study_validation as validation
from careerforge.application.run_service import artifact, commit_stage, event, transition
from careerforge.application.study_versions import family
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.schemas.study import PlanOutput, ReconciliationOutput

if TYPE_CHECKING:
    from careerforge.workflows.study import Runner


def selected_keys(before: list[Json], current: list[Json], original: list[str]) -> list[str]:
    if set(original) == {r["key"] for r in before}:
        return [r["key"] for r in current]
    names = {r["technology"].casefold() for r in before if r["key"] in original}
    return [
        r["key"] for r in current if r["key"] in original or r["technology"].casefold() in names
    ]


async def reconcile(runner: "Runner") -> None:
    ids = runner.m.get("recheck_plan_ids", [])
    allowance = runner.m.get("recheck_call_budget", 0)
    pending: list[str] = []
    original_role = runner.m["role"]
    runner.m["role"] = "reconcile"
    for number, identifier in enumerate(ids):
        key = "rechecked:" + identifier
        with runner.factory.begin() as db:
            run = runner.run(db)
            if artifact(db, run, key, runner.version):
                continue
            row = study.track(db, run.owner_id, UUID(runner.m["track_id"]))
            plan = study.artifact_row(db, run.owner_id, row.id, identifier, "plan")
            try:
                study.validate_plan_scope(db, row, plan)
            except BusinessError:
                pending.append(identifier)
                continue
            if number >= allowance:
                pending.append(identifier)
                continue
            current = study.current_requirements(db, row)
            before = study.artifact_row(
                db, run.owner_id, row.id, plan.payload["requirements_id"], "requirements"
            )
            keys = selected_keys(
                before.payload["requirements"],
                current.payload["requirements"],
                plan.payload["intent"].get(
                    "requirement_keys", [r["key"] for r in before.payload["requirements"]]
                ),
            )
            latest_reviews = {
                v.payload["candidate"]["github_url"]: v for v in study.current_reviews(db, row)
            }
            projects, source_map = [], {}
            for rid in plan.payload["review_ids"]:
                review = study.artifact_row(db, run.owner_id, row.id, rid, "review")
                sources = [
                    repositories.citation_detail(db, run.owner_id, UUID(h["citation_id"]))
                    for h in review.payload["highlights"][:6]
                ]
                # Only these bounded, literal windows are available to the reassessment.
                sources = [v for v in sources if len(v["quote"]) <= 3500]
                source_map[rid] = sources
                latest = latest_reviews.get(review.payload["candidate"]["github_url"])
                feedback = (
                    {
                        "review_id": str(latest.id),
                        "verdict": latest.payload["verdict"],
                        "admitted": latest.payload["admitted"],
                        "summary": latest.payload["summary"][:1200],
                        "limitations": latest.payload.get("limitations", [])[:4],
                        "counterevidence": latest.payload.get("evidence_validation", {}).get(
                            "counterevidence", []
                        )[:3],
                        "verification": "prior_review_unverified_recheck_against_read_sources",
                    }
                    if latest
                    else None
                )
                projects.append(
                    {
                        "review_id": rid,
                        "candidate": review.payload["candidate"],
                        "previous_summary_unverified": review.payload["summary"],
                        "latest_review_feedback": feedback,
                        "read_sources": sources,
                    }
                )
            frozen = {
                "requirements_id": str(current.id),
                "requirements": current.payload["requirements"],
                "intent": {**plan.payload["intent"], "requirement_keys": keys},
            }
            context = {
                "previous_plan": {
                    k: plan.payload[k] for k in ("title", "summary", "modules", "limitations")
                },
                "current_requirements": frozen["requirements"],
                "intent": frozen["intent"],
                "projects": projects,
                "runtime_capabilities": plan.payload.get("runtime_capabilities", {}),
                "scope": "检查需求、项目匹配和覆盖；仅引用 read_sources，缺依据用 needs_evidence。",
            }
        output = await runner.ask("recheck-model:" + identifier, context, ReconciliationOutput)
        with runner.factory.begin() as db:
            run = runner.run(db)
            row = study.track(db, run.owner_id, UUID(runner.m["track_id"]), True)
            if row.payload.get("requirements_id") != frozen["requirements_id"]:
                raise BusinessError(409, "RECHECK_SCOPE_CHANGED", "需求再次变化，停止发布重审。")
            plan = study.artifact_row(db, run.owner_id, row.id, identifier, "plan")
            # One savepoint keeps malformed model proposals from partially publishing reviews/plans.
            try:
                with db.begin_nested():
                    proposed = {p.review_id: p.review for p in output.project_reviews}
                    if set(proposed) != set(plan.payload["review_ids"]) or not keys:
                        raise ValueError("recheck must cover the original project scope")
                    updated_reviews, review_map, citation_map = [], {}, {}
                    for rid, model_review in proposed.items():
                        previous = study.artifact_row(db, run.owner_id, row.id, rid, "review")
                        sources = source_map[rid]
                        reading = {
                            "code": [
                                {
                                    "path": v["path"],
                                    "content": v["quote"],
                                    "start_line": v["start_line"],
                                }
                                for v in sources
                            ],
                            "coverage": {"scope": "bounded_reassessment"},
                        }
                        checked, matches, audit = validation.partial_review(
                            model_review, frozen, reading, True
                        )
                        candidate = {
                            **previous.payload["candidate"],
                            "repository_snapshot_id": previous.payload["repository_snapshot_id"],
                        }
                        review = study.publish_review(
                            db,
                            run,
                            row,
                            {**frozen, "candidate_id": previous.payload["candidate_id"]},
                            {
                                "candidate": candidate,
                                "origin": "user_import",
                                "files": {},
                                "skipped": {},
                            },
                            checked,
                            reading,
                            matches,
                            runner.synthetic,
                            evidence_validation=audit,
                        )
                        if review.payload["admitted"]:
                            updated_reviews.append(study.packed(review))
                            review_map[rid] = str(review.id)
                            for old in sources:
                                match = next(
                                    (
                                        h
                                        for h in review.payload["highlights"]
                                        if h["path"] == old["path"]
                                        and old["start_line"]
                                        <= h["start_line"]
                                        <= h["end_line"]
                                        <= old["end_line"]
                                    ),
                                    None,
                                )
                                if match:
                                    citation_map[old["citation_id"]] = match["citation_id"]
                    report = coverage.coverage(
                        [r for r in frozen["requirements"] if r["key"] in keys], updated_reviews
                    )
                    generated = None
                    status = output.status
                    if report["missing_keys"] or current.payload.get("unresolved"):
                        status = "needs_evidence"
                    if status != "needs_evidence":
                        # Explicit strict counts remain gates even during automatic reassessment.
                        planning_scope = coverage.require_plan_coverage(
                            row.payload,
                            frozen["requirements"],
                            updated_reviews,
                            set(keys),
                            len(study.approved(db, row)),
                            True,
                        )
                        value = (
                            output.plan.model_dump()
                            if output.plan
                            else {
                                k: deepcopy(plan.payload[k])
                                for k in ("title", "summary", "modules", "limitations")
                            }
                        )
                        for module in value["modules"]:
                            module["project_review_ids"] = [
                                review_map.get(v, v) for v in module["project_review_ids"]
                            ]
                            module["highlight_ids"] = [
                                citation_map.get(v, v) for v in module["highlight_ids"]
                            ]
                        focus = deepcopy(plan.payload.get("focus_highlights", []))
                        for point in focus:
                            position = repositories.citation_detail(
                                db, run.owner_id, UUID(point["citation_id"])
                            )
                            if point["requirement_key"] not in keys:
                                point["requirement_key"] = keys[0]
                            for reviewed_project in updated_reviews:
                                if (
                                    reviewed_project["repository_snapshot_id"]
                                    == position["repository_snapshot_id"]
                                ):
                                    reviewed_project["highlights"].append(point)
                        checked_plan = PlanOutput.model_validate(value)
                        modules = validation.plan_modules(
                            checked_plan,
                            {**frozen, "projects": updated_reviews, "focus_highlights": focus},
                        )
                        from careerforge.infrastructure.runtime_capabilities import planning

                        runtime = planning(
                            frozen["intent"],
                            frozen["requirements"],
                            updated_reviews,
                            runner.m.get("runtime_capabilities"),
                        )
                        # Acceptance never carries to changed conditions.
                        from careerforge.application.study_versions import (
                            module_signature,
                            requirement_signature,
                        )

                        unchanged = {
                            m["key"]
                            for m in modules
                            if any(
                                m["key"] == old["key"]
                                and module_signature(db, run.owner_id, m)
                                == module_signature(db, run.owner_id, old)
                                and requirement_signature(
                                    before.payload["requirements"], old["requirement_keys"]
                                )
                                == requirement_signature(
                                    frozen["requirements"], m["requirement_keys"]
                                )
                                for old in plan.payload["modules"]
                            )
                        }
                        generated = study.put(
                            db,
                            row,
                            "plan",
                            {
                                **checked_plan.model_dump(),
                                "modules": modules,
                                **frozen,
                                "intent": {
                                    **frozen["intent"],
                                    "project_review_ids": [v["id"] for v in updated_reviews],
                                },
                                "review_ids": [v["id"] for v in updated_reviews],
                                "focus_highlights": focus,
                                "source_plan_id": str(plan.id),
                                "plan_family_id": family(db, plan),
                                "revision_no": plan.payload.get("revision_no", 1) + 1,
                                "module_outcomes": {
                                    k: v
                                    for k, v in plan.payload.get("module_outcomes", {}).items()
                                    if k in unchanged
                                },
                                "historical_outcomes": [
                                    *plan.payload.get("historical_outcomes", []),
                                    *[
                                        {**v, "needs_review": True}
                                        for k, v in plan.payload.get("module_outcomes", {}).items()
                                        if k not in unchanged
                                    ],
                                ],
                                "planning_scope": planning_scope,
                                "runtime_capabilities": runtime,
                                "update_kind": "agent_reconciliation",
                                "synthetic": runner.synthetic,
                            },
                            run.id,
                        )
                        if row.payload.get("active_plan_id") == identifier:
                            row.payload = {**row.payload, "active_plan_id": str(generated.id)}
                    record = {
                        "source_plan_id": identifier,
                        "plan_family_id": family(db, plan),
                        "requirements_id": frozen["requirements_id"],
                        "status": status,
                        "summary": output.summary,
                        "missing_keys": report["missing_keys"],
                        "new_plan_id": str(generated.id) if generated else None,
                        "limitations": output.limitations,
                        "semantic_support": "agent_reassessment_not_independent_verification",
                    }
            except (ValueError, BusinessError) as error:
                record = {
                    "source_plan_id": identifier,
                    "requirements_id": frozen["requirements_id"],
                    "status": "needs_evidence",
                    "summary": "重审提案未通过范围或原文校验，旧计划保留。",
                    "error": getattr(error, "code", "INVALID_RECHECK_PROPOSAL"),
                }
            saved = study.put(db, row, "reconciliation", record, run.id)
            row.revision += 1
            commit_stage(
                db,
                runner.id,
                runner.epoch,
                runner.worker,
                key,
                runner.version,
                {"artifact_id": str(saved.id)},
            )
            event(
                db,
                run,
                "study.reconciliation",
                {"status": record["status"], "source_plan_id": identifier},
            )
    with runner.factory.begin() as db:
        run = runner.run(db)
        row = study.track(db, run.owner_id, UUID(runner.m["track_id"]), True)
        row.payload = {**row.payload, "recheck_pending": pending}
        row.revision += 1
        commit_stage(
            db,
            runner.id,
            runner.epoch,
            runner.worker,
            "result",
            runner.version,
            {"primary_role": original_role, "recheck_pending": pending},
        )
        from sqlalchemy import select

        from careerforge.infrastructure.db.study_models import StudyArtifact

        incomplete = db.scalar(
            select(StudyArtifact.id)
            .where(
                StudyArtifact.run_id == run.id,
                StudyArtifact.kind == "reconciliation",
                StudyArtifact.payload["status"].astext == "needs_evidence",
            )
            .limit(1)
        )
        transition(db, run, "partially_succeeded" if pending or incomplete else "succeeded")
