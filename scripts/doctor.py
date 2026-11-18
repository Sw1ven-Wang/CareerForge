"""Print capabilities without credentials. Production omissions return exit code 2."""

import argparse
import json
import os
import platform
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--output", default="validation/current/environment.json")
p.add_argument("--production", action="store_true")
args = p.parse_args()
packages = [
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "alembic",
    "httpx",
    "typer",
    "rich",
    "uvicorn",
    "pytest",
    "pytest-asyncio",
    "psycopg",
    "redis",
    "celery",
    "pgvector",
    "tree-sitter",
    "opentelemetry-sdk",
    "prometheus-client",
    "ruff",
    "mypy",
]
versions = {}
for package in packages:
    try:
        versions[package] = version(package)
    except PackageNotFoundError:
        versions[package] = None
binaries = {
    x: shutil.which(x) is not None for x in ["docker", "node", "git", "psql", "redis-server"]
}
node = None
if binaries["node"]:
    node = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, timeout=5
    ).stdout.strip()
production_packages = ["psycopg", "redis", "celery", "pgvector", "tree-sitter"]
blocked = ["package:" + x for x in production_packages if not versions[x]]
if not binaries["docker"]:
    blocked.append("docker_command_sandbox")
for env in [
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "MODEL_NAME",
    "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
]:
    if not bool(os.getenv(env)):
        blocked.append("configuration:" + env)
if node:
    nums = tuple(int(n) for n in node.lstrip("v").split(".")[:3])
    if nums < (22, 19, 0):
        blocked.append("pi_node_version_requires_22.19_or_later")
else:
    blocked.append("pi_node_missing")
report = {
    "python": platform.python_version(),
    "platform": platform.system(),
    "packages": versions,
    "binaries": binaries,
    "node_version": node,
    "production_blockers": blocked,
    "secrets_printed": False,
    "network_or_provider_calls_performed": False,
    "production_acceptance": "not_validated",
}
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(2 if args.production and blocked else 0)
