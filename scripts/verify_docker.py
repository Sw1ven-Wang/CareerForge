"""Build and test disposable Docker services without touching the production Compose project.

Offline fixtures remain explicitly labelled; infrastructure and sandbox checks use real services.
No host ports, privileged containers, host socket mounts, or real model calls.
Requires an existing usable Docker Linux daemon with cgroup v2 and Compose v2+.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    "compose_configuration",
    "docker_daemon",
    "backend_image_build",
    "sandbox_image_build",
    "offline_tests_in_container",
    "infrastructure",
    "api_worker_cli",
    "sandbox_isolation",
    "cleanup",
)


class CheckFailure(RuntimeError):
    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-docker-test", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation" / "local" / ("docker-" + uuid4().hex[:12]),
    )
    options = parser.parse_args(argv)
    output = options.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "docker.json"
    if target.exists():
        parser.error("refusing to overwrite docker.json; choose a new --output-dir")
    identifier = uuid4().hex[:12]
    project = "cf-validation-" + identifier
    backend, sandbox = project + "-backend:local", project + "-sandbox:local"
    runtime = project + "-runtime:local"
    env = dict(os.environ)
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MODEL_NAME",
        "CAREERFORGE_ROLE_MODELS",
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "RERANK_API_KEY",
        "RERANK_URL",
        "RERANK_MODEL",
        "GITHUB_TOKEN",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        env.pop(key, None)
    env.update(
        CF_VALIDATION_BACKEND_IMAGE=backend,
        CF_VALIDATION_RUNTIME_IMAGE=runtime,
        CF_VALIDATION_DB="careerforge_v15_validation_" + identifier,
        COMPOSE_DISABLE_ENV_FILE="1",
    )
    report = {
        "status": "blocked",
        "scope": "docker_build_infrastructure_and_sandbox_smoke",
        "project": project,
        "started_at": datetime.now(UTC).isoformat(),
        "real_provider_calls_enabled": False,
        "checks": {k: "not_run" for k in CHECKS},
    }
    docker = shutil.which("docker")
    compose = [
        docker or "docker",
        "compose",
        "--env-file",
        os.devnull,
        "-f",
        str(ROOT / "compose.test.yaml"),
        "-p",
        project,
    ]
    stage, resources_created = None, False
    sequence = 0

    def run(command, *, timeout=180, check=True):
        nonlocal sequence
        sequence += 1
        path = output / f"{sequence:02d}-{stage or 'preflight'}.log"
        with path.open("w", encoding="utf-8") as handle:
            handle.write("Command: " + json.dumps(command) + "\n")
            handle.flush()
            completed = subprocess.run(
                command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout
            )
        if check and completed.returncode:
            raise CheckFailure("command_failed")
        return completed

    try:
        if not options.confirm_docker_test:
            report["reason_code"] = "explicit_docker_test_authorization_required"
        elif not docker:
            report["reason_code"] = "docker_cli_missing"
        else:
            stage = "compose_configuration"
            run(compose + ["config", "--quiet"], timeout=30)
            report["checks"][stage] = "passed"
            stage = "docker_daemon"
            probe = run([docker, "info"], timeout=20, check=False)
            if probe.returncode:
                report["checks"][stage] = "blocked"
                report["reason_code"] = "docker_daemon_unavailable"
            else:
                report["checks"][stage] = "passed"
                resources_created = True
                stage = "backend_image_build"
                run(compose + ["build", "api", "validation"], timeout=1200)
                report["checks"][stage] = "passed"
                stage = "sandbox_image_build"
                run(
                    [docker, "build", "--tag", sandbox, "-f", "deploy/Dockerfile.sandbox", "."],
                    timeout=600,
                )
                report["checks"][stage] = "passed"
                stage = "offline_tests_in_container"
                run(compose + ["run", "--rm", "--no-deps", "-T", "validation"], timeout=1200)
                report["checks"][stage] = "passed"
                stage = "infrastructure"
                run(
                    compose
                    + ["up", "-d", "--wait", "--wait-timeout", "120", "postgres", "broker", "cache"]
                )
                name = project + "-infra"
                checked = run(
                    compose
                    + [
                        "run",
                        "--name",
                        name,
                        "--no-deps",
                        "-T",
                        "validation",
                        "python",
                        "scripts/verify_production.py",
                        "--confirm-infrastructure-test",
                        "--output-dir",
                        "/data/validation",
                    ],
                    check=False,
                )
                run([docker, "cp", name + ":/data/validation", str(output / "infrastructure")])
                if checked.returncode:
                    raise CheckFailure("infrastructure_failed")
                native = json.loads((output / "infrastructure" / "production.json").read_text())
                if native["status"] != "passed_infrastructure_only":
                    raise CheckFailure("infrastructure_report_not_passed")
                report["checks"][stage] = "passed"
                stage = "api_worker_cli"
                # The infrastructure probe creates its own owner and public
                # namespace. Exercise CLI initialization on a separate empty DB.
                runtime_db = env["CF_VALIDATION_DB"] + "_api"
                run(
                    compose
                    + ["exec", "-T", "postgres", "createdb", "-U", "careerforge", runtime_db]
                )
                env["CF_VALIDATION_DB"] = runtime_db
                run(
                    compose
                    + ["run", "--rm", "--no-deps", "-T", "api", "alembic", "upgrade", "head"]
                )
                run(compose + ["run", "--rm", "--no-deps", "-T", "api", "careerforge", "init"])
                repeated = run(
                    compose + ["run", "--rm", "--no-deps", "-T", "api", "careerforge", "init"],
                    check=False,
                )
                if repeated.returncode == 0:
                    raise CheckFailure("initialization_did_not_protect_existing_credentials")
                run(
                    compose
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        "api",
                        "worker",
                        "dispatcher",
                    ]
                )
                run(compose + ["exec", "-T", "api", "careerforge", "tasks"])
                run(
                    compose
                    + [
                        "exec",
                        "-T",
                        "worker",
                        "celery",
                        "-A",
                        "careerforge.worker:celery_app",
                        "inspect",
                        "ping",
                        "--timeout",
                        "10",
                    ]
                )
                run(compose + ["ps", "--all"])
                report["checks"][stage] = "passed"
                stage = "sandbox_isolation"
                image = subprocess.run(
                    [docker, "image", "inspect", "--format", "{{.Id}}", sandbox],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if image.returncode:
                    raise CheckFailure("sandbox_digest_unavailable")
                digest = image.stdout.strip()
                report["sandbox_image_digest"] = digest
                run(
                    [
                        sys.executable,
                        "scripts/verify_sandbox.py",
                        "--confirm-docker-test",
                        "--image",
                        digest,
                        "--output-dir",
                        str(output / "sandbox"),
                    ],
                    timeout=120,
                )
                observed = json.loads((output / "sandbox" / "sandbox.json").read_text())
                if (
                    observed.get("status") != "passed_docker_sandbox_smoke_only"
                    or observed.get("image_digest") != digest
                ):
                    raise CheckFailure("sandbox_report_not_passed")
                report["checks"][stage] = "passed"
                report["status"] = "passed_docker_smoke_only"
    except (CheckFailure, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report.update(status="failed", reason_code="check_failed", error_type=type(exc).__name__)
        if stage:
            report["checks"][stage] = "failed"
            report["failed_stage"] = stage
    finally:
        if resources_created:
            stage = "cleanup"
            try:
                # Only the newly generated Compose project and its uniquely tagged images.
                run(compose + ["logs", "--no-color"], timeout=30, check=False)
                run(
                    compose + ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
                    timeout=90,
                )
                run([docker, "image", "rm", backend, runtime, sandbox], timeout=30, check=False)
                report["checks"][stage] = "passed"
            except (OSError, CheckFailure, subprocess.TimeoutExpired):
                report["checks"][stage] = "failed"
                report["status"] = "failed"
                report["reason_code"] = "cleanup_failed_manual_review_required"
        report["finished_at"] = datetime.now(UTC).isoformat()
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed_docker_smoke_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
