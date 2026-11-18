"""Fail-closed structural coverage for public multi-stage submissions.

This does NOT score semantic correctness. A nonempty answer and a matching trace
are necessary execution evidence, never proof the answer is right. Independent
judging is still required. Gold is never opened by this validator.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TERMINAL = {"answered", "clarification_needed", "insufficient", "denied", "failed"}
NONEXECUTED = {"not_run", "pending", "running"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{number}: expected object")
            rows.append(value)
    return rows


def audit_submission(dataset: Path, submission: Path, trace_path: Path) -> dict[str, Any]:
    expected = {}
    training_events = []
    for path in sorted((dataset / "inputs").glob("*.jsonl")):
        for row in load_jsonl(path):
            identifier = row.get("case_id") or row.get("trajectory_id")
            if identifier is None:
                training_events.append(row.get("event_id"))
                continue
            if identifier in expected:
                raise ValueError("Duplicate input case identity")
            expected[identifier] = path.stem
    required = json.loads((dataset / "manifests/expected_output_fields.json").read_text())
    traces = load_jsonl(trace_path)
    trace_by_id = {}
    for event in traces:
        identity = event.get("event_id")
        if not isinstance(identity, str) or not identity or identity in trace_by_id:
            raise ValueError("Trace events need unique, nonempty event_id values")
        trace_by_id[identity] = event
    rows = load_jsonl(submission)
    by_case, errors, stage_counts = {}, [], {}
    for row in rows:
        identifier, stage, status = row.get("case_id"), row.get("stage"), row.get("status")
        if identifier in by_case:
            errors.append({"case_id": identifier, "reason": "duplicate_submission"})
            continue
        by_case[identifier] = row
        if identifier not in expected:
            errors.append({"case_id": identifier, "reason": "unknown_case"})
            continue
        stage_counts.setdefault(
            expected[identifier],
            {"expected": 0, "executed": 0, "not_run": 0, "failed": 0, "structurally_complete": 0},
        )
        counts = stage_counts[expected[identifier]]
        row_errors = []
        if stage != expected[identifier]:
            row_errors.append("stage_mismatch")
        if status in NONEXECUTED:
            counts["not_run"] += 1
            row_errors.append("not_executed")
        elif status not in TERMINAL:
            row_errors.append("invalid_status")
        else:
            counts["executed"] += 1
            if status == "failed":
                counts["failed"] += 1
        output = row.get("output")
        if not isinstance(output, dict) or not output:
            row_errors.append("missing_output")
            output = {}
        # One submitted case may extract several requirements or discover several
        # candidates. Validate each actual record, not a made-up aggregate ID.
        collection = {
            "step02_extract_requirements": "requirements",
            "step03_research_candidates": "candidates",
        }.get(expected[identifier])
        records = output.get(collection) if collection else None
        if records is not None:
            if not isinstance(records, list) or not records:
                row_errors.append("missing_collection:" + str(collection))
                records = []
        else:
            records = [output]
        for record in records:
            if not isinstance(record, dict):
                row_errors.append("invalid_collection_record")
                continue
            for expression in required.get(expected[identifier], []):
                key = expression.split("[", 1)[0]
                if key not in record:
                    row_errors.append("missing_field:" + key)
        if status in {"denied", "insufficient", "clarification_needed", "failed"} and not (
            row.get("reason_codes") or output.get("reason_codes") or row.get("answer")
        ):
            row_errors.append("missing_outcome_reason")
        refs = row.get("trace_refs")
        if not isinstance(refs, list) or not refs:
            row_errors.append("missing_trace_refs")
        else:
            for ref in refs:
                event = trace_by_id.get(ref)
                if event is None or event.get("case_id") != identifier:
                    row_errors.append("missing_or_cross_case_trace")
                elif event.get("status") in NONEXECUTED or event.get("event_type") in {
                    "planned",
                    "template_created",
                }:
                    row_errors.append("nonexecution_trace")
        if not row_errors:
            counts["structurally_complete"] += 1
        errors.extend(
            {"case_id": identifier, "reason": reason} for reason in sorted(set(row_errors))
        )
    for identifier, stage in expected.items():
        stage_counts.setdefault(
            stage,
            {"expected": 0, "executed": 0, "not_run": 0, "failed": 0, "structurally_complete": 0},
        )["expected"] += 1
        if identifier not in by_case:
            errors.append({"case_id": identifier, "reason": "missing_submission"})
    return {
        "validator": "structural_execution_coverage_1.6.6",
        "expected_cases": len(expected),
        "training_feedback_events": len(training_events),
        "submission_rows": len(rows),
        "trace_events": len(traces),
        "stages": stage_counts,
        "errors": errors,
        "structural_status": "complete" if not errors else "incomplete",
        "semantic_quality": "not_scored",
        "runtime_success": "not_inferred",
        "policy_improvement": "not_inferred",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_submission(args.agent_input, args.submission, args.trace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("expected_cases", "structural_status", "semantic_quality")
            }
        )
    )
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
