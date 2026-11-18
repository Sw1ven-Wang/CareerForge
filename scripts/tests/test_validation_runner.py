"""Real-process fault controls for the runner, separate from product regression."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    for name in ("run_tests.py", "pytest_evidence.py", "run_regression_batches.py"):
        shutil.copy2(SCRIPTS / name, tmp_path / "scripts" / name)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\nasyncio_mode="auto"\n'
        'asyncio_default_fixture_loop_scope="function"\n'
        'markers=["production: excluded", "live: excluded", "sandbox: excluded"]\n'
    )
    return tmp_path


def command(project, *args):
    return [
        sys.executable,
        str(project / "scripts" / "run_tests.py"),
        "--output-dir",
        "evidence",
        "--timeout",
        "15",
        "--heartbeat-seconds",
        "0.05",
        *args,
    ]


def result(project):
    return json.loads((project / "evidence" / "offline_run.json").read_text())


def write_test(project, content):
    (project / "tests" / "test_control.py").write_text(content)


def run(project, *args, env=None):
    return subprocess.run(
        command(project, *args), capture_output=True, text=True, timeout=30, env=env
    )


def wait_running(project, process):
    marker = project / "test_started"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            pytest.fail(f"runner exited before control started: {process.communicate()}")
        time.sleep(0.03)
    pytest.fail("control did not start")


SLEEP = (
    "from pathlib import Path\nimport time\n"
    "def test_sleep():\n    Path('test_started').write_text('yes')\n    time.sleep(2)\n"
)


@pytest.mark.parametrize(
    "body,status,code,outcome",
    [
        ("def test_ok():\n    assert True\n", "passed", 0, "passed"),
        ("def test_no():\n    assert False\n", "failed", 1, "failed"),
        ("import pytest\ndef test_skip():\n    pytest.skip('control')\n", "passed", 0, "skipped"),
        (
            "import pytest\n@pytest.fixture\ndef broken():\n    raise ValueError('setup')\n"
            "def test_error(broken):\n    pass\n",
            "failed",
            1,
            "error",
        ),
        (
            "import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise ValueError('teardown')\n"
            "def test_error(broken):\n    pass\n",
            "failed",
            1,
            "error",
        ),
    ],
)
def test_real_outcomes(project, body, status, code, outcome):
    write_test(project, body)
    process = run(project)
    report = result(project)
    assert process.returncode == code, process.stderr
    assert report["status"] == status
    assert report["exit_code"] == report["pytest_exit_code"] == code
    assert report["summary"]["node_outcomes"] == {outcome: 1}
    assert report["summary"]["evidence_complete"]
    assert report["source_unchanged"]
    assert not (project / "validation" / "active-run.json").exists()


def test_parallel_selection_and_exclusions(project):
    write_test(
        project,
        "import pytest\n@pytest.mark.parametrize('n',range(8))\ndef test_ok(n):\n    assert n>=0\n"
        "@pytest.mark.production\ndef test_excluded():\n    assert False\n",
    )
    process = run(project, "--workers", "4")
    report = result(project)
    assert process.returncode == 0, process.stdout + process.stderr
    assert report["summary"]["selected_count"] == report["summary"]["completed_count"] == 8
    assert report["summary"]["excluded_count"] == 1


@pytest.mark.parametrize("option", ["--collect-only", "--help", "--maxfail=1"])
def test_ambient_options_cannot_silently_change_execution(project, option):
    write_test(project, "def test_ok():\n    assert True\n")
    process = run(project, env={**os.environ, "PYTEST_ADDOPTS": option})
    report = result(project)
    assert process.returncode == 0
    assert report["inherited_pytest_options_ignored"]
    assert report["summary"]["completed_count"] == 1


@pytest.mark.parametrize(
    "content",
    ["this is not python !!!", "", "import os\ndef test_exit():\n    os._exit(0)\n"],
)
def test_no_false_pass_without_execution_evidence(project, content):
    write_test(project, content)
    process = run(project)
    assert process.returncode != 0
    assert result(project)["status"] != "passed"


def test_source_mutation_fails_success_gate(project):
    write_test(
        project,
        "from pathlib import Path\ndef test_modify():\n    Path('src/changed.py').write_text('x=1')\n",
    )
    process = run(project)
    report = result(project)
    assert process.returncode == 125
    assert report["pytest_exit_code"] == 0
    assert report["status"] == "incomplete"
    assert report["changed_source_files"] == ["src/changed.py"]


def test_insufficient_disk_refuses_start(project):
    write_test(project, "def test_never():\n    assert False\n")
    process = run(project, "--min-free-mb", "1000000000000")
    report = result(project)
    assert process.returncode == 125
    assert report["pytest_exit_code"] is None
    assert "pytest_process" not in report


def test_no_evidence_overwrite(project):
    write_test(project, "def test_ok():\n    assert True\n")
    assert run(project).returncode == 0
    before = (project / "evidence" / "offline_run.json").read_bytes()
    assert run(project).returncode == 125
    assert (project / "evidence" / "offline_run.json").read_bytes() == before


def test_sighup_and_closed_observer_do_not_abandon_run(project):
    write_test(project, SLEEP)
    process = subprocess.Popen(command(project), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        wait_running(project, process)
        process.stdout.close()
        process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=15) == 0
        report = result(project)
        assert report["status"] == "passed"
        assert report["observed_hangups"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_handled_signal_has_terminal_evidence(project, sig):
    write_test(project, SLEEP)
    process = subprocess.Popen(
        command(project), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_running(project, process)
        process.send_signal(sig)
        assert process.wait(timeout=10) == 128 + sig
        report = result(project)
        assert report["status"] == "interrupted"
        assert report["pytest_exit_code"] is not None
        assert report["process_group_cleanup_confirmed"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_timeout_kills_group_and_keeps_partial_progress(project):
    write_test(project, SLEEP.replace("time.sleep(2)", "time.sleep(60)"))
    process = run(project, "--timeout", "10")
    report = result(project)
    assert process.returncode == report["exit_code"] == 124
    assert report["status"] == "timed_out"
    assert report["pytest_exit_code"] is not None
    assert report["summary"]["selected_count"] == 1
    assert report["summary"]["unfinished_nodeids"]
    assert report["process_group_cleanup_confirmed"]


def test_concurrent_run_is_rejected(project):
    write_test(project, SLEEP)
    process = subprocess.Popen(
        command(project), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_running(project, process)
        second = run(project, "--label", "second")
        assert second.returncode == 125
        assert "owns the project lock" in second.stderr
        assert not (project / "evidence" / "second_run.json").exists()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_unreconciled_marker_cannot_be_bypassed_by_a_new_label(project):
    (project / "validation").mkdir()
    (project / "validation" / "active-run.json").write_text('{"run_id":"unknown"}')
    process = run(project, "--label", "replacement")
    assert process.returncode == 125
    assert "unreconciled" in process.stderr
    assert not (project / "evidence").exists()


@pytest.mark.parametrize(
    "line", ['{"seq":1}', '{"seq":1,"kind":"report"}', '{"seq":1,', '{"seq":1,"kind":[]}']
)
def test_corrupt_journal_has_diagnostic_not_success(project, line):
    spec = importlib.util.spec_from_file_location("runner_under_test", SCRIPTS / "run_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = project / "evidence"
    directory.mkdir()
    (directory / "controller.jsonl").write_text(line + "\n")
    summary = module.summarize(directory, directory / "missing.xml")
    assert not summary["evidence_complete"]
    assert any("invalid journal" in problem for problem in summary["problems"])


def test_truncated_worker_journal_cannot_report_success(project):
    write_test(
        project,
        "import pytest\ndef test_ok():\n    assert True\n"
        "@pytest.mark.production\ndef test_excluded():\n    assert False\n",
    )
    assert run(project, "--workers", "2").returncode == 0
    journal = project / "evidence" / "offline_evidence" / "gw0.jsonl"
    journal.write_text("")
    status = run(project, "--status")
    report = json.loads(status.stdout)
    assert not report["observed_evidence"]["evidence_complete"]
    assert any(
        "incomplete worker lifecycle" in item for item in report["observed_evidence"]["problems"]
    )


def batch(project, action):
    return subprocess.run(
        [
            sys.executable,
            str(project / "scripts" / "run_regression_batches.py"),
            action,
            "--output-dir",
            "batches",
            "--workers",
            "2",
            "--batch-size",
            "2",
            "--timeout",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def plan_small_batch(project):
    write_test(
        project,
        "import pytest\n@pytest.mark.parametrize('n',range(5))\ndef test_ok(n):\n    assert n>=0\n"
        "@pytest.mark.production\ndef test_excluded():\n    assert False\n",
    )
    process = batch(project, "plan")
    assert process.returncode == 0, process.stderr
    return project / "batches"


def test_batches_cover_exact_collection_once(project):
    output = plan_small_batch(project)
    for expected in (2, 4, 5):
        process = batch(project, "next")
        assert process.returncode == 0, process.stderr
        state = json.loads(process.stdout)
        assert state["completed_count"] == expected
    assert state["status"] == "passed"
    assert state["selected_count"] == 5
    assert state["excluded_count"] == 1
    assert state["all_selected_executed_exactly_once"]
    before = (output / "aggregate.json").read_bytes()
    assert batch(project, "next").returncode == 0
    assert (output / "aggregate.json").read_bytes() == before


def test_claimed_batch_without_deadline_is_not_retried(project):
    output = plan_small_batch(project)
    (output / "batch_001.claim.json").write_text("{}")
    process = batch(project, "next")
    assert process.returncode == 125
    assert "cannot be reset" in process.stderr
    assert not (output / "batch_001").exists()


def test_batch_source_mutation_blocks_execution(project):
    output = plan_small_batch(project)
    (project / "src" / "new.py").write_text("x=1")
    process = batch(project, "next")
    assert process.returncode == 125
    assert "source changed" in process.stderr
    assert not (output / "batch_001").exists()


def test_truncated_plan_cannot_shrink_original_full_selection(project):
    output = plan_small_batch(project)
    assert batch(project, "next").returncode == 0
    path = output / "plan.json"
    plan = json.loads(path.read_text())
    plan["batches"] = plan["batches"][:1]
    plan["selected_nodeids"] = plan["batches"][0]["nodeids"]
    path.write_text(json.dumps(plan))
    for action in ("status", "next"):
        process = batch(project, action)
        assert process.returncode == 125
        assert "immutable batch plan changed" in process.stderr


@pytest.mark.parametrize("mutation", ["delete", "extend"])
def test_global_deadline_cannot_reset_between_batches(project, mutation):
    output = plan_small_batch(project)
    assert batch(project, "next").returncode == 0
    path = output / "deadline.json"
    if mutation == "delete":
        path.unlink()
    else:
        deadline = json.loads(path.read_text())
        deadline["deadline_monotonic"] += 2400
        path.write_text(json.dumps(deadline))
    process = batch(project, "next")
    assert process.returncode == 125
    assert "deadline" in process.stderr
    assert not (output / "batch_002").exists()


def test_completed_batch_corruption_blocks_later_batches(project):
    output = plan_small_batch(project)
    assert batch(project, "next").returncode == 0
    (output / "batch_001" / "batch_001.log").write_text("tampered")
    process = batch(project, "next")
    assert process.returncode == 125
    assert "artifact changed" in process.stderr
    assert not (output / "batch_002").exists()
