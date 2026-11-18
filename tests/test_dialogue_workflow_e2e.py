import pytest
from sqlalchemy import func, select
from support import FILES
from test_dialogue_actions import CASES, api_client, open_dialogue, send
from test_learning_loop_v16 import configure_planner, observation_for

from careerforge.contracts import ImportSourceInput, ModelOutput
from careerforge.models import AgentRun, LearningPlan, ModelReceipt, Outbox
from careerforge.models_dialogue import WorkflowRunLink


def configure_grouped_extraction(env):
    def extract(request):
        doc = request["context"]["jd_document"]
        text = doc["raw_text"]
        paragraph = doc["paragraphs"][0]

        def span(quote):
            index = text.index(quote)
            return {
                "quote": quote,
                "paragraph_id": paragraph["paragraph_id"],
                "span_start": index,
                "span_end": index + len(quote),
            }

        def requirement(key, name, quote, depth, modality, operator, group):
            return {
                "key": key,
                "technology": name,
                "keywords": [name],
                **span(quote),
                "depth": depth,
                "depth_basis": "explicit" if depth else "unspecified",
                "modality": modality,
                "group_operator": operator,
                "group_key": group,
            }

        alternatives = "熟悉 Python 或 Java"
        negative = "不要求模型微调"
        return ModelOutput(
            synthetic=True,
            structured={
                "requirements": [
                    requirement(
                        "python", "Python", alternatives, "熟悉", "required", "OR", "language"
                    ),
                    requirement("java", "Java", alternatives, "熟悉", "required", "OR", "language"),
                    requirement("redis", "Redis", "Redis 优先", None, "preferred", "SINGLE", None),
                    requirement(
                        "tuning", "模型微调", negative, None, "not_required", "NOT", "exclude"
                    ),
                ],
                "groups": [
                    {
                        "key": "language",
                        "operator": "OR",
                        "member_keys": ["python", "java"],
                        **span(alternatives),
                    },
                    {
                        "key": "exclude",
                        "operator": "NOT",
                        "member_keys": ["tuning"],
                        **span(negative),
                    },
                ],
                "limitations": [],
            },
        )

    env["model"].overrides["extract"] = extract


def fixed_source(env, name="dialogue-demo", commit="a" * 40):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixture/" + name,
            commit_sha=commit,
            license_id="MIT",
            rights_confirmed=True,
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cold_attempt", [1, 2, 3])
async def test_T02_T19_T20_T21_T22_T24_three_cold_sqlite_dialogue_runs(env, cold_attempt):
    app = env["app"]
    configure_grouped_extraction(env)
    configure_planner(env)
    source = fixed_source(env)
    other = fixed_source(env, "dialogue-alternate", "b" * 40)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        imported = (
            await send(api, path, CASES["T02"]["user_message"], authorization_ref=env["funding"])
        ).json()
        extract = imported["run_refs"][0]
        before = len(env["model"].calls)
        for _ in range(3):
            live = (await send(api, path, CASES["T03"]["user_message"])).json()
            assert live["action_receipts"][0]["view"]["run_refs"] == [extract]
        paused = (await send(api, path, CASES["T10"]["user_message"])).json()
        assert paused["action_receipts"][0]["control_state"] == "pause_requested"
        assert paused["workflow_id"] != imported["workflow_id"]
        await send(api, path, "我想学习 Redis")
        resume = (await send(api, path, CASES["T14"]["user_message"])).json()
        assert resume["workflow_id"] == imported["workflow_id"], resume
        assert len(env["model"].calls) == before
        await app.drain()
        status = (await api.get(path + "/workflow-status")).json()
        assert status["milestone"] == "awaiting_goal_confirmation", status
        raw = app.jd.read(env["actor"], status["source_jd_revision_id"])
        assert raw["raw_text"] == CASES["T02"]["user_message"]
        result = app.runtime.stage(extract, "result")
        assert result["goal_version_id"] == status["goal_version_id"]
        assert {x["operator"] for x in result["groups"]} == {"OR", "NOT"}
        assert {r["modality"] for r in result["requirements"]} == {
            "required",
            "preferred",
            "not_required",
        }
        updated = (await send(api, path, CASES["T05"]["user_message"])).json()
        assert updated["action_receipts"][0]["goal_addendum_id"]
        current = (await api.get(path)).json()
        switched = await api.post(
            path + "/focus",
            json={
                "workflow_id": imported["workflow_id"],
                "expected_focus_revision": current["revision"],
                "idempotency_key": "return-to-original-jd",
            },
        )
        assert switched.json()["workflow_id"] == imported["workflow_id"], switched.text
        assert (await send(api, path, CASES["T19"]["user_message"])).json()["action_receipts"][0][
            "status"
        ] == "waiting_confirmation"
        confirmed = (
            await send(
                api,
                path,
                CASES["T19"]["user_message"],
                authorization_ref=env["funding"],
                source_ids=[source, other],
            )
        ).json()
        assert len(confirmed["run_refs"]) == 2, confirmed
        build_run, alternate_run = confirmed["run_refs"]
        await app.drain()
        view = (await api.get(path + "/workflow-status")).json()
        assert view["publication_count"] > 0 and view["coverage"] == "partial", view
        assert view["milestone"] == "awaiting_plan_acceptance", view
        reply = (await send(api, path, CASES["T21"]["user_message"])).json()
        assert reply["action_receipts"][0]["status"] == "waiting_confirmation"
        with app.db.read() as db:
            links = list(
                db.scalars(
                    select(WorkflowRunLink).where(
                        WorkflowRunLink.workflow_id == view["workflow_id"]
                    )
                )
            )
            plan_run = next(link.run_id for link in links if link.milestone == "plan")
            plan = app.runtime.stage(plan_run, "result")
            assert db.get(LearningPlan, plan["plan_id"]).active_version_id is None
            assert (
                db.get(AgentRun, plan_run).input_manifest["goal"]["goal_version_id"]
                == result["goal_version_id"]
            )
            assert db.get(AgentRun, build_run).input_manifest["source_id"] == source
            assert db.get(AgentRun, alternate_run).input_manifest["source_id"] == other
            assert sum(link.milestone == "plan" for link in links) == 1
        # Replay the same terminal milestone through the existing dispatcher.
        with app.db.transaction() as db:
            db.add(Outbox(kind="workflow.milestone", aggregate_id=build_run, payload={}))
        before = len(env["model"].calls)
        await app.drain()
        assert len(env["model"].calls) == before
        approved = (await send(api, path, CASES["T22"]["user_message"])).json()
        assert [r["status"] for r in approved["action_receipts"]] == ["applied", "applied"], (
            approved
        )
        assert approved["turn_id"]
        await app.drain()
        turn = (await api.get(f"/turns/{approved['turn_id']}")).json()
        assert turn["status"] == "completed", turn
        blocks = [
            e["payload"]["content"] for e in turn["events"] if e["type"] == "answer.block.validated"
        ]
        assert any(cite["commit"] == "a" * 40 for b in blocks for cite in b.get("citations", [])), (
            blocks
        )
        assert any(call["role"] == "answer_checker" for call in env["model"].calls)
        feedback = (await send(api, path, "我没理解")).json()["action_receipts"][0]
        assert feedback["feedback"]["experience_id"] is None
        stream = await api.get(
            path + "/activity-events", params={"once": True, "after": approved["cursor"]}
        )
        assert '"answer_event"' in stream.text
        practice = (await send(api, path, CASES["T24"]["user_message"])).json()
        step = practice["action_receipts"][0]
        assert step["status"] == "applied", step
        aid = step["attempt"]["attempt_id"]
        assert (
            await api.post(
                f"/learning/attempts/{aid}/submit",
                json={
                    "response": "该函数返回传入的 value。",
                    "external_help": "unknown",
                },
            )
        ).status_code == 200
        body = observation_for(step["activity"]).model_dump(mode="json")
        for rubric in body["rubric_results"]:
            rubric["evidence_excerpt"] = "返回传入的 value"
        observation = await send(api, path, "记录练习反馈", observation=body)
        step_receipt = observation.json()["action_receipts"][0]
        assert step_receipt["status"] == "applied" and step_receipt["observation"]["observation_id"]
        assert "执行代码需要另行批准" in step["reply"]
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(ModelReceipt)) == len(
                env["model"].calls
            )


@pytest.mark.asyncio
async def test_T23_unreviewed_source_does_not_advance_to_plan(env):
    source = fixed_source(env)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 Python")
        question = (await send(api, path, CASES["T23"]["user_message"], source_id=source)).json()
        assert question["lane"] == "answer"
        turn = (await api.get(f"/turns/{question['turn_id']}")).json()
        blocks = [
            e["payload"]["content"] for e in turn["events"] if e["type"] == "answer.block.validated"
        ]
        assert blocks and all(not block.get("citations") for block in blocks)
        assert not any(block.get("kind") in {"repository_fact", "concept_fact"} for block in blocks)
        status = (await api.get(path + "/workflow-status")).json()
        assert status["publication_count"] == 0 and status["coverage"] == "unknown"
        wait = (await send(api, path, CASES["T20"]["user_message"])).json()
        assert wait["action_receipts"][0]["status"] == "waiting_confirmation"
        assert not wait["run_refs"]


@pytest.mark.asyncio
async def test_T23_independent_reviewer_rejects_unit_without_publication(env, monkeypatch):
    app = env["app"]
    complete = env["model"].complete

    async def reject(request):
        output = await complete(request)
        if request["role"] == "reviewer" and output.structured:
            structured = {
                **output.structured,
                "decision": "reject",
                "accepted_topic_link_ids": [],
                "accepted_facet_ids": [],
                "claim_decisions": [
                    {
                        **claim,
                        "outcome": "insufficient",
                        "independent_reads": [],
                        "boundary": "Independent reading did not establish this claim",
                    }
                    for claim in output.structured["claim_decisions"]
                ],
            }
            if structured.get("scope_review"):
                structured["scope_review"] = {
                    **structured["scope_review"],
                    "standalone_claim_ids": [],
                }
            return output.model_copy(update={"structured": structured})
        return output

    monkeypatch.setattr(env["model"], "complete", reject)
    source = fixed_source(env)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 Python")
        admitted = (
            await send(api, path, "确认目标", source_id=source, authorization_ref=env["funding"])
        ).json()
        assert admitted["run_refs"], admitted
        await app.drain()
        result = app.runtime.stage(admitted["run_refs"][0], "result")
        assert not result["publication_ids"] and any(
            gap.startswith("review_not_accepted:") for gap in result["gaps"]
        )
        status = (await api.get(path + "/workflow-status")).json()
        assert status["publication_count"] == 0 and status["coverage"] == "unknown"
        assert status["waiting_user_action"] == "reviewed_evidence_required"
