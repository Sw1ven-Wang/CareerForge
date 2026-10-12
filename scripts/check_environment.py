"""Read-only environment inventory; never print environment values or credentials."""

import importlib.metadata
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def version(command):
    if shutil.which(command[0]) is None:
        return {"status": "missing"}
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
        # Only explicitly safe version commands; no generic env/config dumps.
        return {
            "status": "available" if result.returncode == 0 else "unavailable",
            "version": result.stdout.strip() if result.returncode == 0 else None,
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}


def main():
    packages = {}
    for name in (
        "pydantic",
        "httpx",
        "fastapi",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "redis",
        "celery",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "packages": packages,
        "node": version(["node", "--version"]),
        "uv": version(["uv", "--version"]),
        "docker": version(["docker", "version", "--format", "{{.Server.Version}}"]),
        "real_model_calls": 0,
        "worker_runtime": "Linux container (required in S02)",
    }
    destination = Path(__file__).resolve().parents[1] / "docs/progress/S00_ENVIRONMENT.json"
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
