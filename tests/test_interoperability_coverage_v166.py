from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/validate_interoperability.py"
spec = importlib.util.spec_from_file_location("interoperability_validator", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def dataset(tmp_path):
    root = tmp_path / "public"
    (root / "inputs").mkdir(parents=True)
    (root / "manifests").mkdir()
    (root / "inputs/step01.jsonl").write_text(json.dumps({"case_id": "CASE-A"}) + "\n")
    (root / "inputs/step11_training_feedback.jsonl").write_text(
        json.dumps({"event_id": "TRAIN-A"}) + "\n"
    )
    (root / "manifests/expected_output_fields.json").write_text(
        json.dumps({"step01": ["raw_sha256", "paragraphs[id,text]"]})
    )
    return root


def audit(tmp_path, row, event):
    root = dataset(tmp_path)
    submission, trace = tmp_path / "submission.jsonl", tmp_path / "trace.jsonl"
    submission.write_text(json.dumps(row) + "\n")
    trace.write_text(json.dumps(event) + "\n")
    return validator.audit_submission(root, submission, trace)


def test_not_run_never_passes_even_with_fields(tmp_path):
    result = audit(
        tmp_path,
        {
            "case_id": "CASE-A",
            "stage": "step01",
            "status": "not_run",
            "output": {"raw_sha256": "x", "paragraphs": []},
            "trace_refs": ["e1"],
        },
        {"event_id": "e1", "case_id": "CASE-A", "event_type": "template_created"},
    )
    assert result["structural_status"] == "incomplete"
    assert result["stages"]["step01"]["executed"] == 0
    assert result["expected_cases"] == 1 and result["training_feedback_events"] == 1


def test_stage_and_trace_cannot_be_borrowed(tmp_path):
    result = audit(
        tmp_path,
        {
            "case_id": "CASE-A",
            "stage": "step02",
            "status": "answered",
            "output": {"raw_sha256": "x", "paragraphs": []},
            "trace_refs": ["e1"],
        },
        {"event_id": "e1", "case_id": "ANOTHER", "event_type": "operation_completed"},
    )
    reasons = {row["reason"] for row in result["errors"]}
    assert {"stage_mismatch", "missing_or_cross_case_trace"} <= reasons


def test_complete_structure_is_not_semantic_pass(tmp_path):
    result = audit(
        tmp_path,
        {
            "case_id": "CASE-A",
            "stage": "step01",
            "status": "answered",
            "output": {"raw_sha256": "x", "paragraphs": []},
            "trace_refs": ["e1"],
        },
        {"event_id": "e1", "case_id": "CASE-A", "event_type": "operation_completed"},
    )
    assert result["structural_status"] == "complete"
    assert result["semantic_quality"] == "not_scored"
    assert result["runtime_success"] == "not_inferred"


def test_bulk_extraction_checks_each_requirement(tmp_path):
    root = dataset(tmp_path)
    (root / "inputs/step01.jsonl").unlink()
    (root / "inputs/step02_extract_requirements.jsonl").write_text(
        json.dumps({"case_id": "CASE-A"}) + "\n"
    )
    (root / "manifests/expected_output_fields.json").write_text(
        json.dumps({"step02_extract_requirements": ["requirement_id", "quote"]})
    )
    row = {
        "case_id": "CASE-A",
        "stage": "step02_extract_requirements",
        "status": "answered",
        "output": {
            "requirements": [{"requirement_id": "r1", "quote": "literal"}, {"requirement_id": "r2"}]
        },
        "trace_refs": ["e1"],
    }
    submission, trace = tmp_path / "submission.jsonl", tmp_path / "trace.jsonl"
    submission.write_text(json.dumps(row) + "\n")
    trace.write_text(
        json.dumps({"event_id": "e1", "case_id": "CASE-A", "event_type": "operation_completed"})
        + "\n"
    )
    result = validator.audit_submission(root, submission, trace)
    assert result["structural_status"] == "incomplete"
    assert {"case_id": "CASE-A", "reason": "missing_field:quote"} in result["errors"]
    row["output"]["requirements"][1]["quote"] = "also literal"
    submission.write_text(json.dumps(row) + "\n")
    assert validator.audit_submission(root, submission, trace)["structural_status"] == "complete"
