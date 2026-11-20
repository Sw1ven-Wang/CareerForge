import pytest
from test_desk_learning_flow import configured, send
from test_learning_intent import classifier
from test_local_web import desk

from careerforge.desk_projects import repository_question

URL = "https://github.com/earendil-works/pi"


@pytest.mark.parametrize(
    "question",
    [
        URL + "  学这个的架构",
        "请讲解 " + URL + " 的 agent 架构",
        URL + " 学这个项目的 agent 架构：请解释模块职责和核心调用流程，并给出源码引用。",
    ],
)
def test_explicit_project_and_question(question):
    assert repository_question(question, {}) == URL


@pytest.mark.parametrize("question", ["agent架构能力", "帮我讲解 Pi 这个项目的agent架构"])
def test_followup_recovers_caller_repository(question):
    context = {"history": [{"question": URL + " 学这个的架构"}]}
    assert repository_question(question, context) == URL
    assert repository_question(question, {"history": [{"question": "你好", "answer": URL}]}) is None


@pytest.mark.parametrize(
    "question",
    ["不要学习 " + URL, "请解释字符串 " + URL, URL, "讲解 https://github.com/a/b 和 " + URL],
)
def test_ambiguous_or_nonlearning_request_does_not_admit_build(question):
    assert repository_question(question, {}) is None


def test_changed_focus_cannot_start_old_project_build(env):
    from careerforge.desk_projects import prepare_project_question
    from careerforge.errors import DomainError

    cid = env["app"].conversations.create(env["actor"])
    with pytest.raises(DomainError) as error:
        prepare_project_question(
            env["app"],
            {
                "actor": env["actor"],
                "conversation_id": cid,
                "question": URL + " 学架构",
                "funding_id": env["funding"],
                "run_id": "test",
            },
            {"status_snapshot": {"focus_revision": 999}},
            URL,
        )
    assert error.value.code == "REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_failed_build_does_not_claim_question_answered(env, monkeypatch):
    import careerforge.desk_workflow as worker
    from careerforge.desk_projects import complete_project_question, prepare_project_question

    app = env["app"]
    cid = app.conversations.create(env["actor"])
    job = {
        "actor": env["actor"],
        "conversation_id": cid,
        "question": URL + " 学架构",
        "funding_id": env["funding"],
        "run_id": "test",
    }
    prepare_project_question(app, job, {"status_snapshot": {"focus_revision": 1}}, URL)

    async def failed(service, job, *, status_key="status"):
        job[status_key] = "failed"

    monkeypatch.setattr(worker, "execute_desk_job", failed)
    await complete_project_question(app, job, lambda: None)
    assert job["project_question"]["status"] == "needs_evidence"
    assert "未完成架构讲解" in job["reply"]
    assert not job["project_question"].get("answer_turn_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("recognized", ["explain", "start", "discover"])
async def test_project_question_builds_reviews_and_answers_in_same_conversation(env, recognized):
    from careerforge.contracts import ModelOutput

    env["model"].overrides["query_rewriter"] = lambda request: ModelOutput(
        structured={
            "variants": ["tool dispatch"],
            "selected_topic_keys": [],
            "reason": "symbol_hint",
        },
        synthetic=True,
    )
    configured(env)
    classifier(env, recognized)
    original_complete = env["model"].complete

    async def complete(request):
        result = await original_complete(request)
        fields = (request.get("schema") or {}).get("properties", {})
        structured = dict(result.structured or {})
        for field, value in {
            "mechanism_edges": [],
            "evidence_read_requests": [],
            "relation_audit": {
                "confirmed_local_issue_indices": [],
                "topic_links": [],
                "facets": [],
            },
        }.items():
            if field in fields:
                structured[field] = value
        return result.model_copy(update={"structured": structured})

    env["model"].complete = complete
    seen = []
    snapshot = env["repos"].snapshot

    async def record(url, run_id, request_cap):
        seen.append(url)
        return await snapshot(url, run_id, request_cap)

    env["repos"].snapshot = record
    async with desk(env) as client:
        job, detail = await send(client, URL + " 学这个的架构")
        assert seen == [URL]
        from careerforge.models import AgentRun

        with env["app"].db.read() as db:
            root = db.get(AgentRun, detail["actions"][-1]["project_question"]["build_run_id"])
            assert root.input_manifest["auto_evidence_retry"] is True
            assert (
                root.input_manifest["repository_retry_contract"]
                == "single-repository-fixed-snapshot-retry-v1"
            )
        assert detail["flow"]["status"]["target"] == URL
        assert detail["flow"]["status"]["publication_count"] > 0
        action = detail["actions"][-1]
        assert action["project_question"]["status"] == "answered", action
        assert detail["turns"][-1]["question"] == URL + " 学这个的架构"
        assert not detail["flow"].get("plan"), "A project question must not silently create a plan"
        classifier(env, "explain")
        await send(client, "agent架构能力", job["conversation_id"])
        assert seen == [URL], "Bound project follow-up must not rebuild the same repository"
