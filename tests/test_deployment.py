"""Offline contracts only; these tests do not claim a Docker daemon or container ran."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from careerforge.adapters import sandbox
from careerforge.config import Settings
from careerforge.errors import DomainError

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = load("sandbox_entry_test", "deploy/sandbox_entry.py")
backend = load("backend_entry_test", "deploy/backend_entry.py")
production = load("verify_production_test", "scripts/verify_production.py")


def receipt(**changes):
    return {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "files": {},
        "timed_out": False,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"exit_code": True},
        {"exit_code": "0"},
        {"exit_code": 999},
        {"timed_out": 1},
        {"timed_out": True, "exit_code": 0},
        {"stdout": []},
        {"stdout": "\ud800"},
        {"stderr": "x" * 48_001},
        {"files": []},
        {"files": {"../escape.py": "x"}},
        {"files": {"ok.py": 1}},
        {"files": {".env": "private"}},
        {"files": {"ok.py": "x" * 100_001}},
    ],
)
def test_receipt_rejects_malformed_or_unsafe_fields(changes):
    with pytest.raises(DomainError):
        sandbox.validate_receipt(receipt(**changes))


def test_receipt_accepts_observed_nonzero_exit_and_deadline():
    assert sandbox.validate_receipt(receipt(exit_code=7))["exit_code"] == 7
    assert sandbox.validate_receipt(receipt(exit_code=124, timed_out=True))["timed_out"] is True


def test_bounded_subprocess_drains_both_streams():
    result = sandbox._run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'ok\\n'); sys.stderr.buffer.write(b'err\\n')",
        ],
        timeout=5,
        limit=128,
    )
    assert result.returncode == 0 and result.stdout == b"ok\n" and result.stderr == b"err\n"


def test_bounded_subprocess_rejects_oversize_output():
    with pytest.raises(DomainError) as caught:
        sandbox._run_bounded([sys.executable, "-c", "print('x'*100000)"], timeout=5, limit=100)
    assert caught.value.code == "SANDBOX_OUTPUT"


def test_bounded_subprocess_times_out_and_reaps():
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1, limit=100
        )


def daemon_info(**changes):
    return {
        "OSType": "linux",
        "MemoryLimit": True,
        "PidsLimit": True,
        "CpuCfsQuota": True,
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        **changes,
    }


def fake_docker(monkeypatch, *, info=None, run_error=None, cleanup_error=None):
    """Explicit fake Docker transport for CLI/contract testing, never real execution."""
    calls = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/fake/docker")

    def fake(command, **kwargs):
        calls.append(command)
        if command[1] == "info":
            return subprocess.CompletedProcess(
                command, 0, json.dumps(info or daemon_info()).encode(), b""
            )
        if command[1] == "rm":
            if cleanup_error:
                raise cleanup_error
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if run_error:
            raise run_error
        mount = command[command.index("--mount") + 1]
        path = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        assert path.exists() and path.stat().st_mode & 0o777 == 0o444
        assert json.loads(path.read_text())["argv"] == ["python", "-c", "print(1)"]
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt()).encode(), b"")

    monkeypatch.setattr(sandbox, "_run_bounded", fake)
    return calls


def execution():
    return {"image_digest": "sha256:" + "1" * 64, "files": {}}


def test_docker_command_preserves_security_and_cleans_up(monkeypatch):
    calls = fake_docker(monkeypatch)
    result = sandbox.DockerSandbox().execute(execution(), ["python", "-c", "print(1)"])
    command = calls[1]
    for flag, value in [
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--pids-limit", "64"),
        ("--memory", "256m"),
        ("--cpus", "1"),
        ("--pull", "never"),
        ("--security-opt", "no-new-privileges=true"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert "--read-only" in command and "--privileged" not in command
    assert {command[i + 1] for i, value in enumerate(command) if value == "--cap-add"} == {
        "SETUID",
        "SETGID",
        "KILL",
        "DAC_READ_SEARCH",
    }
    assert calls[-1][1:3] == ["rm", "-f"]
    assert result["synthetic"] is False  # Adapter flag only, transport above is explicitly a fake.
    assert result["image_digest"] == execution()["image_digest"]


@pytest.mark.parametrize(
    "change",
    [
        {"MemoryLimit": False},
        {"PidsLimit": False},
        {"CpuCfsQuota": False},
        {"OSType": "windows"},
        {"SecurityOptions": []},
        {"SecurityOptions": ["name=seccomp,profile=unconfined"]},
        {"SecurityOptions": ["prefixname=seccomp,profile=builtin"]},
    ],
)
def test_docker_rejects_unsupported_isolation_without_running_job(monkeypatch, change):
    calls = fake_docker(monkeypatch, info=daemon_info(**change))
    with pytest.raises(DomainError) as caught:
        sandbox.DockerSandbox().execute(execution(), ["python", "-c", "print(1)"])
    assert caught.value.code == "SANDBOX_ISOLATION"
    assert len(calls) == 1


def test_cleanup_error_does_not_mask_original_timeout(monkeypatch):
    fake_docker(
        monkeypatch,
        run_error=subprocess.TimeoutExpired("docker", 45),
        cleanup_error=OSError("cleanup"),
    )
    with pytest.raises(DomainError) as caught:
        sandbox.DockerSandbox().execute(execution(), ["python", "-c", "print(1)"])
    assert caught.value.code == "SANDBOX_TIMEOUT"


def test_cleanup_error_prevents_success(monkeypatch):
    fake_docker(monkeypatch, cleanup_error=OSError("cleanup"))
    with pytest.raises(DomainError) as caught:
        sandbox.DockerSandbox().execute(execution(), ["python", "-c", "print(1)"])
    assert caught.value.code == "SANDBOX_CLEANUP"


@pytest.mark.parametrize(
    "name", ["../x", "/x", "a//b", "a/./b", ".env", "dir/.ssh/key", "x\\y", "x:y", "x\x00y"]
)
def test_runner_paths_are_defensive(name):
    with pytest.raises(ValueError):
        entry.validate_job({"files": {name: "hi"}, "argv": ["python"]})


def test_runner_rejects_file_directory_conflict():
    with pytest.raises(ValueError):
        entry.validate_job({"files": {"a": "x", "a/b.py": "y"}, "argv": ["python"]})


def test_runner_refuses_host_execution_before_any_job_read(monkeypatch):
    monkeypatch.setattr(entry.os, "getpid", lambda: 99)
    with pytest.raises(RuntimeError):
        entry.main()
    with pytest.raises(RuntimeError):
        entry.kill_children()


def test_runner_exports_only_bounded_safe_text(tmp_path):
    (tmp_path / "valid.txt").write_text("hello")
    (tmp_path / ".env").write_text("private")
    (tmp_path / "nul.txt").write_bytes(b"a\x00b")
    (tmp_path / "long.txt").write_text("x" * 25_001)
    (tmp_path / "symlink").symlink_to(tmp_path / "valid.txt")
    (tmp_path / "blocked-dir").symlink_to(tmp_path, target_is_directory=True)
    assert entry.collect_files(tmp_path) == {"valid.txt": "hello"}


def test_backend_entrypoint_percent_encodes_credentials_and_leaves_direct_urls():
    from sqlalchemy.engine import make_url

    env = {
        "CAREERFORGE_DATABASE_HOST": "postgres",
        "CAREERFORGE_DATABASE_PASSWORD": "p@ss:/?#%",
        "CAREERFORGE_DATABASE_USER": "user@name",
        "CAREERFORGE_DATABASE_NAME": "db",
    }
    backend.configure_database(env)
    parsed = make_url(env["CAREERFORGE_DATABASE_URL"])
    assert parsed.password == "p@ss:/?#%" and parsed.username == "user@name"
    assert "CAREERFORGE_DATABASE_PASSWORD" not in env
    direct = {"CAREERFORGE_DATABASE_URL": "existing-url"}
    backend.configure_database(direct)
    assert direct == {"CAREERFORGE_DATABASE_URL": "existing-url"}


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///test.db",
        "postgresql://u:p@host/production",
        "postgresql://u:p@host/careerforge_v15_validation_",
        "postgresql://u:p@host/careerforge_v15_validation_x/other",
    ],
)
def test_production_checker_refuses_non_dedicated_database(url):
    with pytest.raises(production.Blocked):
        production.validate_targets(Settings(mode="production", database_url=url))


def test_production_checker_scrubs_provider_environment(monkeypatch):
    for name in production.PROVIDER_ENV:
        monkeypatch.setenv(name, "do-not-inherit")
    env = production.safe_environment()
    assert env["CAREERFORGE_ROLE_MODELS"] == "{}"
    assert all(
        name not in env for name in production.PROVIDER_ENV if name != "CAREERFORGE_ROLE_MODELS"
    )
    assert "secret" not in production.redact_log(
        "redis://host secret password", ["secret", "password"]
    )


def test_production_checker_requires_authorization_and_preserves_reports(tmp_path):
    assert production.main(["--output-dir", str(tmp_path)]) == 2
    result = json.loads((tmp_path / "production.json").read_text())
    assert result["status"] == "blocked" and set(result["checks"].values()) == {"not_run"}
    original = (tmp_path / "production.json").read_bytes()
    with pytest.raises(SystemExit):
        production.main(["--output-dir", str(tmp_path)])
    assert (tmp_path / "production.json").read_bytes() == original


def test_invalid_security_options_fail_closed(monkeypatch):
    calls = fake_docker(monkeypatch, info=daemon_info(SecurityOptions=None))
    with pytest.raises(DomainError) as caught:
        sandbox.DockerSandbox().execute(execution(), ["python", "-c", "print(1)"])
    assert caught.value.code == "SANDBOX_ISOLATION" and len(calls) == 1


def test_docker_harness_blocked_daemon_never_builds(monkeypatch, tmp_path):
    harness = load("verify_docker_blocked_test", "scripts/verify_docker.py")
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/fake/docker")
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[1] == "info" else 0)

    monkeypatch.setattr(harness.subprocess, "run", fake)
    assert harness.main(["--confirm-docker-test", "--output-dir", str(tmp_path)]) == 2
    result = json.loads((tmp_path / "docker.json").read_text())
    assert result["status"] == "blocked"
    assert result["checks"]["backend_image_build"] == "not_run"
    assert len(calls) == 2 and all("build" not in c for c in calls)


def test_docker_harness_requires_consent_before_any_process(monkeypatch, tmp_path):
    harness = load("verify_docker_consent_test", "scripts/verify_docker.py")
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *a, **k: pytest.fail("unauthorized process")
    )
    assert harness.main(["--output-dir", str(tmp_path)]) == 2
    report = json.loads((tmp_path / "docker.json").read_text())
    assert report["reason_code"] == "explicit_docker_test_authorization_required"


def test_docker_harness_cleans_only_unique_project_on_build_failure(monkeypatch, tmp_path):
    harness = load("verify_docker_failure_test", "scripts/verify_docker.py")
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/fake/docker")
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "build" in command else 0)

    monkeypatch.setattr(harness.subprocess, "run", fake)
    assert harness.main(["--confirm-docker-test", "--output-dir", str(tmp_path)]) == 2
    report = json.loads((tmp_path / "docker.json").read_text())
    assert report["checks"]["backend_image_build"] == "failed"
    assert report["checks"]["cleanup"] == "passed"
    assert report["checks"]["sandbox_isolation"] == "not_run"
    cleaned = next(c for c in calls if "down" in c)
    assert cleaned[cleaned.index("-p") + 1] == report["project"]
    assert report["project"].startswith("cf-validation-")
    assert all("prune" not in c for c in calls)


def test_docker_harness_contract_success_is_explicitly_fake(monkeypatch, tmp_path):
    harness = load("verify_docker_contract_test", "scripts/verify_docker.py")
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/fake/docker")
    calls = []
    digest = "sha256:" + "2" * 64
    init_calls = 0

    def fake(command, **kwargs):
        nonlocal init_calls
        calls.append(command)
        if command[-2:] == ["careerforge", "init"]:
            init_calls += 1
            return subprocess.CompletedProcess(command, 0 if init_calls == 1 else 2)
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, digest + "\n", "")
        if command[1] == "cp":
            directory = tmp_path / "infrastructure"
            directory.mkdir()
            (directory / "production.json").write_text(
                json.dumps({"status": "passed_infrastructure_only"})
            )
        if "scripts/verify_sandbox.py" in command:
            directory = tmp_path / "sandbox"
            directory.mkdir()
            (directory / "sandbox.json").write_text(
                json.dumps({"status": "passed_docker_sandbox_smoke_only", "image_digest": digest})
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(harness.subprocess, "run", fake)
    assert harness.main(["--confirm-docker-test", "--output-dir", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "docker.json").read_text())
    assert set(report["checks"].values()) == {"passed"}
    assert "--env-file" in calls[0]  # The real harness never loads production .env implicitly.
    assert any("down" in c for c in calls)


@pytest.mark.parametrize("missing", ["stdout", "stderr"])
def test_bounded_subprocess_missing_pipe_fails_closed_and_reaps(monkeypatch, missing):
    from io import BytesIO

    class BrokenPipeProcess:
        def __init__(self):
            self.stdout = BytesIO()
            self.stderr = BytesIO()
            setattr(self, missing, None)
            self.returncode = None
            self.waited = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self):
            self.waited = True
            return self.returncode

    process = BrokenPipeProcess()
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(DomainError) as caught:
        sandbox._run_bounded(["synthetic-child"], timeout=1, limit=100)
    assert caught.value.code == "SANDBOX_UNAVAILABLE"
    assert process.returncode == -9 and process.waited
    remaining = process.stderr if missing == "stdout" else process.stdout
    assert remaining.closed


@pytest.mark.parametrize("delay", [None, "5"])
def test_validation_runner_traceback_timer_is_opt_in_and_hard_timeout_remains(
    monkeypatch, tmp_path, delay
):
    runner = load("offline_runner_traceback_test", "scripts/run_tests.py")
    calls = []

    def synthetic_pytest(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write("synthetic subprocess contract check\n")
        evidence = Path(kwargs["env"]["CAREERFORGE_TEST_EVIDENCE"])
        nodeid = "synthetic.py::test_runner_contract"
        events = [
            {"kind": "collection", "nodeids": [nodeid]},
            *[
                {"kind": "report", "nodeid": nodeid, "when": phase, "outcome": "passed"}
                for phase in ("setup", "call", "teardown")
            ],
            {"kind": "session_finish", "exit_code": 0},
        ]
        (evidence / "controller.jsonl").write_text(
            "\n".join(json.dumps({"seq": index, **event}) for index, event in enumerate(events, 1))
            + "\n"
        )
        (tmp_path / "offline.xml").write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0" /></testsuites>'
        )
        kwargs["report"]["pytest_exit_code"] = 0
        return 0, None, True

    # Stub the new supervisor seam, never a stale subprocess.run seam that
    # could accidentally launch a nested full regression. Keep this synthetic
    # project outside the real runner's project-wide execution lock.
    synthetic_root = tmp_path / "synthetic_root"
    synthetic_root.mkdir()
    monkeypatch.setattr(runner, "__file__", str(synthetic_root / "scripts" / "run_tests.py"))
    monkeypatch.setattr(runner, "supervise", synthetic_pytest)
    argv = ["run_tests.py", "--output-dir", str(tmp_path), "--timeout", "17"]
    if delay is not None:
        argv += ["--traceback-after", delay]
    monkeypatch.setattr(sys, "argv", argv)
    assert runner.main() == 0
    command, options = calls[0]
    expected = float(delay) if delay is not None else 0.0
    assert f"faulthandler_timeout={expected}" in command
    assert options["timeout"] == 17
    report = json.loads((tmp_path / "offline_run.json").read_text())
    assert report["traceback_after_seconds"] == expected
    assert report["timeout_seconds"] == 17


@pytest.mark.parametrize("delay", ["-1", "nan", "inf"])
def test_validation_runner_rejects_invalid_traceback_delay(monkeypatch, delay):
    runner = load("offline_runner_invalid_traceback_test", "scripts/run_tests.py")
    monkeypatch.setattr(sys, "argv", ["run_tests.py", "--traceback-after", delay])
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 2
