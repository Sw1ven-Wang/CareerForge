"""True cross-code replay, with the original database created by a legacy process.

Set CAREERFORGE_LEGACY_SOURCE and CAREERFORGE_COMPATIBILITY_HARNESS to run.
Synthetic model verdicts test protocol compatibility, not model quality.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_real_legacy_database_preserves_checker_wire_and_paid_replay(tmp_path):
    legacy = os.environ.get("CAREERFORGE_LEGACY_SOURCE")
    harness = os.environ.get("CAREERFORGE_COMPATIBILITY_HARNESS")
    if not legacy or not harness:
        pytest.skip("An explicit legacy source checkout and compatibility harness are required")
    candidate = Path(__file__).resolve().parents[1]
    output = tmp_path / "cross-source-evidence"
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
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "compatibility_report.json").read_text())
    assert report["passed"]
    assert report["paid"]["new_provider_calls"] == []
    assert report["completed"]["new_provider_calls"] == []
    assert report["queued"]["legacy_wire"] == report["queued"]["candidate_wire"]
    assert report["queued"]["legacy_request_hashes"] == report["queued"]["candidate_request_hashes"]
    assert report["busy_adoption_code"] == "CONVERSATION_BUSY"
    assert report["fresh_turn_binding"] and report["queued"]["new_turn_binding"]
