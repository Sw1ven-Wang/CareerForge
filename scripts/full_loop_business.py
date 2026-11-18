"""Continue the explicitly authorized integration learner through existing HTTP contracts."""

from sqlalchemy import select

from careerforge.models_dialogue import WorkflowExecution, WorkflowSession, WorkflowTutorRequest
from careerforge.services.evolution_replay import DEVELOPMENT_PROBE_CONTRACT


def applied(result, *, allowed=("applied", "already_applied")):
    receipts = result.get("action_receipts", [])
    if not receipts or any(row["status"] not in allowed for row in receipts):
        raise RuntimeError(str(result))
    return receipts[0]


async def continue_business(app, owner, call, path, report, save, *, scenario=None):
    scenario = scenario or {}
    actor, funding = owner["principal_id"], owner["funding_id"]
    report["development_probe_contract"] = DEVELOPMENT_PROBE_CONTRACT

    async def status():
        return await call("GET", path + "/workflow-status")

    async def practice_command(intent, *, authorize=False):
        view = await status()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, view["workflow_id"])
            execution = workflow.current_execution_id
        result = await call(
            "POST",
            path + "/messages",
            {
                "contract": "practice-dialogue-v1",
                "text": intent["type"],
                "expected_focus_revision": view["focus_revision"],
                "idempotency_key": "full-loop-practice-" + intent["type"],
                "workflow_id": view["workflow_id"],
                "execution_id": execution,
                "expected_workflow_revision": view["workflow_revision"],
                "intent": intent,
                "authorization_ref": funding if authorize else None,
            },
        )
        applied(result, allowed=("applied", "already_applied", "active", "completed"))
        return result

    view = await status()
    if "plan_accepted" not in report["stages"]:
        if not view.get("pending_confirmation"):
            raise RuntimeError("No acceptable reviewed plan: " + str(view))
        result = await call(
            "POST",
            "/workflows/"
            + view["workflow_id"]
            + "/learning-confirmations/"
            + view["pending_confirmation"],
            {
                "expected_revision": view["workflow_revision"],
                "idempotency_key": "full-loop-accept-plan",
                "authorization_ref": funding,
                "tutor_request": {
                    "question": scenario.get(
                        "tutor_question",
                        "请先展示当前已审核源码，讲解它实际证明的工具调用步骤与适用条件。",
                    )
                },
            },
        )
        applied(result)
        save("plan_accepted", result)
    await app.drain(limit=60)
    view = await status()
    wid = view["workflow_id"]

    def tutor_result():
        with app.db.read() as db:
            request = db.scalar(
                select(WorkflowTutorRequest)
                .where(WorkflowTutorRequest.workflow_id == wid)
                .order_by(WorkflowTutorRequest.sequence.desc())
                .limit(1)
            )
            if request is None or request.status != "completed":
                raise RuntimeError(
                    "Tutor did not complete: "
                    + str((request.status, request.failure_code) if request else None)
                )
            return app.answers.events(actor, request.turn_id), request.turn_id

    answer, turn_id = tutor_result()
    if "tutor_initial" not in report["stages"]:
        if not any(
            event["type"] == "answer.block.validated"
            and event["payload"]["content"].get("citations")
            for event in answer["events"]
        ):
            raise RuntimeError("Tutor has no validated source citation")
        save("tutor_initial", answer)
    if "tutor_followup_admitted" not in report["stages"]:
        view = await status()
        result = await call(
            "POST",
            path + "/learning-messages",
            {
                "text": "请解释刚才引用所保留的条件，并指出尚未核验的实现边界。",
                "expected_focus_revision": view["focus_revision"],
                "idempotency_key": "full-loop-followup",
                "authorization_ref": funding,
                "tutor_request": {
                    "question": "请解释刚才引用所保留的条件，并指出尚未核验的实现边界。"
                },
            },
        )
        applied(result)
        save("tutor_followup_admitted", result)
    await app.drain(limit=60)
    answer, turn_id = tutor_result()
    save("tutor_followup", answer)
    if "practice_started" not in report["stages"]:
        profile = app.learning.profile(actor)
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, wid)
            execution = db.get(WorkflowExecution, workflow.current_execution_id)
            module_hash = execution.binding_payload["accepted"]["module_hashes"][
                profile["active_module_id"]
            ]
        result = await practice_command(
            {
                "type": "start_practice",
                "activity": {
                    **scenario.get("activity", {"kind": "understanding_check"}),
                    "module_id": profile["active_module_id"],
                },
                "assistance_mode": "assisted",
                "expected_profile_revision": profile["revision"],
                "module_hash": module_hash,
            }
        )
        save("practice_started", result)
    await app.drain(limit=30)
    pid = report["stages"]["practice_started"]["action_receipts"][0]["practice_id"]

    def detail():
        return app.practice_workflows.detail(actor, report["conversation_id"], pid)

    def target(practice):
        return {
            key: practice[key] for key in ("practice_id", "attempt_id", "specification_hash")
        } | {"practice_hash": practice["request_hash"]}

    practice = detail()
    if "practice_submitted" not in report["stages"]:
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, wid)
            packs = [
                app.knowledge.publication_pack(db, actor, pub)
                for pub in workflow.target["publication_ids"]
            ]
        explanation = (
            "自动联调练习作答，使用 AI 辅助；以下为静态源码说明，未执行学习者代码。依据已审核材料：\n"
            + "\n".join(fact["text"] for pack in packs for fact in pack["facts"])
        )
        explanation += "\n版本：" + ", ".join(pack["commit"] for pack in packs)
        explanation += "\n未知边界：仅覆盖已发布 facts；没有项目运行证明，也不推断未读取部分。"
        save(
            "practice_submitted",
            await practice_command(
                {
                    "type": "submit_practice",
                    **target(practice),
                    "submission": {"response": explanation[:9000], "external_help": "used"},
                }
            ),
        )
    await app.drain(limit=30)
    practice = detail()
    if "assessment_requested" not in report["stages"]:
        save(
            "assessment_requested",
            await practice_command(
                {
                    "type": "request_assessment",
                    **target(practice),
                    "submission_hash": practice["submission_hash"],
                },
                authorize=True,
            ),
        )
    await app.drain(limit=60)
    practice = detail()
    if not practice.get("assessment_candidate"):
        raise RuntimeError(
            "Assessment has no validated candidate: " + str(practice.get("assessment"))
        )
    save("assessment_result", practice)
    if "learner_observation" not in report["stages"]:
        observation = {
            "evaluator_kind": "self_review",
            "observed_behavior": scenario.get(
                "observed_behavior",
                "自动化联调记录，答案使用了 AI 辅助，不据此确认真实学习者掌握。",
            ),
            "rubric_results": [
                {
                    "rubric_item_id": row["id"],
                    "outcome": "unknown",
                    "evidence_excerpt": "",
                    "error_type": "not_observed",
                }
                for row in practice["activity"]["rubric"]
            ],
        }
        save(
            "learner_observation",
            await practice_command(
                {
                    "type": "record_practice_observation",
                    **target(practice),
                    "observation": observation,
                }
            ),
        )
    if "learner_feedback" not in report["stages"]:
        result = await practice_command(
            {
                "type": "record_tutor_feedback",
                **target(practice),
                "feedback": {
                    "turn_id": turn_id,
                    "signal": "explicit_feedback",
                    "content": "自动联调反馈：请先看代码，再分步解释条件；保留证据缺口并给出理解问题。",
                    "consent": True,
                },
            }
        )
        save("learner_feedback", result)
    if "practice_closed" not in report["stages"]:
        save(
            "practice_closed", await practice_command({"type": "end_practice", **target(practice)})
        )
    await app.drain(limit=30)
    if (
        "evolution_candidate_admitted" in report["stages"]
        and report.get("evolution_attempt", 1) == 1
    ):
        prior_id = report["stages"]["evolution_candidate_admitted"]["workflow_id"]
        prior = await call("GET", path + "/evolution/" + prior_id)
        if prior.get("request_status") == "failed" and prior.get("status") == "completed":
            keys = [
                key
                for key in report["stages"]
                if key.startswith("evolution_")
                or (key.startswith("development_replay_") and key != "development_replay_dataset")
            ]
            report["evolution_attempt_history"] = [{key: report["stages"].pop(key) for key in keys}]
            report["evolution_attempt"] = 2
    attempt = str(report.get("evolution_attempt", 1))
    if "evolution_candidate_admitted" not in report["stages"]:
        experience = report["stages"]["learner_feedback"]["action_receipts"][0]["experience_id"]
        result = await call(
            "POST",
            path + "/evolution",
            {
                "contract": "evolution-dialogue-v1",
                "idempotency_key": "full-loop-candidate"
                + ("-" + attempt if attempt != "1" else ""),
                "parent_workflow_id": wid,
                "intent": {
                    "type": "candidate",
                    "target": scenario.get("evolution_target", "teaching_skill"),
                    "experience_ids": [experience],
                    "funding_id": funding,
                },
            },
        )
        save("evolution_candidate_admitted", result)
    await app.drain(limit=60)
    evolution_id = report["stages"]["evolution_candidate_admitted"]["workflow_id"]
    if "evolution_candidate" not in report["stages"]:
        evolution = await call("GET", path + "/evolution/" + evolution_id)
        save("evolution_candidate", evolution)
    evolution = report["stages"]["evolution_candidate"]
    candidate_id = (evolution.get("result") or {}).get("candidate_id")
    if not candidate_id:
        raise RuntimeError("Candidate generation did not settle: " + str(evolution))
    if "development_replay_dataset" not in report["stages"]:
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, wid)
            publications = workflow.target["publication_ids"]
            replay_packs = [app.knowledge.publication_pack(db, actor, p) for p in publications]
            pack = replay_packs[0]
            replay_facts = [fact for item in replay_packs for fact in item["facts"]]
        cases = [
            {
                "case_id": "integration-" + str(index),
                "group_id": "integration:" + str(index),
                "repository_family": pack["repository"],
                "user_question_group": "conversation:" + report["conversation_id"],
                "time_partition": "2026-11",
                "question": question,
                "publication_ids": publications,
                "expected": {
                    "required_fact_ids": [replay_facts[index % len(replay_facts)]["fact_atom_id"]]
                },
            }
            for index, question in enumerate(
                [
                    "请引用当前已审核材料，说明工具调用的一个实际执行步骤与条件。",
                    "请引用当前已审核材料，说明工具结果处理的实际步骤与尚未核验的边界。",
                ]
            )
        ]
        dataset = await call(
            "POST",
            "/evolution/replay-datasets",
            {
                "namespace_id": owner["namespace_id"],
                "name": "full-loop-native-development",
                "version": "1",
                "split": "development",
                "provenance": "development_probe",
                "cases": cases,
                "thresholds": {"min_cases": 2, "min_groups": 2},
            },
        )
        save("development_replay_dataset", dataset)
    evolution = await call("GET", path + "/evolution/" + evolution_id)
    if evolution.get("request_status") == "running" and evolution.get("run_id"):
        from careerforge.models import AgentRun
        from careerforge.services.evolution_workflow import validate_run_binding_in
        from careerforge.services.runtime import TERMINAL

        with app.db.transaction() as db:
            run = db.get(AgentRun, evolution["run_id"])
            if run.status in TERMINAL:
                workflow = db.get(WorkflowSession, evolution_id)
                execution = app.evolution_workflows.current(db, workflow)
                validate_run_binding_in(
                    app, db, actor, workflow.namespace_id, run.input_manifest, run=run
                )
                app.evolution_workflows.wake_in(
                    db, workflow, execution, "explicit_live_terminal_reconciliation"
                )
        await app.drain(limit=20)
        evolution = await call("GET", path + "/evolution/" + evolution_id)
    retry_binding = evolution.get("request_status") == "failed"
    replay_stage = (
        "development_replay_after_code_freeze" if retry_binding else "development_replay_admitted"
    )
    replay_key = (
        "full-loop-replay-code-frozen"
        if retry_binding
        else "full-loop-development-replay-" + attempt
    )
    if replay_stage not in report["stages"]:
        evolution = await call("GET", path + "/evolution/" + evolution_id)
        result = await call(
            "POST",
            path + "/evolution",
            {
                "contract": "evolution-dialogue-v1",
                "idempotency_key": replay_key,
                "workflow_id": evolution_id,
                "expected_workflow_revision": evolution["workflow_revision"],
                "intent": {
                    "type": "replay",
                    "candidate_id": candidate_id,
                    "replay": {
                        "dataset_id": report["stages"]["development_replay_dataset"]["dataset_id"],
                        "funding_id": funding,
                        "per_arm_call_cap": scenario.get("per_arm_call_cap", 2),
                        "idempotency_key": replay_key + "-paired",
                    },
                },
            },
        )
        save(replay_stage, result)
    await app.drain(limit=100)
    evolution = await call("GET", path + "/evolution/" + evolution_id)
    save("development_replay_result", evolution)
    if not (evolution.get("result") or {}).get("evaluation_id"):
        raise RuntimeError("Development replay did not produce an evaluation: " + str(evolution))
    if evolution["status"] == "completed":
        save(
            "candidate_not_promoted",
            {"reason": "development_probe_not_independent_gold", "evolution": evolution},
        )
    elif "candidate_rejected" not in report["stages"]:
        rejected = await call(
            "POST",
            path + "/evolution",
            {
                "contract": "evolution-dialogue-v1",
                "idempotency_key": "full-loop-reject-candidate",
                "workflow_id": evolution_id,
                "expected_workflow_revision": evolution["workflow_revision"],
                "intent": {
                    "type": "reject",
                    "reason": "本轮为自动化联调与开发回放，缺少独立人工留出验收，明确拒绝晋级。",
                },
            },
        )
        save("candidate_rejected", rejected)
    await app.drain(limit=30)
    return {
        "workflow_id": wid,
        "evolution_id": evolution_id,
        "evolution": await call("GET", path + "/evolution/" + evolution_id),
    }
