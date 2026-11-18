"""Append-only, fsynced pytest evidence; no application behavior is changed."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

_stream = None
_sequence = 0


def emit(kind, **values):
    global _sequence
    if _stream is None:
        return
    _sequence += 1
    event = {"seq": _sequence, "time_ns": time.time_ns(), "kind": kind, **values}
    _stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    _stream.flush()
    os.fsync(_stream.fileno())


def pytest_configure(config):
    global _stream
    directory = os.environ.get("CAREERFORGE_TEST_EVIDENCE")
    if not directory:
        raise pytest.UsageError("pytest_evidence requires a runner-owned evidence directory")
    worker = getattr(config, "workerinput", {}).get("workerid", "controller")
    _stream = (Path(directory) / f"{worker}.jsonl").open("x", encoding="utf-8")
    emit("configure", worker=worker, pid=os.getpid())


def pytest_deselected(items):
    emit("deselected", nodeids=[item.nodeid for item in items])


def pytest_collection_finish(session):
    emit("collection", nodeids=[item.nodeid for item in session.items])


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    emit("worker_collection", worker=node.gateway.id, nodeids=list(ids))


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    emit("worker_down", worker=node.gateway.id, error=str(error) if error else None)


def pytest_runtest_logstart(nodeid, location):
    emit("start", nodeid=nodeid)


def pytest_runtest_logreport(report):
    emit(
        "report",
        nodeid=report.nodeid,
        when=report.when,
        outcome=report.outcome,
        duration=report.duration,
        wasxfail=getattr(report, "wasxfail", None),
    )


def pytest_collectreport(report):
    if report.failed:
        emit("collection_error", nodeid=report.nodeid, outcome=report.outcome)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    emit("session_finish", exit_code=int(exitstatus), tests_collected=session.testscollected)


def pytest_unconfigure(config):
    global _stream
    if _stream is not None:
        _stream.close()
        _stream = None
