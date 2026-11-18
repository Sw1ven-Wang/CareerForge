"""Original public C09 setup fidelity only. No model evaluation or answer oracle."""

from copy import deepcopy
from pathlib import Path

import pytest
from public_exercise_support import (
    MAPPINGS,
    RUNTIME_DECLARATION,
    SCOPED_MAPPINGS,
    ExerciseSetupFixture,
    attempt_declarations,
    read_inputs,
    selected_mapping,
    setup_context,
    verify_snapshot,
)
from test_question_observables_v166 import scoped_snapshot

from careerforge.contracts_learning_ext import LearningPreferences
from careerforge.contracts_learning_scope_v166 import (
    QUESTION_PLAN_CONTRACT,
    QUESTION_SCOPE,
    QuestionPlanDraft,
)
from careerforge.services.learning_projection import (
    QUOTE_PROJECTION_VERSION,
    project_task,
    text_catalog,
)
from careerforge.services.learning_scope_v166 import question_activity


def test_all_original_public_inputs_use_explicit_narrow_source_bound_observables(tmp_path):
    cases, exercises, files = read_inputs(Path(__file__).parent / "fixtures" / "public_c09")
    fixture = ExerciseSetupFixture(cases, exercises, tmp_path, task_scope=QUESTION_SCOPE)
    frozen = {
        "planner_contract_version": QUESTION_PLAN_CONTRACT,
        "task_scope": QUESTION_SCOPE,
        "quote_projection_version": QUOTE_PROJECTION_VERSION,
        "preferences": LearningPreferences(
            self_reported_context=setup_context(cases, exercises)
        ).model_dump(mode="json"),
        "title": "Public C09 original questions",
        "goal": None,
        "units": [
            {
                "publication_id": exercise_id,
                "title": exercise_id,
                "facts": [
                    {
                        "fact_atom_id": exercise_id + "-source",
                        "text": files[exercise_id][exercise["source_path"]].strip(),
                        "conditions": [],
                    }
                ],
                "limitations": [],
            }
            for exercise_id, exercise in exercises.items()
        ],
    }
    raw = fixture.plan(
        {
            "context": {
                "frozen_input": frozen,
                "text_catalog": list(text_catalog(frozen).values()),
            }
        }
    )
    draft = QuestionPlanDraft.model_validate(raw)
    assert len(draft.modules) == len(cases) == 8
    for case, module in zip(cases, draft.modules, strict=True):
        task = project_task(module, frozen)
        prompt, rubric = question_activity(task, module.rubric_keys)
        declaration = attempt_declarations(case)
        snapshot = {
            "assessment_scope": QUESTION_SCOPE,
            "task": task,
            "prompt": prompt,
            "rubric": rubric,
            "response": case["user_submission"]["answer"],
            "transfer_notes": "",
            "runtime_evidence": [],
            "source_ids": ["source"],
            "specification_hash": "frozen-test-hash",
            "help_conditions": {
                "assistance_mode": declaration["expected_help_label"],
                "external_help": declaration["external_help"],
                "declared_assistance_mode": declaration["assistance_mode"],
            },
        }
        fidelity = verify_snapshot(
            case, snapshot, {"specification_hash": "frozen-test-hash"}, "source"
        )
        assert fidelity["explicit_question_scope"]
        assert fidelity["per_observable_identity_preserved"]
        assert fidelity["generic_rubric_requirements_not_added"]
        assert not fidelity["semantic_grading_verified"]
        assert [item["observable_identity"]["quote"] for item in rubric] == exercises[
            case["exercise_id"]
        ]["required_observables"]
    assert fixture.sequence == 0  # Not even synthetic model calls were performed.


def test_original_mapping_remains_available_only_without_new_opt_in():
    assert selected_mapping() is MAPPINGS
    assert selected_mapping(QUESTION_SCOPE) is SCOPED_MAPPINGS
    assert MAPPINGS["EX-CACHE"][0]["observable"] == "explain_conditions"
    assert SCOPED_MAPPINGS["EX-CACHE"][0]["observable"] == "state_condition"
    assert SCOPED_MAPPINGS["EX-BRANCH"][0]["observable"] == "identify_members"
    with pytest.raises(ValueError, match="Unknown task scope"):
        selected_mapping("unknown-contract")


def test_scoped_adapter_matches_each_identity_not_just_its_shared_dimension(monkeypatch):
    # A generalized synthetic mechanism fixture, not another public input or Gold.
    snapshot = scoped_snapshot()
    case = {
        "case_id": "generic-case",
        "exercise_id": "EX-GENERIC",
        "plan_version": "generic-plan",
        "question": snapshot["task"]["title"],
        "user_submission": {"answer": snapshot["response"], "help_used": True},
    }
    snapshot["task"]["inputs"] = [
        {"quote": value}
        for value in (
            case["question"],
            case["exercise_id"],
            case["plan_version"],
            case["case_id"],
            RUNTIME_DECLARATION,
        )
    ]
    snapshot.update(source_ids=["generic-source"], specification_hash="generic-hash")
    snapshot["help_conditions"] = {
        "assistance_mode": "assisted",
        "declared_assistance_mode": "assisted",
        "external_help": "used",
    }
    mappings = [
        {
            "required_observable": row["observable_identity"]["quote"],
            "rubric_key": row["key"],
            "observable": row["specific_criteria"][0]["observable"],
            "source_quotes": [ref["quote"] for ref in row["specific_criteria"][0]["targets"]],
        }
        for row in snapshot["rubric"]
    ]
    monkeypatch.setitem(MAPPINGS, "EX-GENERIC", deepcopy(mappings))
    monkeypatch.setitem(SCOPED_MAPPINGS, "EX-GENERIC", deepcopy(mappings))
    activity = {"specification_hash": "generic-hash"}
    assert verify_snapshot(case, snapshot, activity, "generic-source")[
        "per_observable_identity_preserved"
    ]
    snapshot["rubric"][2]["id"] = snapshot["rubric"][0]["id"]
    with pytest.raises(AssertionError, match="shared rubric identity"):
        verify_snapshot(case, snapshot, activity, "generic-source")
