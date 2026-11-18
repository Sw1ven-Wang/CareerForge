"""Pure prompt/projection regressions; no app, database, or model execution."""

from copy import deepcopy

import pytest
from test_learning_projection_repair import context, reference, structured_draft
from test_planner_context_v166 import request, snapshot

from careerforge.contracts_learning_ext import PersonalizedPlanDraft
from careerforge.contracts_learning_plan_v162 import (
    PLAN_CONTRACT_VERSION,
    PersonalizedPlanDraftV162,
)
from careerforge.contracts_learning_scope_v166 import (
    QUESTION_PLAN_CONTRACT,
    QUESTION_SCOPE,
    QuestionPlanDraft,
)
from careerforge.errors import DomainError
from careerforge.services import learning_ext
from careerforge.services.learning_projection import (
    EVIDENCE_KINDS,
    GOAL_KINDS,
    project_gaps,
    project_task,
    text_catalog,
)
from careerforge.services.learning_scope_v166 import QUESTION_PLAN_RULES
from careerforge.services.session_plans import SESSION_PLAN_CONTRACT, SESSION_PLAN_RULES
from careerforge.util import fingerprint

MARKER = {"planner_reference_prompt_version": learning_ext.PLANNER_REFERENCE_PROMPT_VERSION}


@pytest.mark.parametrize("session", [False, True])
def test_explicit_marker_adds_precise_field_rules_without_changing_schema(session):
    frozen = {"planner_contract_version": PLAN_CONTRACT_VERSION}
    if session:
        frozen["session_contract"] = SESSION_PLAN_CONTRACT
    suffix = SESSION_PLAN_RULES if session else ""
    assert learning_ext._planner_contract(frozen) == (
        learning_ext.PLAN_RULES_V162 + suffix,
        PersonalizedPlanDraftV162,
    )
    rules, schema = learning_ext._planner_contract({**frozen, **MARKER})
    assert rules == learning_ext.PLAN_RULES_V162 + learning_ext.PLAN_REFERENCE_RULES + suffix
    assert schema is PersonalizedPlanDraftV162
    assert "task.boundaries只允许fact_text、fact_condition、unit_limitation" in rules
    assert "必须属于本模块publication_id" in rules
    assert "fact引用还必须属于本模块focus_fact_ids" in rules
    assert "profile.context(self_reported_context)" in rules
    assert "需要引用时可放入task.inputs，不能变成审核证据边界" in rules
    assert "没有适用的来源边界时boundaries保持空数组" in rules
    assert "原有条件仍然适用" in rules
    assert (
        "task.title、task.inputs、task.scenarios[].target、task.acceptance_checks[].targets可引用"
        in rules
    )
    assert "缺口的evidence不受单个模块focus_fact_ids限制" in rules
    assert EVIDENCE_KINDS == {"fact_text", "fact_condition", "unit_limitation"}
    assert GOAL_KINDS == {"plan_title", "self_reported_context", "goal_text", "goal_requirement"}


@pytest.mark.parametrize("marker", ["unknown", "", False, 1, [], {}])
def test_unknown_marker_fails_closed(marker):
    frozen = {
        "planner_contract_version": PLAN_CONTRACT_VERSION,
        "planner_reference_prompt_version": marker,
    }
    with pytest.raises(DomainError) as error:
        learning_ext._planner_contract(frozen)
    assert error.value.code == "PLAN_REFERENCE_PROMPT_VERSION"


@pytest.mark.parametrize("version", [None, QUESTION_PLAN_CONTRACT])
def test_reference_marker_cannot_silently_upgrade_other_contracts(version):
    frozen = {"planner_contract_version": version, **MARKER}
    with pytest.raises(DomainError) as error:
        learning_ext._planner_contract(frozen)
    assert error.value.code == "PLAN_REFERENCE_PROMPT_VERSION"


def test_absent_and_null_markers_preserve_legacy_and_question_branches():
    for marker in ({}, {"planner_reference_prompt_version": None}):
        assert learning_ext._planner_contract(marker) == (
            learning_ext.PLAN_RULES,
            PersonalizedPlanDraft,
        )
        assert learning_ext._planner_contract(
            {
                "planner_contract_version": QUESTION_PLAN_CONTRACT,
                "task_scope": QUESTION_SCOPE,
                **marker,
            }
        ) == (QUESTION_PLAN_RULES, QuestionPlanDraft)


def test_marker_is_bound_into_frozen_and_model_request_hashes_not_output_schema():
    old = snapshot()
    before = deepcopy(old)
    current = {**old, **MARKER}
    old_request, new_request = request(old), request(current)
    assert (
        new_request["context"]["frozen_input"]["planner_reference_prompt_version"]
        == MARKER["planner_reference_prompt_version"]
    )
    assert fingerprint(old) != fingerprint(current)
    assert fingerprint(old_request) != fingerprint(new_request)
    assert old_request["schema"] == new_request["schema"]
    assert old_request["access_dependencies"] == new_request["access_dependencies"]
    assert old == before
    assert request(old) == old_request


def test_new_prompt_preserves_valid_evidence_and_request_data_projection():
    frozen = {**context(), "planner_contract_version": PLAN_CONTRACT_VERSION}
    marked = {**frozen, **MARKER}
    raw = structured_draft(frozen)
    task = raw["modules"][0]["task"]
    catalog = text_catalog(frozen)
    task["boundaries"] = [
        reference(key, catalog[key]["text"])
        for key in ("fact.f.text", "fact.f.condition.0", "unit.p.limitation.0")
    ]
    # Request data is allowed in these fields, but is never reviewed evidence.
    task["title"] = reference("profile.context", "RetryableError")
    task["acceptance_checks"][0]["targets"] = [reference("profile.context", "RetryableError")]
    # Goal-gap evidence is catalog-scoped, not limited to this module's focus f.
    raw["uncovered_goal_requirements"][0]["evidence"] = [
        reference("fact.r.text", catalog["fact.r.text"]["text"])
    ]
    draft = PersonalizedPlanDraftV162.model_validate(raw)
    projected = project_task(draft.modules[0], marked)
    assert projected == project_task(draft.modules[0], frozen)
    assert {r["origin"] for r in projected["boundaries"]} == EVIDENCE_KINDS
    assert projected["inputs"][0]["origin"] == "self_reported_context"
    assert projected["inputs"][0]["reviewed_source"] is False
    assert projected["runtime_observed"] is projected["competence_verified"] is False
    assert project_gaps(draft, marked) == project_gaps(draft, frozen)


@pytest.mark.parametrize("kind", sorted(GOAL_KINDS | {"unit_title"}))
def test_new_prompt_does_not_make_request_or_title_data_valid_boundaries(kind):
    frozen = {**context(), "planner_contract_version": PLAN_CONTRACT_VERSION, **MARKER}
    frozen["goal"] = {
        "source_text": "Learn retry boundaries.",
        "requirements": [{"quote": "Trace retry failure.", "technology": "retry"}],
    }
    entry = next(e for e in text_catalog(frozen).values() if e["kind"] == kind)
    raw = structured_draft(frozen)
    raw["modules"][0]["task"]["boundaries"] = [reference(entry["text_id"], entry["text"])]
    before = deepcopy(raw)
    draft = PersonalizedPlanDraftV162.model_validate(raw)
    with pytest.raises(DomainError) as error:
        project_task(draft.modules[0], frozen)
    assert error.value.code == "PLAN_TEXT_SCOPE"
    assert raw == before
