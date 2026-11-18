"""Caller-only M2 admission choices, historical hashes and API/CLI forwarding."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_dialogue_actions import api_client, open_dialogue, send
from test_dialogue_workflow_e2e import fixed_source

from careerforge.contracts_conversation_actions_v1 import ConversationActionPlan, DialogueMessage
from careerforge.models import AgentRun
from careerforge.models_dialogue import ConversationActionReceipt
from careerforge.util import fingerprint


@pytest.mark.parametrize(
    "value",
    [
        {"research_question": ""},
        {"research_question": "x" * 6001},
        {"auto_evidence_retry": "true"},
        {"auto_evidence_retry": 1},
        {"research_question": "public research", "source_id": "fixed"},
        {"research_question": "public research", "source_ids": ["fixed"]},
        {"research_question": "public research", "kind": "repository"},
        {"research_question": "public research", "auto_evidence_retry": True},
        {"auto_evidence_retry": True, "source_ids": ["one", "two"]},
    ],
)
def test_admission_options_are_strict_and_unambiguous(value):
    with pytest.raises(ValidationError):
        DialogueMessage(text="/build", expected_focus_revision=1, idempotency_key="key", **value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("research_question", "Inspect public fixtures/agent"),
        ("auto_evidence_retry", True),
    ],
)
def test_coordinator_cannot_supply_caller_admission_options(field, value):
    with pytest.raises(ValidationError):
        ConversationActionPlan.model_validate(
            {
                "conversation_id": "c",
                "expected_focus_revision": 1,
                "idempotency_key": "k",
                "actions": [
                    {
                        "step_id": "1",
                        "action": {"type": "start_build", field: value},
                    }
                ],
            }
        )


async def test_default_fields_preserve_historical_hash_and_receipt_replay(env):
    async with api_client(env) as api:
        path = await open_dialogue(api)
        revision = (await api.get(path)).json()["revision"]
        key = uuid4().hex
        body = DialogueMessage(
            text="当前进度", expected_focus_revision=revision, idempotency_key=key
        )
        # The historical wire predates both M2 and the later desk learning inputs.
        legacy = body.model_dump(mode="json", exclude={
            "research_question", "auto_evidence_retry", "submission", "module_ack", "publication_ids"
        })
        expected = fingerprint(
            {
                "message": legacy,
                "actions": None,
                "workflow_id": None,
                "workflow_revision": None,
                "expected_goal": None,
            }
        )
        first = await api.post(path + "/messages", json=legacy)
        assert first.status_code == 202, first.text
        with env["app"].db.read() as db:
            receipt = db.scalar(
                select(ConversationActionReceipt).where(
                    ConversationActionReceipt.idempotency_key == key
                )
            )
            assert receipt.request_hash == expected
        replay = await api.post(
            path + "/messages",
            json={
                **legacy,
                "research_question": None,
                "auto_evidence_retry": False,
            },
        )
        assert replay.json() == first.json()
        conflicting = await api.post(
            path + "/messages",
            json={
                **legacy,
                "research_question": "Inspect public fixtures/agent",
            },
        )
        assert conflicting.status_code == 409 and "IDEMPOTENCY_CONFLICT" in conflicting.text


async def test_explicit_search_question_reaches_frozen_research_input_without_shortcut(env):
    app = env["app"]
    question = "Inspect public fixtures/agent and explain its tool-return implementation."
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 agent.tool_calling")
        revision = (await api.get(path)).json()["revision"]
        admitted = await send(
            api,
            path,
            "/build",
            research_question=question,
            authorization_ref=env["funding"],
            key="targeted",
            revision=revision,
        )
        assert admitted.status_code == 202, admitted.text
        run_id = admitted.json()["run_refs"][0]
        with app.db.read() as db:
            run = db.get(AgentRun, run_id)
            saved = deepcopy(run.input_manifest)
            assert saved["question"] == question
            assert not saved.get("source_id") and not saved.get("repository_url")
            assert saved["research_scope"] == "focused"
        replay = await send(
            api,
            path,
            "/build",
            research_question=question,
            authorization_ref=env["funding"],
            key="targeted",
            revision=revision,
        )
        assert replay.json() == admitted.json()
        await app.execute(run_id)
        research = [call for call in env["model"].calls if call["role"] == "planner"]
        assert research, [call["role"] for call in env["model"].calls]
        assert any(call["context"].get("question") == question for call in research)
        assert (
            app.runtime.stage(run_id, "research_context")["research_task"]["original_question"]
            == question
        )
        with app.db.read() as db:
            assert db.get(AgentRun, run_id).input_manifest == saved


async def test_search_question_rejects_inherited_direct_repository(env):
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "https://github.com/fixtures/agent", kind="repository")
        response = await send(
            api,
            path,
            "/build",
            research_question="Search public fixtures/agent",
            authorization_ref=env["funding"],
        )
        assert response.status_code == 202, response.text
        assert not response.json()["run_refs"]
        assert response.json()["action_receipts"][0]["status"] == "failed"
    with env["app"].db.read() as db:
        assert not list(db.scalars(select(AgentRun)))


@pytest.mark.parametrize("enabled", [False, True])
async def test_fixed_source_retry_optin_frozen_before_execution(env, enabled):
    source = fixed_source(env)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 agent.tool_calling")
        response = await send(
            api,
            path,
            "/build",
            source_id=source,
            auto_evidence_retry=enabled,
            build_call_cap=24,
            authorization_ref=env["funding"],
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_refs"][0]
    with env["app"].db.read() as db:
        run = db.get(AgentRun, run_id)
        assert bool(run.input_manifest.get("auto_evidence_retry")) is enabled
        assert bool(run.input_manifest.get("evidence_retry_contract")) is enabled
        assert run.input_manifest["source_id"] == source
        assert run.input_manifest["max_units_per_project"] == 1
        assert run.call_cap == 24
    assert not env["model"].calls


@pytest.mark.parametrize("source_kind", ["search", "direct_repository"])
async def test_retry_does_not_expand_to_unpinned_research(env, source_kind):
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(
            api,
            path,
            "我想学习 agent.tool_calling"
            if source_kind == "search"
            else "https://github.com/fixtures/agent",
        )
        response = await send(
            api, path, "/build", auto_evidence_retry=True, authorization_ref=env["funding"]
        )
        assert response.status_code == 202, response.text
        assert not response.json()["run_refs"]
        assert response.json()["action_receipts"][0]["status"] == "failed"
    with env["app"].db.read() as db:
        assert not list(db.scalars(select(AgentRun)))


async def test_retry_does_not_silently_increase_insufficient_budget(env):
    source = fixed_source(env)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 agent.tool_calling")
        response = await send(
            api,
            path,
            "/build",
            source_id=source,
            auto_evidence_retry=True,
            build_call_cap=8,
            authorization_ref=env["funding"],
        )
        assert response.status_code == 202, response.text
        assert not response.json()["run_refs"]
        assert response.json()["action_receipts"][0]["status"] == "failed"
    with env["app"].db.read() as db:
        assert not list(db.scalars(select(AgentRun)))


@pytest.mark.parametrize(
    "options,sources",
    [
        ({"research_question": "Inspect public fixtures/agent"}, []),
        ({"auto_evidence_retry": True}, ["fixed-source"]),
    ],
)
def test_cli_forwards_admission_choices_only_for_build(monkeypatch, options, sources):
    from careerforge.cli import main

    class NoThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    posted = []
    entries = iter(["当前进度", "/build", "/exit"])
    monkeypatch.setattr(main, "Thread", NoThread)
    monkeypatch.setattr(main.typer, "prompt", lambda _: next(entries))

    def request(method, path, body=None):
        if method == "GET":
            return {"focus_revision": 1}
        posted.append(body)
        return {"action_receipts": []}

    monkeypatch.setattr(main, "request", request)
    main.dialogue_chat("c", {"funding_id": "funding"}, sources, 24, **options)
    assert len(posted) == 2
    assert posted[0]["research_question"] is None
    assert posted[0]["auto_evidence_retry"] is False
    assert posted[1]["source_ids"] == sources
    for key, value in options.items():
        assert posted[1][key] == value
