"""Real legacy-process paid and no-funding SQLite recovery compatibility.

Set CAREERFORGE_LEGACY_SOURCE and CAREERFORGE_COMPLETION_COMPATIBILITY_HARNESS.
Synthetic providers exercise protocol and persistence, not native model quality.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_real_legacy_paid_and_no_run_completion_recovery(tmp_path):
    legacy = os.environ.get("CAREERFORGE_LEGACY_SOURCE")
    harness = os.environ.get("CAREERFORGE_COMPLETION_COMPATIBILITY_HARNESS")
    if not legacy or not harness:
        pytest.skip("Explicit legacy source and completion compatibility harness are required")
    output = tmp_path / "legacy-completion-evidence"
    candidate = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            harness,
            "--legacy-source",
            legacy,
            "--candidate-source",
            str(candidate),
            "--output",
            str(output),
            "--suite",
            "all",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    paid = json.loads((output / "compatibility_report.json").read_text())
    assert paid["passed"]
    assert paid["paid"]["new_provider_calls"] == paid["completed"]["new_provider_calls"] == []
    assert paid["queued"]["legacy_wire"] == paid["queued"]["candidate_wire"]
    no_run = json.loads((output / "no_run_compatibility_report.json").read_text())
    assert no_run["passed"]
    assert set(no_run["cases"]) == {"before_plan", "after_plan", "completed"}
    for phase, case in no_run["cases"].items():
        assert case["passed"] and case["candidate_model_call_count"] == 0
        assert case["original_model_call_count"] == 0
        if phase != "before_plan":
            assert case["preserved_frozen_plan"]
