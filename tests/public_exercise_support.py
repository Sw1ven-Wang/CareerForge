"""Portable public C09 setup-fidelity helper; never produces an evaluation answer.

It preserves original questions/submissions, maps public observables explicitly to
closed product rubric keys, and uses source code verbatim as fixture facts. No
source code is executed and no learner execution receipt is created.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

FIXTURE_NOTICE = (
    "C09 stage fixture: synthetic_lab source, deterministic source/review/plan setup; "
    "not native repository analysis, not learner execution, not measured quality."
)
RUNTIME_DECLARATION = "runtime_environment=not_observed; runtime_result_ref=null"
MAPPINGS = {
    "EX-CACHE": [
        {
            "required_observable": "expiry_compare",
            "rubric_key": "conditions",
            "observable": "explain_conditions",
            "source_quotes": [
                "self.items[key] = (value, now + ttl)",
                "if now >= expires_at:",
            ],
        },
        {
            "required_observable": "return_on_expired",
            "rubric_key": "execution_path",
            "observable": "trace_input_output",
            "source_quotes": [
                "if now >= expires_at:\n            self.items.pop(key, None)\n            return None"
            ],
        },
        {
            "required_observable": "no_runtime_fabrication",
            "rubric_key": "runtime",
            "observable": "verify_runtime_receipt",
            "source_quotes": [],
        },
    ],
    "EX-BRANCH": [
        {
            "required_observable": "allowed_destination",
            "rubric_key": "conditions",
            "observable": "explain_conditions",
            "source_quotes": ['ALLOWED = {"review", "end"}'],
        },
        {
            "required_observable": "invalid_destination",
            "rubric_key": "execution_path",
            "observable": "trace_input_output",
            "source_quotes": [
                'if value not in ALLOWED:\n        raise ValueError("invalid destination")'
            ],
        },
        {
            "required_observable": "no_runtime_fabrication",
            "rubric_key": "runtime",
            "observable": "verify_runtime_receipt",
            "source_quotes": [],
        },
    ],
}

# Old run01 mappings remain unchanged. New runs must explicitly select the
# versioned question contract; this is a scope adapter, never an answer oracle.
SCOPED_MAPPINGS = deepcopy(MAPPINGS)
for _exercise, _operations in {
    "EX-CACHE": ["state_condition", "state_result", "verify_runtime_receipt"],
    "EX-BRANCH": ["identify_members", "state_result", "verify_runtime_receipt"],
}.items():
    for _item, _operation in zip(SCOPED_MAPPINGS[_exercise], _operations, strict=True):
        _item["observable"] = _operation


def selected_mapping(task_scope=None):
    if task_scope is None:
        return MAPPINGS
    if task_scope == "question_observables":
        return SCOPED_MAPPINGS
    raise ValueError("Unknown task scope must not silently fall back to legacy criteria")


def read_inputs(dataset: Path):
    dataset = dataset.resolve()
    permitted = (Path(__file__).parent / "fixtures" / "public_c09").resolve()
    if dataset != permitted:
        raise ValueError("This helper accepts only the packaged public C09 dataset")
    provenance = json.loads((dataset / "PROVENANCE.json").read_text())
    expected_files = {
        "inputs/step09_exercise_evaluation.jsonl",
        "fixtures/exercises.json",
        "fixtures/synthetic_lab/mini_cache.py",
        "fixtures/synthetic_lab/mini_routes.py",
    }
    if set(provenance["files"]) != expected_files:
        raise ValueError("Public C09 provenance file set changed")
    for relative, record in provenance["files"].items():
        raw = (dataset / relative).read_bytes()
        if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("Packaged public C09 bytes differ from their source provenance")
    cases_path = dataset / "inputs/step09_exercise_evaluation.jsonl"
    cases = [json.loads(line) for line in cases_path.read_text().splitlines() if line.strip()]
    exercises = json.loads((dataset / "fixtures/exercises.json").read_text())
    exercise_map = {row["exercise_id"]: row for row in exercises}
    if [row["case_id"] for row in cases] != [f"C09-A{i:02}" for i in range(1, 9)]:
        raise ValueError("Expected all eight original public C09 cases in their original order")
    files = {}
    for identifier, exercise in exercise_map.items():
        if identifier not in MAPPINGS:
            raise ValueError("Unknown exercise must not be silently substituted")
        if exercise["required_observables"] != [
            x["required_observable"] for x in MAPPINGS[identifier]
        ]:
            raise ValueError(
                "Original rubric changed; review mapping rather than infer replacements"
            )
        path = exercise["source_path"]
        resolved = (dataset / path).resolve()
        if not resolved.is_relative_to(dataset / "fixtures/synthetic_lab"):
            raise ValueError("Only the explicitly synthetic lab source is permitted")
        files[identifier] = {path: resolved.read_text()}
        for item in MAPPINGS[identifier]:
            if not all(quote in files[identifier][path] for quote in item["source_quotes"]):
                raise ValueError("Mapped source text does not match the frozen synthetic fixture")
    for case in cases:
        submission = case["user_submission"]
        if case["exercise_id"] != case["requested_rubric_ref"]:
            raise ValueError("Exercise and requested rubric differ")
        if case["plan_version"] != "PLAN-ACCEPTED-01":
            raise ValueError("Original plan alias changed")
        if (
            case["runtime_environment"] != "not_observed"
            or submission["runtime_result_ref"] is not None
        ):
            raise ValueError(
                "Runtime declaration changed; this harness does not simulate execution"
            )
        if type(submission["help_used"]) is not bool:
            raise ValueError("Exact help declaration is required")
    return cases, exercise_map, files


def setup_context(cases, exercises):
    """Request references only. Original answer texts never enter setup model calls."""
    rows = [FIXTURE_NOTICE, "PLAN-ACCEPTED-01", RUNTIME_DECLARATION]
    for exercise in exercises.values():
        rows.extend(
            [exercise["exercise_id"], exercise["description"], *exercise["required_observables"]]
        )
    rows.extend(case["case_id"] + ": " + case["question"] for case in cases)
    value = "\n".join(rows)
    if len(value) > 2000:
        raise ValueError("Explicit request catalog does not fit the product profile limit")
    return value


def attempt_declarations(case):
    used = case["user_submission"]["help_used"]
    return {
        "assistance_mode": "assisted" if used else "independent",
        "external_help": "used" if used else "none_declared",
        "expected_help_label": "assisted" if used else "self_declared_independent",
        "interpretation": "self declaration only; not independent observation of help usage",
    }


class ExerciseSetupFixture:
    """Deterministic plan setup only; no model completion or response bridge."""

    synthetic = True
    supports_embeddings = False
    supports_reranking = False
    profile_version = "public-c09-deterministic-stage-fixture-v1-not-model-quality"

    def __init__(self, cases, exercises, directory, task_scope=None):
        self.cases = deepcopy(cases)
        self.exercises = deepcopy(exercises)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.task_scope = task_scope
        self.mappings = selected_mapping(task_scope)
        if task_scope:
            self.profile_version += "-question-observables-v1"

    def plan(self, request):
        context = request["context"]
        units = {unit["title"]: unit for unit in context["frozen_input"]["units"]}
        catalog = {row["text_id"]: row for row in context["text_catalog"]}
        request_text = catalog["profile.context"]["text"]

        def reference(text):
            if text not in request_text:
                raise ValueError("Public request text is not in the frozen request catalog")
            return {"text_id": "profile.context", "quote": text}

        modules = []
        for case in self.cases:
            unit = units[case["exercise_id"]]
            if len(unit["facts"]) != 1:
                raise ValueError("Expected exactly one verbatim source fixture fact")
            fact = unit["facts"][0]
            fact_id = fact["fact_atom_id"]
            checks, boundaries = [], []
            for mapping in self.mappings[case["exercise_id"]]:
                targets = [] if self.task_scope else [reference(mapping["required_observable"])]
                for quote in mapping["source_quotes"]:
                    if quote not in fact["text"]:
                        raise ValueError("Mapped exact source quote missing from fixture fact")
                    ref = {"text_id": f"fact.{fact_id}.text", "quote": quote}
                    targets.append(ref)
                    if ref not in boundaries:
                        boundaries.append(ref)
                if mapping["rubric_key"] == "runtime" and not self.task_scope:
                    targets.append(reference(RUNTIME_DECLARATION))
                checks.append(
                    {
                        "rubric_key": mapping["rubric_key"],
                        "observable": mapping["observable"],
                        "targets": targets,
                        **(
                            {"observable_identity": reference(mapping["required_observable"])}
                            if self.task_scope
                            else {"scenario_indexes": []}
                        ),
                    }
                )
            modules.append(
                {
                    "key": case["case_id"],
                    "publication_id": unit["publication_id"],
                    "focus_fact_ids": [fact_id],
                    "prerequisite_keys": [],
                    "steps": ["read_reviewed"]
                    if self.task_scope
                    else ["read_reviewed", "explain_conditions", "trace_path"],
                    "rubric_keys": [
                        row["rubric_key"] for row in self.mappings[case["exercise_id"]]
                    ],
                    "estimated_minutes": 5,
                    "rationale_codes": ["focus_on_function"],
                    "task": {
                        "kind": "exercise",
                        "title": reference(case["question"]),
                        **({"question": reference(case["question"])} if self.task_scope else {}),
                        "inputs": [
                            reference(text)
                            for text in [
                                case["question"],
                                case["exercise_id"],
                                case["plan_version"],
                                case["case_id"],
                                FIXTURE_NOTICE,
                                RUNTIME_DECLARATION,
                            ]
                        ],
                        "scenarios": [],
                        "boundaries": boundaries,
                        "acceptance_checks": checks,
                    },
                }
            )
        return {"modules": modules, "uncovered_goal_requirements": []}


def verify_snapshot(case, snapshot, activity, source_id):
    """Mechanical fidelity checks, deliberately not a semantic grading oracle."""
    if snapshot["response"] != case["user_submission"]["answer"] or snapshot["transfer_notes"]:
        raise AssertionError("The learner submission was changed")
    if snapshot["task"]["title"] != case["question"]:
        raise AssertionError("Original exercise question was changed")
    quotes = [ref["quote"] for ref in snapshot["task"]["inputs"]]
    for text in (
        case["question"],
        case["exercise_id"],
        case["plan_version"],
        case["case_id"],
        RUNTIME_DECLARATION,
    ):
        if text not in quotes:
            raise AssertionError("Original task identity/declaration was omitted")
    if snapshot["runtime_evidence"]:
        raise AssertionError("An execution receipt was manufactured")
    if snapshot["source_ids"] != [source_id]:
        raise AssertionError("The original exercise source was substituted")
    declaration = attempt_declarations(case)
    help_conditions = snapshot["help_conditions"]
    if (
        help_conditions["assistance_mode"] != declaration["expected_help_label"]
        or help_conditions["external_help"] != declaration["external_help"]
        or help_conditions["declared_assistance_mode"] != declaration["assistance_mode"]
    ):
        raise AssertionError("Help declaration was changed")
    scoped = snapshot.get("assessment_scope") == "question_observables"
    mappings = selected_mapping("question_observables" if scoped else None)
    observed = []
    if scoped and len({row["id"] for row in snapshot["rubric"]}) != len(snapshot["rubric"]):
        raise AssertionError("Original observables were merged into a shared rubric identity")
    for rubric in snapshot["rubric"]:
        criteria = rubric["specific_criteria"]
        identity = criteria[0].get("observable_identity") if scoped and len(criteria) == 1 else None
        expected = next(
            (
                row
                for row in mappings[case["exercise_id"]]
                if (
                    row["required_observable"] == identity.get("quote")
                    if scoped and identity
                    else row["rubric_key"] == rubric["key"]
                )
            ),
            None,
        )
        if expected is None or expected["rubric_key"] != rubric["key"]:
            raise AssertionError("Unknown or conflicting original observable identity")
        if (
            len(criteria) != 1
            or (identity["quote"] if identity else criteria[0]["targets"][0]["quote"])
            != expected["required_observable"]
        ):
            raise AssertionError("Original observable missing or merged into a different rubric")
        if scoped:
            if (
                criteria[0]["observable"] != expected["observable"]
                or criteria[0]["question_reference"]["quote"] != case["question"]
                or rubric["observable_identity"] != identity
                or rubric["criterion"] != criteria[0]["criterion"]
                or snapshot["task"]["question_reference"]["quote"] != case["question"]
                or snapshot["task"]["scenarios"]
            ):
                raise AssertionError("Narrow question or observable scope was expanded")
            if [row["quote"] for row in criteria[0]["targets"]] != expected["source_quotes"]:
                raise AssertionError("Narrow original observable source targets changed")
        observed.append(expected["required_observable"])
    if observed != [row["required_observable"] for row in MAPPINGS[case["exercise_id"]]]:
        raise AssertionError("Original rubric order changed")
    if snapshot["specification_hash"] != activity["specification_hash"]:
        raise AssertionError("Activity changed before the attempt")
    return {
        "original_question_preserved": True,
        "original_submission_preserved": True,
        "original_help_declaration_preserved": True,
        "original_rubric_targets_preserved": True,
        "original_exercise_source_preserved": True,
        "runtime_evidence_empty": True,
        "semantic_grading_verified": False,
        **(
            {
                "explicit_question_scope": True,
                "per_observable_identity_preserved": True,
                "generic_rubric_requirements_not_added": True,
            }
            if scoped
            else {}
        ),
    }
