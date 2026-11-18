"""Synthetic width/transport regressions, not live model or quality evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from conftest import publish_fixture
from sqlalchemy import func, select
from test_context_once_contract import attach, usage
from test_learning_loop_v16 import plan_input
from test_planner_context_v166 import dependencies, expand, request, snapshot

from careerforge.adapters.request_contract import (
    CONTEXT_ONCE_CONTRACT,
    MODEL_REQUEST_MAX_BYTES,
    pack_request,
    request_context,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt
from careerforge.services.learning_ext import RUBRIC_TEXT, STEP_TEXT
from careerforge.services.learning_projection import text_catalog
from careerforge.services.planner_context_v166 import (
    BOUNDED_PLANNER_CONTEXT_VERSION,
    CONTEXT_ONCE_PLANNER_CONTEXT_VERSION,
    PLANNER_CONTEXT_VERSION,
    PLANNER_CONTEXT_WIRE_BUDGET,
    _wire_size,
    planner_model_context,
    require_visible_choices,
)
from careerforge.util import canonical, fingerprint


def wide_snapshot():
    frozen = snapshot()
    # Synthetic width only: no public JD body or expected model response.
    frozen["goal"]["source_text"] += (
        '\nSynthetic preferred qualifier: use "A" OR "B"; no assumed depth.' * 350
    )
    frozen["units"][1]["source_id"] = "independent-second-source"
    frozen["units"][0]["facts"][0]["text"] = "oversized complete fact " * 15_000
    frozen["planner_model_context_version"] = CONTEXT_ONCE_PLANNER_CONTEXT_VERSION
    return frozen


@pytest.mark.parametrize(
    "version,expected",
    [
        (None, "093ddd4e9dfe44073da8e889825b2b72dc6acc90521b54958c3edae70018424f"),
        (
            PLANNER_CONTEXT_VERSION,
            "e76c2c92f149978e62158c3c44af75f9e37bd51d0fe4e4bffd8cb9344a3a9b4b",
        ),
        (
            BOUNDED_PLANNER_CONTEXT_VERSION,
            "7f5ca2958392c78b01e05e99b84d854c2717dd112970231007b759de3331e1db",
        ),
    ],
)
def test_historical_request_bytes_match_unmodified_candidate(version, expected):
    # Digests calculated with the unmodified 549-file source and this dependency set.
    frozen = snapshot()
    if version:
        frozen["planner_model_context_version"] = version
    assert fingerprint(request(frozen)) == expected


def test_real_width_goal_fits_once_with_whole_bounded_facts_and_full_authority():
    frozen = wide_snapshot()
    original = deepcopy(frozen)
    legacy = {**frozen, "planner_model_context_version": BOUNDED_PLANNER_CONTEXT_VERSION}
    with pytest.raises(DomainError, match="完整学习目标") as exc:
        planner_model_context(legacy, STEP_TEXT, RUBRIC_TEXT)
    assert exc.value.code == "PLAN_GOAL_CONTEXT_LIMIT"
    payload = pack_request(request(frozen), generated=True)
    context = request_context(payload)
    assert len(canonical(payload).encode()) < MODEL_REQUEST_MAX_BYTES
    assert _wire_size(context) == len(canonical(canonical(context)).encode())
    assert _wire_size(context) <= PLANNER_CONTEXT_WIRE_BUDGET
    assert frozen == original
    assert payload["access_dependencies"] == dependencies(original)
    assert expand(context["frozen_input"]["goal"], context["group_catalog"]) == original["goal"]
    assert {g["operator"] for g in context["group_catalog"].values()} >= {"AND", "OR"}
    assert any(e.get("modality") == "bonus" for e in context["text_catalog"])
    assert any(
        e.get("required_depth") is None
        for e in context["text_catalog"]
        if e["kind"] == "goal_requirement"
    )
    assert {u["publication_id"] for u in context["frozen_input"]["units"]} == set(
        frozen["publication_ids"]
    )
    assert len({u["source_id"] for u in context["frozen_input"]["units"] if u["facts"]}) == 2
    authoritative = text_catalog(frozen)
    visible = {e["text_id"]: e for e in context["text_catalog"]}
    for unit in context["frozen_input"]["units"]:
        for fact in unit["facts"]:
            entries = [
                e for e in authoritative.values() if e.get("fact_atom_id") == fact["fact_atom_id"]
            ]
            assert fact["text_refs"] == [e["text_id"] for e in entries]
            assert all(visible[e["text_id"]]["text"] == e["text"] for e in entries)
    assert any(u["omitted_standalone_facts"] > 0 for u in context["frozen_input"]["units"])
    assert planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT) == context

    hidden = frozen["units"][0]["facts"][0]["fact_atom_id"]
    publication = frozen["units"][0]["publication_id"]
    for facts, reference, code in [
        ([hidden], "goal.text", "PLAN_EVIDENCE"),
        ([], f"fact.{hidden}.text", "PLAN_TEXT_REFERENCE"),
    ]:
        draft = SimpleNamespace(
            modules=[SimpleNamespace(publication_id=publication, focus_fact_ids=facts)],
            model_dump=lambda reference=reference, **_: {"nested": [{"text_id": reference}]},
        )
        with pytest.raises(DomainError) as exc:
            require_visible_choices(draft, context)
        assert exc.value.code == code


@pytest.mark.parametrize("overflow", ["goal", "evidence"])
def test_projection_overflow_fails_closed_without_mutating_authority(overflow):
    frozen = wide_snapshot()
    if overflow == "goal":
        frozen["goal"]["source_text"] += "retained goal " * 30_000
    else:
        for unit in frozen["units"]:
            for fact in unit["facts"]:
                fact["text"] = "whole fact cannot fit " * 30_000
    before = fingerprint(frozen)
    with pytest.raises(DomainError) as exc:
        planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)
    assert exc.value.code == f"PLAN_{overflow.upper()}_CONTEXT_LIMIT"
    assert fingerprint(frozen) == before


@pytest.mark.parametrize(
    "padding,expected",
    [(80_000, PLANNER_CONTEXT_VERSION), (300_000, CONTEXT_ONCE_PLANNER_CONTEXT_VERSION)],
)
async def test_new_admission_selects_actual_transport_freezes_version_and_keeps_dependencies(
    env, monkeypatch, padding, expected
):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    gateway, provider = attach(env)
    app.gateway, app.provider = gateway, provider
    original = app.knowledge.publication_pack

    def wide_pack(*args, **kwargs):
        pack = original(*args, **kwargs)
        # Full immutable snapshot retains extra source metadata. Only the
        # explicitly bounded projection removes it from model-only unit shells.
        return {**pack, "synthetic_source_metadata": "x" * padding}

    monkeypatch.setattr(app.knowledge, "publication_pack", wide_pack)
    admitted = app.learning.start_personalized_plan(env["actor"], plan_input(env, publication))
    repeated = app.learning.start_personalized_plan(env["actor"], plan_input(env, publication))
    assert repeated["replayed"] and repeated["run_id"] == admitted["run_id"]
    with app.db.read() as db:
        run = db.get(AgentRun, admitted["run_id"])
        frozen = deepcopy(run.input_manifest)
        assert frozen["planner_model_context_version"] == expected
        assert run.input_hash == fingerprint(frozen)
        assert len(frozen["units"][0]["synthetic_source_metadata"]) == padding
    context = planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)
    dependency_ids = app.learning._planner_access_dependencies(frozen)
    epoch = app.runtime.claim(admitted["run_id"], "worker")
    # Synthetic gateway call proves dependency closure/authority guards still run;
    # this is not a planner quality or end-to-end native-model claim.
    await gateway.call(
        admitted["run_id"],
        epoch,
        "worker",
        "request",
        "learning_planner",
        "synthetic",
        context,
        data_classes=frozen["data_classes"],
        access_dependencies=dependency_ids,
    )
    wire = provider.calls[-1]
    assert wire["request_contract"] == CONTEXT_ONCE_CONTRACT
    assert wire["access_dependencies"] == dependency_ids
    assert wire["access_closure_hash"] and "privacy_generations" in wire
    assert len(canonical(wire).encode()) <= MODEL_REQUEST_MAX_BYTES
    assert usage(env, admitted["run_id"])[0] == 1


async def test_new_admission_goal_overflow_precedes_new_run_receipt_or_reservation(
    env, monkeypatch
):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    gateway, provider = attach(env)
    app.gateway, app.provider = gateway, provider
    goal = wide_snapshot()["goal"]
    goal["source_text"] = "untruncated goal " * 50_000
    monkeypatch.setattr(app.learning, "_goal", lambda *a, **k: goal)

    def state():
        with app.db.read() as db:
            funding = db.get(FundingAgreement, env["funding"])
            return (
                db.scalar(select(func.count()).select_from(AgentRun)),
                db.scalar(select(func.count()).select_from(ModelReceipt)),
                funding.calls_consumed,
                funding.calls_held,
            )

    before = state()
    with pytest.raises(DomainError) as exc:
        app.learning.start_personalized_plan(env["actor"], plan_input(env, publication))
    assert exc.value.code == "PLAN_GOAL_CONTEXT_LIMIT"
    assert state() == before and provider.calls == []


async def test_once_projection_cannot_execute_with_legacy_gateway(env):
    run = SimpleNamespace(
        input_manifest={"planner_model_context_version": CONTEXT_ONCE_PLANNER_CONTEXT_VERSION},
        id="not-admitted",
    )
    with pytest.raises(DomainError) as exc:
        await env["app"].learning.execute_plan(run, 0, "worker")
    assert exc.value.code == "PLAN_MODEL_CONTEXT_VERSION"
    assert env["model"].calls == []


@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_context_once_admission_uses_exact_actual_envelope_threshold(env, monkeypatch, delta):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    gateway, provider = attach(env)
    app.gateway, app.provider = gateway, provider
    original = gateway.request_payload
    payload = original("learning_planner", "", {"evidence": "x" * 60_000})
    payload["messages"][0]["content"] = "s" * (
        MODEL_REQUEST_MAX_BYTES + delta - len(canonical(payload).encode())
    )
    assert len(canonical(payload).encode()) == MODEL_REQUEST_MAX_BYTES + delta
    calls = 0

    def controlled_payload(*args, **kwargs):
        nonlocal calls
        calls += 1
        return payload if calls == 1 else original(*args, **kwargs)

    monkeypatch.setattr(gateway, "request_payload", controlled_payload)
    admitted = app.learning.start_personalized_plan(env["actor"], plan_input(env, publication))
    with app.db.read() as db:
        run = db.get(AgentRun, admitted["run_id"])
        expected = CONTEXT_ONCE_PLANNER_CONTEXT_VERSION if delta > 0 else PLANNER_CONTEXT_VERSION
        assert run.input_manifest["planner_model_context_version"] == expected
    assert calls == (2 if delta > 0 else 1)
    assert provider.calls == []


async def test_new_projection_does_not_bypass_gateway_full_envelope_limit(env):
    app = env["app"]
    gateway, provider = attach(env)
    frozen = wide_snapshot()
    frozen["model_profile"] = provider.profile_version
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "learning_plan", frozen, 1, "oversize-envelope"
    )
    epoch = app.runtime.claim(identifier, "worker")
    before = usage(env, identifier)
    context = planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)
    assert _wire_size(context) < PLANNER_CONTEXT_WIRE_BUDGET
    with pytest.raises(DomainError) as exc:
        await gateway.call(
            identifier,
            epoch,
            "worker",
            "request",
            "learning_planner",
            "s" * 250_000,
            context,
            data_classes=frozen["data_classes"],
        )
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"
    assert usage(env, identifier) == before and provider.calls == []
