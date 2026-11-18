"""Durable offline validation independent of the observer's terminal connection.

A normal exit, timeout or handled signal records a terminal report. SIGKILL,
host loss and storage loss may leave an unknown outcome: --status never invents
an exit code, clears an active marker, or starts a replacement test process.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "careerforge-durable-validation-v1"
OFFLINE = "not production and not live and not sandbox"


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_manifest(root):
    files = {}
    for name in ("src", "tests", "scripts", "alembic", "deploy", "coding-agent"):
        for path in sorted((root / name).rglob("*")):
            if path.is_file() and not any(
                part in {"__pycache__", ".pytest_cache", "node_modules"}
                for part in path.relative_to(root).parts
            ):
                files[str(path.relative_to(root))] = digest(path)
    for name in (
        "pyproject.toml",
        "constraints-python312-linux.txt",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
    ):
        path = root / name
        if path.is_file():
            files[name] = digest(path)
    return files


def process_identity(pid):
    """PID alone is never used to infer a historical process exit."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start_ticks": stat[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
        }
    except OSError:
        return {"pid": pid, "identity_unavailable": True}


def read_events(path):
    events, problems = [], []
    if not path.exists():
        return events, ["controller journal is missing"]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            required = {
                "configure": {"worker": str, "pid": int},
                "deselected": {"nodeids": list},
                "collection": {"nodeids": list},
                "worker_collection": {"worker": str, "nodeids": list},
                "worker_down": {"worker": str},
                "start": {"nodeid": str},
                "report": {"nodeid": str, "when": str, "outcome": str},
                "collection_error": {"nodeid": str},
                "session_finish": {"exit_code": int},
            }
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("kind"), str)
                or event["kind"] not in required
            ):
                raise ValueError("unknown event kind")
            if any(
                type(event.get(key)) is not kind for key, kind in required[event["kind"]].items()
            ):
                raise ValueError("missing or invalid event fields")
            if "nodeids" in event and any(not isinstance(node, str) for node in event["nodeids"]):
                raise ValueError("invalid node list")
            if event["kind"] == "report" and (
                event["when"] not in {"setup", "call", "teardown"}
                or event["outcome"] not in {"passed", "failed", "skipped"}
            ):
                raise ValueError("invalid phase or outcome")
            if event["kind"] == "worker_down" and "error" not in event:
                raise ValueError("missing worker outcome")
            if event.get("seq") != number:
                problems.append(f"journal sequence mismatch at line {number}")
            events.append(event)
        except (ValueError, AttributeError):
            problems.append(f"incomplete or invalid journal line {number}")
    return events, problems


def summarize(evidence, xml_path):
    events, problems = read_events(evidence / "controller.jsonl")
    collections = [
        event["nodeids"]
        for event in events
        if event["kind"] in {"collection", "worker_collection"} and event["nodeids"]
    ]
    selected = collections[0] if collections else []
    if not selected:
        problems.append("no nonempty completed collection is recorded")
    if any(ids != selected for ids in collections[1:]):
        problems.append("worker collections disagree")
    if len(set(selected)) != len(selected):
        problems.append("duplicate selected node IDs")
    reports, starts, finishes = {}, [], []
    for event in events:
        if event["kind"] == "report":
            key = event["nodeid"]
            phases = reports.setdefault(key, {})
            if event["when"] in phases:
                problems.append(f"duplicate {event['when']} report: {key}")
            phases[event["when"]] = event
        elif event["kind"] == "start":
            starts.append(event["nodeid"])
        elif event["kind"] == "session_finish":
            finishes.append(event["exit_code"])
        elif event["kind"] == "collection_error":
            problems.append(f"collection error: {event['nodeid']}")
        elif event["kind"] == "worker_down" and event["error"]:
            problems.append(f"worker failure: {event['worker']}: {event['error']}")
    completed = sorted(key for key, phases in reports.items() if "teardown" in phases)
    missing = sorted(set(selected) - set(completed))
    unexpected = sorted(set(reports) - set(selected))
    if missing:
        problems.append(f"{len(missing)} selected nodes lack a teardown report")
    if unexpected:
        problems.append(f"{len(unexpected)} reported nodes were not selected")
    if len(finishes) != 1:
        problems.append("exactly one pytest session-finish event is required")
    outcomes = Counter()
    for key in completed:
        phases = reports[key]
        if any(phases.get(when, {}).get("outcome") == "failed" for when in ("setup", "teardown")):
            outcomes["error"] += 1
        elif phases.get("call", {}).get("outcome") == "failed":
            outcomes["failed"] += 1
        elif any(phase["outcome"] == "skipped" for phase in phases.values()):
            outcomes["skipped"] += 1
        elif all(
            phases.get(when, {}).get("outcome") == "passed"
            for when in ("setup", "call", "teardown")
        ):
            outcomes["passed"] += 1
        else:
            outcomes["incomplete"] += 1
            problems.append(f"incomplete phases: {key}")
    xml = None
    try:
        tree = ET.parse(xml_path).getroot()
        suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
        xml = {
            key: sum(int(suite.get(key, "0")) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        if not suites or xml["tests"] < len(selected):
            problems.append("JUnit test count is smaller than selected collection")
    except (OSError, ET.ParseError, ValueError) as error:
        problems.append(f"JUnit unavailable or invalid: {type(error).__name__}")
    excluded = set()
    journal_paths = list(evidence.glob("*.jsonl"))
    worker_ids = {event["worker"] for event in events if event["kind"] == "worker_collection"}
    if not worker_ids.issubset({path.stem for path in journal_paths}):
        problems.append("one or more collected worker journals are missing")
    for path in journal_paths:
        worker_events, worker_problems = read_events(path)
        if path.name != "controller.jsonl":
            problems.extend(f"{path.name}: {problem}" for problem in worker_problems)
        if path.stem in worker_ids:
            configured = [event for event in worker_events if event["kind"] == "configure"]
            worker_collections = [
                event["nodeids"] for event in worker_events if event["kind"] == "collection"
            ]
            worker_finished = [
                event for event in worker_events if event["kind"] == "session_finish"
            ]
            if (
                len(configured) != 1
                or configured[0]["worker"] != path.stem
                or worker_collections != [selected]
                or len(worker_finished) != 1
            ):
                problems.append(f"{path.name}: incomplete worker lifecycle or collection mismatch")
        for event in worker_events:
            if event["kind"] == "deselected":
                excluded.update(event["nodeids"])
    return {
        "selected_nodeids": selected,
        "completed_nodeids": completed,
        "unfinished_nodeids": missing,
        "started_nodeids": list(dict.fromkeys(starts)),
        "excluded_nodeids": sorted(excluded),
        "unexpected_nodeids": unexpected,
        "node_outcomes": dict(outcomes),
        "selected_count": len(selected),
        "completed_count": len(completed),
        "excluded_count": len(excluded),
        "pytest_session_exit_code": finishes[-1] if finishes else None,
        "junit": xml,
        "evidence_complete": not problems,
        "problems": problems,
    }


def safe_print(value):
    # The authoritative result is already durable. A closed client pipe is not
    # a product failure and must not change its recorded exit code.
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(fd, sys.stdout.fileno())
            finally:
                os.close(fd)


def inspect(output, label):
    report = json.loads((output / f"{label}_run.json").read_text(encoding="utf-8"))
    report["observed_evidence"] = summarize(output / f"{label}_evidence", output / f"{label}.xml")
    if report["status"] in {"starting", "running", "stopping"}:
        owner = report.get("supervisor")
        same = owner and process_identity(owner["pid"]) == owner
        report["observation"] = "owner_identity_visible" if same else "unfinished_unreconciled"
        report["retry_safe"] = False
    return report


def group_active(pgid):
    """Zombies consume no test resources; unknown visibility is treated as active."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    seen = False
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            stat = path.read_text().rsplit(")", 1)[1].split()
            if int(stat[2]) == pgid:
                seen = True
                if stat[0] != "Z":
                    return True
        except (OSError, ValueError, IndexError):
            continue
    return not seen


def stop_group(process, grace=3.0):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while group_active(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if group_active(process.pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return False
    deadline = time.monotonic() + 2
    while group_active(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not group_active(process.pid)


def supervise(
    command, *, timeout, cwd, env, stdout, lock_fd, report, status_path, options, signals, hangups
):
    """Explicit seam for synthetic contract tests; this owns the whole child group."""
    process = None
    code, reason, cleaned = 125, None, True
    started = time.monotonic()
    try:
        free = shutil.disk_usage(status_path.parent).free
        report["free_bytes_before"] = free
        if free < report["minimum_free_bytes"]:
            raise RuntimeError("insufficient free disk space before pytest start")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lock_fd,),
        )
        cleaned = False
        report.update(status="running", pytest_process=process_identity(process.pid))
        while process.poll() is None:
            elapsed = time.monotonic() - started
            free = shutil.disk_usage(status_path.parent).free
            report.update(
                heartbeat_at=now(),
                elapsed_seconds=round(elapsed, 3),
                free_bytes=free,
                observed_hangups=hangups,
            )
            if signals:
                reason, code = "interrupted", 128 + signals[0]
            elif elapsed >= timeout:
                reason, code = "timed_out", 124
            elif free < report["minimum_free_bytes"]:
                reason, code = "resource_exhausted", 125
            if reason:
                report.update(status="stopping", stop_reason=reason)
                atomic_json(status_path, report)
                cleaned = stop_group(process)
                break
            atomic_json(status_path, report)
            time.sleep(options.heartbeat_seconds)
        if not reason:
            code = process.returncode
            cleaned = stop_group(process) if group_active(process.pid) else True
            if signals:
                reason, code = "interrupted", 128 + signals[0]
        stdout.flush()
        os.fsync(stdout.fileno())
        report["pytest_exit_code"] = process.returncode
    except Exception as error:
        reason, code = "runner_error", 125
        report["runner_error"] = f"{type(error).__name__}: {error}"
        if process is not None:
            cleaned = stop_group(process)
            report["pytest_exit_code"] = process.returncode
    return code, reason, cleaned


def run(options, root):
    output = (root / options.output_dir).resolve()
    if options.status:
        safe_print(inspect(output, options.label))
        return 0
    lock_dir = root / "validation"
    lock_dir.mkdir(exist_ok=True)
    active_path = lock_dir / "active-run.json"
    with (lock_dir / ".offline.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "another validation process owns the project lock; no test was started"
            ) from None
        if active_path.exists():
            raise RuntimeError(
                f"unreconciled active run: {active_path}; inspect it before any retry"
            )
        output.mkdir(parents=True, exist_ok=True)
        paths = [
            output / f"{options.label}{suffix}"
            for suffix in ("_run.json", ".log", ".xml", "_evidence", "_tmp")
        ]
        if any(path.exists() for path in paths):
            raise RuntimeError(
                "refusing to overwrite evidence; choose a new --output-dir or --label"
            )
        status_path, log_path, xml_path, evidence, temp = paths
        evidence.mkdir()
        temp.mkdir()
        before = source_manifest(root)
        atomic_json(evidence / "source_before.json", before)
        env = {
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "scripts")]),
        }
        # Explicit CLI owns the scope; inherited options cannot change it into
        # collect-only, maxfail, or an unrecorded plugin/service configuration.
        for name in (
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "MODEL_NAME",
            "GITHUB_TOKEN",
            "EMBEDDING_API_KEY",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_MODEL",
            "RERANK_API_KEY",
            "RERANK_URL",
            "RERANK_MODEL",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "CAREERFORGE_DATABASE_URL",
            "CAREERFORGE_REDIS_BROKER_URL",
            "CAREERFORGE_REDIS_CACHE_URL",
        ):
            env.pop(name, None)
        env.update(
            CAREERFORGE_MODE="test", CAREERFORGE_TEST_EVIDENCE=str(evidence), PYTHONUNBUFFERED="1"
        )
        command = [
            sys.executable,
            "-u",
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "pytest_evidence",
            "-v",
            "-m",
            OFFLINE,
            "--durations=10",
            f"--junitxml={xml_path}",
            f"--basetemp={temp}",
            "-o",
            "addopts=",
            "-o",
            f"faulthandler_timeout={options.traceback_after}",
        ]
        if options.workers:
            command.extend(["-p", "xdist.plugin", "-n", str(options.workers)])
        if options.coverage:
            command.extend(
                [
                    "-p",
                    "pytest_cov.plugin",
                    "--cov=careerforge",
                    "--cov-report=term-missing",
                    f"--cov-report=json:{output / (options.label + '_coverage.json')}",
                ]
            )
            env["COVERAGE_FILE"] = str(output / "coverage.data")
            if options.append_coverage:
                command.append("--cov-append")
        command.extend(options.tests)
        run_id = str(uuid.uuid4())
        report = {
            "schema": SCHEMA,
            "run_id": run_id,
            "started_at": now(),
            "command": command,
            "timeout_seconds": options.timeout,
            "traceback_after_seconds": options.traceback_after,
            "scope": "offline_contracts_and_fixture_integration",
            "real_provider_calls": 0,
            "status": "starting",
            "exit_code": None,
            "pytest_exit_code": None,
            "supervisor": process_identity(os.getpid()),
            "workers": options.workers,
            "evidence_dir": str(evidence),
            "temporary_dir": str(temp),
            "source_manifest_sha256": digest(evidence / "source_before.json"),
            "minimum_free_bytes": options.min_free_mb * 1024 * 1024,
            "inherited_pytest_options_ignored": bool(
                os.environ.get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_PLUGINS")
            ),
            "limitations": "Host/SIGKILL/storage loss may leave unknown state; no automatic retry.",
        }
        atomic_json(
            evidence / "dependencies.json",
            {
                "python": sys.version,
                "executable": sys.executable,
                "packages": dict(
                    sorted(
                        (dist.metadata["Name"], dist.version)
                        for dist in importlib.metadata.distributions()
                    )
                ),
            },
        )
        atomic_json(
            active_path,
            {"run_id": run_id, "status_path": str(status_path), "supervisor": report["supervisor"]},
        )
        atomic_json(status_path, report)
        requested_signals, hangups = [], []

        def receive(signum, frame):
            if signum == signal.SIGHUP:
                hangups.append(now())
            else:
                requested_signals.append(signum)

        previous = {
            number: signal.signal(number, receive)
            for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        }
        started = time.monotonic()
        try:
            with log_path.open("x", encoding="utf-8") as log:
                code, reason, cleaned = supervise(
                    command,
                    timeout=options.timeout,
                    cwd=root,
                    env=env,
                    stdout=log,
                    lock_fd=lock.fileno(),
                    report=report,
                    status_path=status_path,
                    options=options,
                    signals=requested_signals,
                    hangups=hangups,
                )
            summary = summarize(evidence, xml_path)
            after = source_manifest(root)
            atomic_json(evidence / "source_after.json", after)
            changed = sorted(
                path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
            )
            report.update(
                source_unchanged=not changed,
                changed_source_files=changed,
                process_group_cleanup_confirmed=cleaned,
            )
            if requested_signals and not reason:
                reason, code = "interrupted", 128 + requested_signals[0]
            if reason:
                status = reason
            elif code != 0:
                status = "failed" if code == 1 and summary["evidence_complete"] else "incomplete"
            elif (
                not summary["evidence_complete"]
                or summary["pytest_session_exit_code"] != 0
                or changed
                or not cleaned
            ):
                status, code = "incomplete", 125
            elif (
                summary["junit"]["tests"] != summary["selected_count"]
                or summary["junit"]["failures"]
                or summary["junit"]["errors"]
            ):
                status, code = "incomplete", 125
            else:
                status = "passed"
            atomic_json(evidence / "summary.json", summary)
            report.update(
                status=status,
                exit_code=code,
                finished_at=now(),
                finished_monotonic=time.monotonic(),
                elapsed_seconds=round(time.monotonic() - started, 3),
                observed_hangups=hangups,
                summary=summary,
            )
            # Commit the terminal status LAST. A failure to persist any final
            # evidence must leave the old nonterminal record, never a false
            # "passed" record followed by a failed finalization.
            artifacts = [
                log_path,
                xml_path,
                output / (options.label + "_coverage.json"),
                *evidence.glob("*"),
            ]
            for path in artifacts:
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            atomic_json(
                evidence / "artifact_sha256.json",
                {
                    str(path.relative_to(output)): digest(path)
                    for path in sorted(artifacts)
                    if path.is_file() and path.name != "artifact_sha256.json"
                },
            )
            report["artifacts_manifest_sha256"] = digest(evidence / "artifact_sha256.json")
            atomic_json(status_path, report)
            if cleaned:
                active_path.unlink()
            safe_print(
                {key: value for key, value in report.items() if key != "summary"}
                | {
                    "counts": summary["node_outcomes"],
                    "completed_count": summary["completed_count"],
                    "selected_count": summary["selected_count"],
                    "excluded_count": summary["excluded_count"],
                }
            )
            return code if code >= 0 else 128 - code
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--append-coverage", action="store_true")
    parser.add_argument("--output-dir", default="validation/local")
    parser.add_argument("--label", default="offline")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--traceback-after", type=float, default=0.0)
    parser.add_argument(
        "--workers", type=int, default=0, help="Explicit xdist workers; 0 runs serially"
    )
    parser.add_argument("--heartbeat-seconds", type=float, default=2)
    parser.add_argument("--min-free-mb", type=int, default=512)
    parser.add_argument(
        "--status", action="store_true", help="Read existing evidence only; never restart tests"
    )
    parser.add_argument("tests", nargs="*", help="Optional pytest files / node IDs")
    options = parser.parse_args()
    if options.timeout < 10 or options.min_free_mb < 1 or not 0 <= options.workers <= 8:
        parser.error("require timeout >=10, min-free-mb >=1, and workers in 0..8")
    if not math.isfinite(options.traceback_after) or options.traceback_after < 0:
        parser.error("--traceback-after must be finite and nonnegative")
    if not math.isfinite(options.heartbeat_seconds) or not 0.05 <= options.heartbeat_seconds <= 10:
        parser.error("--heartbeat-seconds must be between 0.05 and 10")
    if not options.label.replace("-", "").replace("_", "").isalnum():
        parser.error("--label must be alphanumeric (hyphens/underscores allowed)")
    if options.append_coverage and not options.coverage:
        parser.error("--append-coverage requires --coverage")
    if any(test.startswith("-") for test in options.tests):
        parser.error("positional arguments must be test paths or node IDs, not pytest options")
    try:
        return run(options, Path(__file__).resolve().parents[1])
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Validation not started or evidence incomplete: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
