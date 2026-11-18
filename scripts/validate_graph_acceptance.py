"""Run M1-M6 graph engineering checks in one pytest session with source identity."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    prefixes = (
        "test_langgraph_",
        "test_session_graph",
        "test_session_controls_",
        "test_learning_graph_",
        "test_learning_tutor_graph_",
        "test_practice_",
        "test_assessment_graph_",
        "test_evolution_graph_",
        "test_evolution_commands_m6",
        "test_session_claim_",
    )
    tests = sorted(p for p in (ROOT / "tests").glob("test_*.py") if p.name.startswith(prefixes))
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
    }
    (args.output / "source.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-m",
        "not production and not live and not sandbox",
        *map(str, tests),
        "--junitxml=" + str((args.output / "junit.xml").resolve()),
    ]
    with (args.output / "pytest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    (args.output / "result.json").write_text(
        json.dumps(
            {
                "command": command,
                "exit_code": result.returncode,
                "postgres_requested": False,
                "real_model_quality": "NOT_RUN",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
