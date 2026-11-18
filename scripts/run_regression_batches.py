"""Checkpoint a full offline selection across short, independently observed batches.

plan collects once. Each next invocation starts at most one new batch. It never
reruns an interrupted batch. All batches share one original wall-clock budget;
time between invocations counts. status is read-only. A combined success means
the planned selection completed exactly once in separate pytest sessions, not
that a single-session regression or production/native-model QA was performed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import run_tests as runner


def environment(root, evidence):
    env = {**os.environ}
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
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        CAREERFORGE_MODE="test",
        PYTHONPATH=os.pathsep.join([str(root / "src"), str(root / "scripts")]),
        CAREERFORGE_TEST_EVIDENCE=str(evidence),
    )
    return env


def create_plan(root, output, options):
    output.mkdir(parents=True, exist_ok=False)
    collection = output / "collection"
    collection.mkdir()
    before = runner.source_manifest(root)
    runner.atomic_json(output / "source_manifest.json", before)
    command = [
        sys.executable,
        "-u",
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "pytest_evidence",
        "--collect-only",
        "-q",
        "-m",
        runner.OFFLINE,
        "-o",
        "addopts=",
        "tests",
    ]
    with (output / "collection.log").open("x") as log:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment(root, collection),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    runner.atomic_json(
        output / "collection_result.json", {"exit_code": result.returncode, "command": command}
    )
    events, problems = runner.read_events(collection / "controller.jsonl")
    selected = [event["nodeids"] for event in events if event["kind"] == "collection"]
    excluded = [
        node for event in events if event["kind"] == "deselected" for node in event["nodeids"]
    ]
    finish = [event["exit_code"] for event in events if event["kind"] == "session_finish"]
    if result.returncode or problems or len(selected) != 1 or not selected[0] or finish != [0]:
        raise RuntimeError("collection did not finish cleanly; inspect collection evidence")
    nodes = selected[0]
    if (
        len(set(nodes)) != len(nodes)
        or set(nodes) & set(excluded)
        or before != runner.source_manifest(root)
    ):
        raise RuntimeError("collection/source identity is inconsistent")
    batches = []
    for index, offset in enumerate(range(0, len(nodes), options.batch_size), 1):
        label = f"batch_{index:03d}"
        chosen = nodes[offset : offset + options.batch_size]
        path = output / f"{label}.nodes.txt"
        with path.open("x") as stream:
            stream.write("\n".join(chosen) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        batches.append(
            {
                "label": label,
                "nodeids": chosen,
                "node_file": path.name,
                "node_file_sha256": runner.digest(path),
            }
        )
    plan = {
        "schema": "careerforge-offline-batch-plan-v1",
        "created_at": runner.now(),
        "source_manifest_sha256": runner.digest(output / "source_manifest.json"),
        "selected_nodeids": nodes,
        "excluded_nodeids": excluded,
        "batches": batches,
        "workers": options.workers,
        "total_timeout_seconds": options.timeout,
        "per_batch_timeout_seconds": options.batch_timeout,
        "scope": "offline_selection_in_separate_pytest_sessions",
        "real_provider_calls": 0,
    }
    runner.atomic_json(output / "plan.json", plan)
    runner.atomic_json(
        output / "plan_commit.json", {"plan_sha256": runner.digest(output / "plan.json")}
    )
    return {
        "status": "planned",
        "selected": len(nodes),
        "excluded": len(excluded),
        "batches": len(batches),
    }


def validate_batch(root, output, plan, batch):
    label = batch["label"]
    path = output / label / f"{label}_run.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    if report["status"] not in {"passed", "failed"}:
        raise RuntimeError(
            f"{label} is {report['status']}; no replacement or later batch will be started"
        )
    evidence = path.parent / f"{label}_evidence"
    if runner.digest(evidence / "source_before.json") != plan["source_manifest_sha256"]:
        raise RuntimeError(f"{label} source fingerprint differs from the plan")
    summary = runner.summarize(evidence, path.parent / f"{label}.xml")
    if not summary["evidence_complete"] or summary != report["summary"]:
        raise RuntimeError(f"{label} evidence is incomplete or changed")
    expected_exit = 0 if report["status"] == "passed" else 1
    if (
        report["exit_code"] != expected_exit
        or report["pytest_exit_code"] != expected_exit
        or summary["pytest_session_exit_code"] != expected_exit
        or report["workers"] != plan["workers"]
    ):
        raise RuntimeError(f"{label} exit/worker records are inconsistent")
    if report["status"] == "passed" and (
        summary["node_outcomes"].get("failed", 0)
        or summary["node_outcomes"].get("error", 0)
        or summary["junit"]["failures"]
        or summary["junit"]["errors"]
    ):
        raise RuntimeError(f"{label} claims success despite recorded failures")
    if summary["selected_nodeids"] != batch["nodeids"] or set(summary["completed_nodeids"]) != set(
        batch["nodeids"]
    ):
        raise RuntimeError(f"{label} did not execute its exact planned selection")
    if not report["source_unchanged"] or not report["process_group_cleanup_confirmed"]:
        raise RuntimeError(f"{label} source or resource state is unresolved")
    manifest = evidence / "artifact_sha256.json"
    if runner.digest(manifest) != report["artifacts_manifest_sha256"]:
        raise RuntimeError(f"{label} manifest changed")
    for relative, expected in json.loads(manifest.read_text()).items():
        if runner.digest(path.parent / relative) != expected:
            raise RuntimeError(f"{label} artifact changed: {relative}")
    return report


def observe(root, output, plan):
    plan_hash = runner.digest(output / "plan.json")
    if json.loads((output / "plan_commit.json").read_text())["plan_sha256"] != plan_hash:
        raise RuntimeError("immutable batch plan changed")
    planned = plan["selected_nodeids"]
    if (
        not planned
        or len(planned) != len(set(planned))
        or [node for batch in plan["batches"] for node in batch["nodeids"]] != planned
        or set(planned) & set(plan["excluded_nodeids"])
        or [batch["label"] for batch in plan["batches"]]
        != [f"batch_{index:03d}" for index in range(1, len(plan["batches"]) + 1)]
        or any(batch["node_file"] != batch["label"] + ".nodes.txt" for batch in plan["batches"])
    ):
        raise RuntimeError("batch plan contains missing, duplicate or inconsistent nodes/labels")
    lease_path = output / "deadline.json"
    if lease_path.exists():
        lease = json.loads(lease_path.read_text())
        if (
            lease["plan_sha256"] != plan_hash
            or lease["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ):
            raise RuntimeError("original plan/deadline host identity is unresolved")
        if json.loads((output / "execution_anchor.json").read_text())[
            "deadline_sha256"
        ] != runner.digest(lease_path):
            raise RuntimeError("original deadline changed")
    elif list(output.glob("*.claim.json")) or list(output.glob("batch_*/*_run.json")):
        raise RuntimeError("original deadline is missing after execution began; it cannot be reset")
    reports = []
    for batch in plan["batches"]:
        reports.append(validate_batch(root, output, plan, batch))
    completed = [report for report in reports if report]
    counts = Counter()
    executed = []
    for report in completed:
        counts.update(report["summary"]["node_outcomes"])
        executed.extend(report["summary"]["completed_nodeids"])
    finished = len(completed) == len(reports)
    if finished and Counter(executed) != Counter(plan["selected_nodeids"]):
        raise RuntimeError("full selected/completed node multisets do not match exactly once")
    if finished:
        deadline = json.loads((output / "deadline.json").read_text())["deadline_monotonic"]
        if max(report["finished_monotonic"] for report in completed) > deadline:
            raise RuntimeError("full selection finished outside the original total deadline")
    return {
        "status": (
            "passed" if all(report["status"] == "passed" for report in completed) else "failed"
        )
        if finished
        else "in_progress",
        "scope": plan["scope"],
        "selected_count": len(plan["selected_nodeids"]),
        "excluded_count": len(plan["excluded_nodeids"]),
        "completed_count": len(executed),
        "completed_batches": len(completed),
        "batch_count": len(reports),
        "node_outcomes": dict(counts),
        "all_selected_executed_exactly_once": finished,
        "reports": reports,
    }


def advance(root, output, plan):
    with (output / ".controller.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another batch controller is active") from None
        if runner.digest(output / "source_manifest.json") != plan["source_manifest_sha256"]:
            raise RuntimeError("source manifest changed")
        if runner.source_manifest(root) != json.loads(
            (output / "source_manifest.json").read_text()
        ):
            raise RuntimeError("source changed after planning")
        state = observe(root, output, plan)
        if state["status"] != "in_progress":
            return state
        lease_path = output / "deadline.json"
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if lease_path.exists():
            lease = json.loads(lease_path.read_text())
            if lease["boot_id"] != boot:
                raise RuntimeError("host rebooted; original deadline/process state is unresolved")
        else:
            lease = {
                "started_at": runner.now(),
                "boot_id": boot,
                "deadline_monotonic": time.monotonic() + plan["total_timeout_seconds"],
                "plan_sha256": runner.digest(output / "plan.json"),
            }
            runner.atomic_json(lease_path, lease)
            runner.atomic_json(
                output / "execution_anchor.json", {"deadline_sha256": runner.digest(lease_path)}
            )
        if lease["plan_sha256"] != runner.digest(output / "plan.json"):
            raise RuntimeError("batch plan changed after execution began")
        remaining = math.floor(lease["deadline_monotonic"] - time.monotonic())
        if remaining < 10:
            raise RuntimeError("the original total deadline expired; no new batch started")
        index = next(index for index, report in enumerate(state["reports"]) if report is None)
        batch = plan["batches"][index]
        label = batch["label"]
        node_file = output / batch["node_file"]
        if runner.digest(node_file) != batch["node_file_sha256"]:
            raise RuntimeError("batch node list changed")
        claim = output / f"{label}.claim.json"
        if claim.exists():
            raise RuntimeError(
                f"{label} was claimed but has no confirmed terminal evidence; no retry"
            )
        runner.atomic_json(
            claim,
            {"label": label, "claimed_at": runner.now(), "remaining_total_seconds": remaining},
        )
        command = [
            sys.executable,
            str(root / "scripts" / "run_tests.py"),
            "--output-dir",
            str(output / label),
            "--label",
            label,
            "--workers",
            str(plan["workers"]),
            "--timeout",
            str(min(remaining, plan["per_batch_timeout_seconds"])),
            "@" + str(node_file),
        ]
        with (output / f"{label}.console.log").open("x") as console:
            result = subprocess.run(command, cwd=root, stdout=console, stderr=subprocess.STDOUT)
        runner.atomic_json(
            output / f"{label}.launcher.json",
            {"command": command, "exit_code": result.returncode, "finished_at": runner.now()},
        )
        state = observe(root, output, plan)
        runner.atomic_json(
            output / "aggregate.json",
            {key: value for key, value in state.items() if key != "reports"},
        )
        if state["status"] in {"passed", "failed"}:
            combined = ET.Element("testsuites")
            for part in plan["batches"]:
                tree = ET.parse(output / part["label"] / f"{part['label']}.xml").getroot()
                for suite in tree:
                    combined.append(suite)
            ET.ElementTree(combined).write(
                output / "aggregate.xml", encoding="utf-8", xml_declaration=True
            )
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "next", "status"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--batch-timeout", type=int, default=600)
    parser.add_argument("--batch-size", type=int, default=450)
    options = parser.parse_args()
    if (
        not 1 <= options.workers <= 8
        or options.timeout < 10
        or options.batch_timeout < 10
        or not 1 <= options.batch_size <= 1000
    ):
        parser.error("invalid worker count, timeout or batch size")
    root = Path(__file__).resolve().parents[1]
    output = (root / options.output_dir).resolve()
    try:
        if options.action == "plan":
            state = create_plan(root, output, options)
        else:
            plan = json.loads((output / "plan.json").read_text())
            state = (
                advance(root, output, plan)
                if options.action == "next"
                else observe(root, output, plan)
            )
        runner.safe_print({key: value for key, value in state.items() if key != "reports"})
        return 1 if state["status"] == "failed" else 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Batch execution stopped without retry: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
