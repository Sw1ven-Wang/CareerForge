"""Generic contract tests only; no real-model outputs or grading inputs are reused."""

import tomllib
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select
from test_learning_loop_v16 import plan_input
from test_learning_projection_repair import context, reference, structured_draft

from careerforge import __version__
from careerforge.adapters.model import strict_schema
from careerforge.api import create_app
from careerforge.contracts import ModelOutput
from careerforge.contracts_learning_ext import PersonalizedPlanDraft
from careerforge.contracts_learning_plan_v162 import (
    PLAN_CONTRACT_VERSION,
    PLAN_DRAFT_VERSION,
    PersonalizedPlanDraftV162,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, LearningPlan, ModelReceipt
from careerforge.models_learning_ext import PlanProposalManifest
from careerforge.services import learning_ext
from careerforge.services.learning_projection import OBSERVABLES, project_task
from careerforge.services.orchestration import _intent_model_schema
from careerforge.util import fingerprint

KINDS = ["reading", "diagnostic", "understanding_check", "exercise", "transfer"]


def draft_for(kind="understanding_check", frozen=None):
    value = structured_draft(frozen)
    task = value["modules"][0]["task"]
    task["kind"] = kind
    if kind == "reading":
        task["scenarios"] = []
        task["acceptance_checks"] = []
    return value


def test_legacy_wire_contract_fingerprints_are_unchanged():
    # Captured from the unchanged 1.6.1 code before this patch, not a model answer.
    assert fingerprint(learning_ext.PLAN_RULES) == (
        "5d671b609a9d1205e83df78db916fde5aeff3d98df53f505e9503dbd349c61a0"
    )
    assert fingerprint(strict_schema(PersonalizedPlanDraft.model_json_schema())) == (
        "87afaf1f2d91164bf5b68b1dc12eba61ea0c6267b0d2e64617e6ac43d34ff227"
    )
    assert fingerprint(strict_schema(_intent_model_schema())) == (
        "cbf01a2f6fa4ab41a40aec477c27c70c6e96a8928f498876d8d9ba649c1809ad"
    )
    assert learning_ext._planner_contract({}) == (
        learning_ext.PLAN_RULES,
        PersonalizedPlanDraft,
    )


def test_new_schema_exposes_exclusive_kinds_and_provider_compatible_pairs():
    schema = strict_schema(PersonalizedPlanDraftV162.model_json_schema())
    assert "oneOf" not in str(schema) and "discriminator" not in str(schema)
    definitions = schema["$defs"]
    branches = definitions["PlanModuleDraftV162"]["properties"]["task"]["anyOf"]
    tasks = [definitions[x["$ref"].split("/")[-1]] for x in branches if "$ref" in x]
    assert {t["properties"]["kind"]["const"] for t in tasks} == set(KINDS)
    for task in tasks:
        assert task["additionalProperties"] is False
        assert "kind" in task["required"]
        props = task["properties"]
        kind = props["kind"]["const"]
        if kind == "reading":
            assert props["scenarios"]["maxItems"] == props["acceptance_checks"]["maxItems"] == 0
        check_branches = props["acceptance_checks"]["items"]["anyOf"]
        pairs = {}
        for branch in check_branches:
            check = definitions[branch["$ref"].split("/")[-1]]["properties"]
            observable = check["observable"]
            for name in observable.get("enum", [observable.get("const")]):
                pairs[name] = check["rubric_key"]["const"]
        expected = {
            name: pair[0]
            for name, pair in OBSERVABLES.items()
            if kind in {"exercise", "transfer"} or pair[0] not in {"transfer", "runtime"}
        }
        assert pairs == expected
    for name, pair in OBSERVABLES.items():
        assert f"{name} -> {pair[0]}" in learning_ext.PLAN_RULES_V162
    for preference in ("diagnostics_enabled", "checks_enabled", "exercises_enabled"):
        assert preference in learning_ext.PLAN_RULES_V162


@pytest.mark.parametrize("field", ["scenarios", "acceptance_checks"])
def test_reading_rejects_each_assessed_payload_without_relabeling(field):
    value = draft_for("reading")
    value["modules"][0]["task"][field] = structured_draft()["modules"][0]["task"][field]
    saved = deepcopy(value)
    with pytest.raises(ValidationError):
        PersonalizedPlanDraftV162.model_validate(value)
    assert value == saved
    # Legacy parsing and its historical error remain available unchanged.
    legacy = PersonalizedPlanDraft.model_validate(value)
    with pytest.raises(DomainError) as error:
        project_task(legacy.modules[0], context())
    assert error.value.code == "LEARNING_OPT_OUT"
    current = {**context(), "planner_contract_version": PLAN_CONTRACT_VERSION}
    with pytest.raises(DomainError) as error:
        project_task(legacy.modules[0], current)
    assert error.value.code == "PLAN_TASK_KIND"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("diagnostics,checks,exercises", list(product([False, True], repeat=3)))
def test_valid_shape_does_not_override_any_frozen_opt_out(kind, diagnostics, checks, exercises):
    frozen = context()
    frozen["planner_contract_version"] = PLAN_CONTRACT_VERSION
    frozen["preferences"].update(
        diagnostics_enabled=diagnostics,
        checks_enabled=checks,
        exercises_enabled=exercises,
    )
    draft = PersonalizedPlanDraftV162.model_validate(draft_for(kind, frozen))
    allowed = {
        "reading": True,
        "diagnostic": diagnostics,
        "understanding_check": checks,
        "exercise": exercises,
        "transfer": exercises,
    }[kind]
    if allowed:
        projected = project_task(draft.modules[0], frozen)
        assert projected["kind"] == kind
    else:
        with pytest.raises(DomainError) as error:
            project_task(draft.modules[0], frozen)
        assert error.value.code == "LEARNING_OPT_OUT"
        assert kind in str(error.value) and "阅读标签" not in str(error.value)


@pytest.mark.parametrize("observable", list(OBSERVABLES))
@pytest.mark.parametrize("kind", KINDS)
def test_check_pair_and_kind_agree_between_schema_and_projection(observable, kind):
    frozen = context()
    value = draft_for(kind)
    module = value["modules"][0]
    rubric_key = OBSERVABLES[observable][0]
    module["rubric_keys"] = [rubric_key]
    module["task"]["acceptance_checks"] = [
        {
            "rubric_key": rubric_key,
            "observable": observable,
            "targets": [reference("fact.f.text", frozen["units"][0]["facts"][0]["text"])],
            "scenario_indexes": [],
        }
    ]
    allowed = kind != "reading" and (
        rubric_key not in {"transfer", "runtime"} or kind in {"exercise", "transfer"}
    )
    if not allowed:
        with pytest.raises(ValidationError):
            PersonalizedPlanDraftV162.model_validate(value)
        return
    draft = PersonalizedPlanDraftV162.model_validate(value)
    projected = project_task(draft.modules[0], frozen)
    assert projected["acceptance_checks"][0]["rubric_key"] == rubric_key
    module["task"]["acceptance_checks"][0]["rubric_key"] = (
        "conditions" if rubric_key != "conditions" else "execution_path"
    )
    with pytest.raises(ValidationError):
        PersonalizedPlanDraftV162.model_validate(value)


async def queued_plan(env, *, legacy=False, malformed=None):
    _, publication, _ = await publish_fixture(env)

    def produce(request):
        value = draft_for("reading", request["context"]["frozen_input"])
        # The general helper's profile input is absent in this minimal profile.
        value["modules"][0]["task"]["inputs"] = []
        value["uncovered_goal_requirements"] = []
        if malformed == "reading_scenario":
            value["modules"][0]["task"]["scenarios"] = [
                {
                    "kind": "as_given",
                    "target": value["modules"][0]["task"]["title"],
                }
            ]
        return ModelOutput(structured=value, synthetic=True)

    env["model"].overrides["learning_planner"] = produce
    request = env["app"].learning.start_personalized_plan(
        env["actor"], plan_input(env, publication)
    )
    # Synthetic queued DB only: emulate historical 1.6.1/1.6.2 admission,
    # before model-context 1.6.6 existed. Keep the historical request bytes and
    # their full assertions below; do not mix today's projection into the fixture.
    with env["app"].db.transaction() as db:
        run = db.get(AgentRun, request["run_id"])
        manifest = {**run.input_manifest}
        manifest.pop("planner_model_context_version")
        manifest.pop("planner_reference_prompt_version")
        manifest.pop("consumer_context_contract")
        manifest["units"] = [
            {k: v for k, v in unit.items() if k != "consumer_context"} for unit in manifest["units"]
        ]
        if legacy:
            manifest.pop("planner_contract_version")
        run.input_manifest, run.input_hash = manifest, fingerprint(manifest)
    epoch = env["app"].runtime.claim(request["run_id"], "contract-worker")
    with env["app"].db.read() as db:
        run = db.get(AgentRun, request["run_id"])
    return run, epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_contract_branches_replay_exact_receipts_after_projection_interruption(
    env, monkeypatch, legacy
):
    app = env["app"]
    run, epoch = await queued_plan(env, legacy=legacy)
    original_project = learning_ext.project_task

    def interrupted(*args):
        raise DomainError("TEST_INTERRUPTION", "Synthetic interruption after model receipt.")

    monkeypatch.setattr(learning_ext, "project_task", interrupted)
    with pytest.raises(DomainError) as error:
        await app.learning.execute_plan(run, epoch, "contract-worker")
    assert error.value.code == "TEST_INTERRUPTION"
    calls = [c for c in env["model"].calls if c["role"] == "learning_planner"]
    assert len(calls) == 1
    wire = calls[0]
    assert wire["context"]["frozen_input"] == run.input_manifest
    assert ("planner_contract_version" not in wire["context"]["frozen_input"]) == legacy
    assert wire["messages"][0]["content"] == (
        learning_ext.PLAN_RULES if legacy else learning_ext.PLAN_RULES_V162
    )
    output_type = PersonalizedPlanDraft if legacy else PersonalizedPlanDraftV162
    assert wire["schema"] == strict_schema(output_type.model_json_schema())
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
        saved = (receipt.id, receipt.request_hash, deepcopy(receipt.response))
        assert receipt.request_hash == fingerprint(wire)
        assert db.get(AgentRun, run.id).calls_consumed == 1
        assert db.get(AgentRun, run.id).call_cap == 1
        assert not db.scalars(select(LearningPlan)).all()
        funding_consumed = db.get(FundingAgreement, env["funding"]).calls_consumed
    monkeypatch.setattr(learning_ext, "project_task", original_project)
    await app.learning.execute_plan(run, epoch, "contract-worker")
    result = app.runtime.stage(run.id, "result")
    assert result is not None
    assert len([c for c in env["model"].calls if c["role"] == "learning_planner"]) == 1
    with app.db.read() as db:
        receipt = db.get(ModelReceipt, saved[0])
        assert (receipt.id, receipt.request_hash, receipt.response) == saved
        manifest = db.scalar(
            select(PlanProposalManifest).where(PlanProposalManifest.run_id == run.id)
        )
        assert manifest.payload["draft_schema_version"] == (
            "learning-plan-draft-1.6.1" if legacy else PLAN_DRAFT_VERSION
        )
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == funding_consumed
    detail = app.learning.detail(env["actor"], result["plan_id"])
    assert detail["versions"][0]["modules"][0]["content"]["task"]["kind"] == "reading"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_malformed_output_consumes_exactly_one_call_and_cannot_be_retried(env, legacy):
    app = env["app"]
    run, epoch = await queued_plan(env, legacy=legacy, malformed="reading_scenario")
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            await app.learning.execute_plan(run, epoch, "contract-worker")
        assert error.value.code == ("LEARNING_OPT_OUT" if legacy else "MODEL_SCHEMA")
    assert len([c for c in env["model"].calls if c["role"] == "learning_planner"]) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).calls_consumed == 1
        assert len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)).all()) == 1
        assert not db.scalars(select(LearningPlan)).all()
        assert not db.scalars(select(PlanProposalManifest)).all()
    assert app.runtime.stage(run.id, "result") is None


@pytest.mark.asyncio
async def test_unknown_planner_contract_stops_before_any_model_attempt(env):
    app = env["app"]
    run, epoch = await queued_plan(env)
    with app.db.transaction() as db:
        saved = db.get(AgentRun, run.id)
        saved.input_manifest = {**saved.input_manifest, "planner_contract_version": "unknown"}
        saved.input_hash = fingerprint(saved.input_manifest)
        run = saved
    with pytest.raises(DomainError) as error:
        await app.learning.execute_plan(run, epoch, "contract-worker")
    assert error.value.code == "PLAN_CONTRACT_VERSION"
    assert not [c for c in env["model"].calls if c["role"] == "learning_planner"]
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).calls_consumed == 0
        assert not db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)).all()


def test_package_and_api_versions_match(env):
    # This test covers package/API agreement, not the historical planner
    # contract version, which remains independently asserted elsewhere.
    project = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == create_app(env["app"]).version == declared


@pytest.mark.parametrize("draft_version", [None, "learning-plan-draft-1.6.1", PLAN_DRAFT_VERSION])
@pytest.mark.parametrize("marker", [None, PLAN_CONTRACT_VERSION, "unknown"])
def test_historical_draft_and_input_contract_markers_must_agree(draft_version, marker):
    frozen = context()
    if marker is not None:
        frozen["planner_contract_version"] = marker
    value = draft_for("reading")
    value["uncovered_goal_requirements"] = []
    data = {"input": frozen, "draft": value}
    if draft_version is not None:
        data["draft_schema_version"] = draft_version
    expected_marker = PLAN_CONTRACT_VERSION if draft_version == PLAN_DRAFT_VERSION else None
    if marker == expected_marker:
        assert learning_ext.LearningLoop._project_manifest(data)["source_gap_count"] == 0
    else:
        with pytest.raises(DomainError) as error:
            learning_ext.LearningLoop._project_manifest(data)
        assert error.value.code == "PLAN_MANIFEST_SCHEMA"
