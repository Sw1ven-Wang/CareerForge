"""Offline behavior tests, not claims of model or research quality."""

import httpx
import pytest
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, ModelOutput
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.contracts_learning_language import KnowledgeImplementationLinkInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ExternalRequest, ObjectRow
from careerforge.models_dialogue import WorkflowSession
from careerforge.services.knowledge_implementations import (
    create_link_in,
    linked_materials_in,
    material_matches,
)


def extraction(request):
    document = request["context"]["jd_document"]
    labels = ["工具调用"] if "工具调用" in document["raw_text"] else ["后端开发"]
    for label in ["Java", "Python", "Go"]:
        if label in document["raw_text"]:
            labels.append(label)
    rows = []
    for i, label in enumerate(labels):
        paragraph = next(p for p in document["paragraphs"] if label in p["text"])
        start = document["raw_text"].index(label)
        rows.append(
            dict(
                key=str(i),
                technology=label,
                keywords=[label],
                quote=label,
                paragraph_id=paragraph["paragraph_id"],
                span_start=start,
                span_end=start + len(label),
                depth=None,
                depth_basis="unspecified",
                modality="required",
                group_operator="SINGLE",
                group_key=None,
                topic_key=None,
            )
        )
    return ModelOutput(
        structured={"requirements": rows, "groups": [], "limitations": []}, synthetic=True
    )


async def imported(env, text="AI Agent 开发工程师，岗位职责：工具调用。"):
    env["model"].overrides["extract"] = extraction
    app, actor = env["app"], env["actor"]
    cid = app.conversations.create(actor)
    conv = app.conversations.detail(actor, cid)
    result = app.dialogue.dispatch(
        actor,
        cid,
        DialogueMessage(
            text=text,
            kind="jd",
            expected_focus_revision=conv["revision"],
            idempotency_key="import",
            authorization_ref=env["funding"],
        ),
    )
    await app.drain()
    return cid, result["workflow_id"]


def choose(env, cid, text="/language Java,Go", key="choose", revision=None):
    app, actor = env["app"], env["actor"]
    return app.dialogue.dispatch(
        actor,
        cid,
        DialogueMessage(
            text=text,
            expected_focus_revision=revision or app.conversations.detail(actor, cid)["revision"],
            idempotency_key=key,
        ),
    )


@pytest.mark.asyncio
async def test_jd_without_language_asks_before_build_and_does_not_research(env):
    cid, wid = await imported(env)
    app, actor = env["app"], env["actor"]
    view = app.workflows.status(actor, cid)
    assert view["milestone"] == "awaiting_language_choice"
    assert "Python" in view["language_question"]
    assert view["language_profile"]["required_core_languages"] == ["python"]
    before = len(env["model"].calls)
    blocked = choose(env, cid, "确认目标", "blocked-build")
    assert blocked["action_receipts"][0]["failure_code"] == "LANGUAGE_CHOICE_REQUIRED"
    assert len(env["model"].calls) == before
    original_revision = app.conversations.detail(actor, cid)["revision"]
    result = choose(env, cid)
    assert result["action_receipts"][0]["status"] == "applied"
    view = app.workflows.status(actor, cid)
    assert view["language_profile"]["backend_languages"] == ["java", "go"]
    assert view["language_profile"]["required_core_languages"] == ["python"]
    assert view["milestone"] == "awaiting_goal_confirmation"
    entry = view["learning_knowledge_outline"]["entries"][0]
    assert entry["architecture"]["language"] is None
    assert entry["implementation"]["languages"] == ["python"]
    assert entry["architecture"]["knowledge_status"] == "awaiting_reviewed_material"
    assert choose(env, cid, revision=original_revision) == result
    with app.db.read() as db:
        assert all(r.workflow == "jd_extract" for r in db.scalars(select(AgentRun)))
        assert not list(db.scalars(select(ExternalRequest)))


@pytest.mark.asyncio
async def test_non_ai_backend_has_no_forced_python_and_jd_language_is_preserved(env):
    cid, wid = await imported(env, "岗位职责：后端开发，要求 Java。")
    view = env["app"].workflows.status(env["actor"], cid)
    assert view["language_profile"]["status"] == "configured_from_jd"
    assert view["language_profile"]["backend_languages"] == ["java"]
    assert view["language_profile"]["required_core_languages"] == []
    assert view["language_question"] is None


@pytest.mark.asyncio
async def test_ai_jd_java_does_not_remove_python_or_rewrite_employer_requirement(env):
    cid, wid = await imported(env, "AI Agent 开发，岗位职责：工具调用，要求 Java。")
    app, actor = env["app"], env["actor"]
    view = app.workflows.status(actor, cid)
    assert view["language_profile"]["required_core_languages"] == ["python"]
    assert view["language_profile"]["jd_languages"] == ["java"]
    assert all("Python" != r["technology"] for r in view["learning_knowledge_outline"]["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice, expected",
    [
        ("/language Python", ["python"]),
        ("后端用 Rust", ["rust"]),
        ("/language golang,Java", ["go", "java"]),
    ],
)
async def test_language_choices_are_normalized_and_backend_python_is_allowed(env, choice, expected):
    cid, _ = await imported(env)
    result = choose(env, cid, choice)
    assert result["action_receipts"][0]["language_profile"]["backend_languages"] == expected


@pytest.mark.asyncio
async def test_invalid_language_is_rejected_without_starting_work(env):
    cid, _ = await imported(env)
    result = choose(env, cid, "/language ???")
    assert result["action_receipts"][0]["failure_code"] == "LANGUAGE_INPUT"
    assert env["app"].workflows.status(env["actor"], cid)["milestone"] == "awaiting_language_choice"


@pytest.mark.asyncio
async def test_language_route_checks_revision_and_preserves_idempotency(env):
    cid, wid = await imported(env)
    app, actor = env["app"], env["actor"]
    view = app.workflows.status(actor, cid)
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test", headers=headers
    ) as api:
        body = dict(
            expected_revision=view["workflow_revision"],
            expected_focus_revision=view["focus_revision"],
            idempotency_key="api-language",
            languages=["Java"],
        )
        response = await api.post(f"/api/v1/workflows/{wid}/implementation-language", json=body)
        assert response.status_code == 202, response.text
        again = await api.post(f"/api/v1/workflows/{wid}/implementation-language", json=body)
        assert again.json() == response.json()
        body.update(idempotency_key="stale", languages=["Go"])
        stale = await api.post(f"/api/v1/workflows/{wid}/implementation-language", json=body)
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_language_configuration_cannot_target_another_users_workflow(env):
    cid, wid = await imported(env)
    app = env["app"]
    from careerforge.services.security import create_principal

    with app.db.transaction() as db:
        _, _, token = create_principal(db, "other-language-user")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as api:
        response = await api.post(
            f"/api/v1/workflows/{wid}/implementation-language",
            json={
                "expected_revision": 1,
                "expected_focus_revision": 1,
                "idempotency_key": "not-my-workflow",
                "languages": ["Java"],
            },
        )
        assert response.status_code == 404
        materials = await api.get(f"/api/v1/workflows/{wid}/knowledge-materials")
        assert materials.status_code == 404


def test_architecture_is_language_neutral_but_implementation_is_not():
    profile = {"ai_agent_track": True, "backend_languages": ["java"]}
    architecture = {"unit_type": "architecture", "citations": [{"path": "design.go"}]}
    java = {"unit_type": "implementation", "citations": [{"path": "src/Task.java"}]}
    python = {"unit_type": "function", "citations": [{"path": "src/agent.py"}]}
    assert material_matches(architecture, profile, "ai_agent")
    assert not material_matches(java, profile, "ai_agent")
    assert material_matches(java, profile, "backend")
    assert material_matches(python, profile, "ai_agent")
    assert not material_matches(python, profile, "backend")
    mixed = {
        "unit_type": "implementation",
        "citations": [{"path": "Task.java"}, {"path": "test.py"}],
    }
    assert not material_matches(mixed, profile, "ai_agent", language="java")


@pytest.mark.asyncio
async def test_tampered_language_profile_is_rejected(env):
    cid, wid = await imported(env)
    choose(env, cid)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        wf = db.get(WorkflowSession, wid)
        obj = db.get(ObjectRow, wf.target["learning_language_profile_id"])
        obj.payload = {**obj.payload, "required_core_languages": []}
    with pytest.raises(DomainError):
        app.workflows.status(actor, cid)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["Python", "Java,Go"])
async def test_plain_language_reply_is_a_control_not_a_paid_question(env, choice):
    cid, _ = await imported(env)
    before = len(env["model"].calls)
    result = choose(env, cid, choice)
    assert result["action_receipts"][0]["action_type"] == "select_implementation_language"
    assert result["action_receipts"][0]["status"] == "applied"
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_ai_application_without_agent_still_requires_python(env):
    cid, _ = await imported(env, "AI 应用开发，岗位职责：后端开发。")
    view = env["app"].workflows.status(env["actor"], cid)
    assert view["language_profile"]["required_core_languages"] == ["python"]
    assert view["milestone"] == "awaiting_language_choice"


@pytest.mark.asyncio
async def test_selected_backend_language_cannot_erase_jd_required_language(env):
    cid, _ = await imported(env, "岗位职责：后端开发，要求 Java。")
    rejected = choose(env, cid, "/language Go")
    assert rejected["action_receipts"][0]["failure_code"] == "JD_LANGUAGE_REQUIRED"
    accepted = choose(env, cid, "/language Java,Go", "include-jd")
    assert accepted["action_receipts"][0]["status"] == "applied"


@pytest.mark.asyncio
async def test_outline_links_stay_identical_when_backend_language_changes(env):
    cid, _ = await imported(env)
    choose(env, cid, "/language Python")
    first = env["app"].workflows.status(env["actor"], cid)["learning_knowledge_outline"]
    choose(env, cid, "/language Java", "change-language")
    second = env["app"].workflows.status(env["actor"], cid)["learning_knowledge_outline"]
    assert [e["architecture"]["entry_id"] for e in first["entries"]] == [
        e["architecture"]["entry_id"] for e in second["entries"]
    ]
    assert all(e["architecture"]["language"] is None for e in second["entries"])


@pytest.mark.asyncio
async def test_goal_addition_inherits_language_choice_after_its_receipt_is_committed(env):
    cid, wid = await imported(env)
    choose(env, cid, "/language Java")
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        wf = db.get(WorkflowSession, wid)
        wf.orchestration_engine = "langgraph"
        wf.session_graph_version = "knowledge-session-v2"
    result = choose(env, cid, "增加 LangGraph，要求掌握", "new-objective")
    assert result["action_receipts"][0]["status"] == "applied", result
    view = app.workflows.status(actor, cid)
    assert view["workflow_id"] != wid
    assert view["language_profile"]["backend_languages"] == ["java"]
    assert view["language_profile"]["required_core_languages"] == ["python"]
    assert view["language_profile"]["goal_version_id"] == view["goal_version_id"]


@pytest.mark.asyncio
async def test_jd_language_alternatives_remain_alternatives(env):
    from test_dialogue_workflow_e2e import configure_grouped_extraction

    app, actor = env["app"], env["actor"]
    configure_grouped_extraction(env)
    cid = app.conversations.create(actor)
    app.dialogue.dispatch(
        actor,
        cid,
        DialogueMessage(
            text="熟悉 Python 或 Java；Redis 优先；不要求模型微调。",
            kind="jd",
            expected_focus_revision=app.conversations.detail(actor, cid)["revision"],
            idempotency_key="or-jd",
            authorization_ref=env["funding"],
        ),
    )
    await app.drain()
    result = choose(env, cid, "/language Java")
    assert result["action_receipts"][0]["status"] == "applied", result


@pytest.mark.asyncio
async def test_unrecognized_choice_cannot_fall_through_into_paid_answer_or_auto_build(env):
    cid, _ = await imported(env)
    before = len(env["model"].calls)
    response = choose(env, cid, "还没想好", "not-a-language")
    assert response["lane"] == "control"
    assert "Python" in response["reply"]
    assert len(env["model"].calls) == before


async def publish(env, origin, key):
    from support import FILES

    app, actor = env["app"], env["actor"]
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            origin_kind=origin,
            repository_key="fixtures/" + key,
            commit_sha="a" * 40 if origin == "repository" else None,
            license_id="MIT",
            files=FILES,
        ),
    )
    run = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source,
            question="返回值",
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key=key,
        ),
    )
    await app.drain()
    return app.runtime.stage(run, "result")["publication_ids"][0]


@pytest.mark.asyncio
async def test_real_reviewed_publications_link_persist_and_keep_architecture_across_languages(env):
    architecture = await publish(env, "concept", "architecture")
    implementation = await publish(env, "repository", "implementation")
    app, actor = env["app"], env["actor"]
    body = KnowledgeImplementationLinkInput(
        namespace_id=env["ns"],
        architecture_publication_id=architecture,
        implementation_publication_id=implementation,
        language="Python",
        domain="backend",
    )
    with app.db.transaction() as db:
        result = create_link_in(app, db, actor, body)
        assert create_link_in(app, db, actor, body) == result
    with app.db.read() as db:
        profile = {"ai_agent_track": True, "backend_languages": ["java"]}
        materials = linked_materials_in(app, db, actor, profile)
        assert len(materials) == 1
        assert materials[0]["architecture"]["publication_id"] == architecture
        assert materials[0]["implementation"] is None
        assert materials[0]["semantic_quality_verified"] is False
        assert db.get(ObjectRow, result["link_id"]) is not None
    with app.db.transaction() as db:
        with pytest.raises(DomainError, match="源码路径"):
            create_link_in(app, db, actor, body.model_copy(update={"language": "java"}))
    with app.db.transaction() as db:
        db.get(ObjectRow, implementation).state = "suspended"
    with app.db.read() as db:
        assert not linked_materials_in(
            app, db, actor, {"ai_agent_track": True, "backend_languages": ["python"]}
        )
