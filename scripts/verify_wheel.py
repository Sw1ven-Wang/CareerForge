"""Build-artifact smoke test outside the checkout, without downloading dependencies."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
wheel = root / "dist" / f"careerforge-{version}-py3-none-any.whl"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", type=Path, default=root / "validation" / "local")
options = parser.parse_args()
output = options.output_dir.resolve()
output.mkdir(parents=True, exist_ok=True)
if (output / "wheel_install.json").exists() or (output / "wheel_install.log").exists():
    parser.error("refusing to overwrite wheel evidence; choose a new --output-dir")
if not wheel.is_file():
    raise SystemExit("Build the wheel before running this check.")
report = {
    "scope": "wheel_install_with_existing_host_dependencies",
    "network_calls": 0,
    "status": "failed",
    "checks": {},
}
log_path = output / "wheel_install.log"
with tempfile.TemporaryDirectory(prefix="cf-wheel-check-") as tmp, log_path.open("w") as log:
    tmp = Path(tmp)
    site, state = tmp / "site", tmp / "state"
    env = {
        **os.environ,
        "PYTHONPATH": str(site),
        "CAREERFORGE_STATE_DIR": str(state),
        "NO_COLOR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    commands = [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(site),
            str(wheel),
        ],
        [sys.executable, "-m", "careerforge.cli.main", "--help"],
        [sys.executable, "-m", "careerforge.cli.main", "init", "--development"],
    ]
    for index, command in enumerate(commands):
        result = subprocess.run(
            command, cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=30
        )
        report["checks"][["install", "cli_help", "sqlite_initialization"][index]] = (
            result.returncode
        )
        if result.returncode:
            break
    else:
        database = sqlite3.connect(state / "development.db")
        report["checks"]["table_count"] = database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        report["checks"]["seed_topic_count"] = database.execute(
            "SELECT COUNT(*) FROM technology_topics"
        ).fetchone()[0]
        report["checks"]["publication_count"] = database.execute(
            "SELECT COUNT(*) FROM publications"
        ).fetchone()[0]
        database.close()
        report["checks"]["private_client_file_created"] = (state / "client.json").is_file()
        assert report["checks"]["publication_count"] == 0
        assert report["checks"]["seed_topic_count"] >= 25
        assert report["checks"]["private_client_file_created"]
        report["status"] = "passed"
(output / "wheel_install.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
raise SystemExit(0 if report["status"] == "passed" else 1)
