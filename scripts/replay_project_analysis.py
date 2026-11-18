#!/usr/bin/env python3
"""Offline C05 read-projection replay on a disposable consistent SQLite backup.

Never opens the source database, changes its manifests/receipts, or invokes a
provider. Copy WAL/SHM sidecars before SQLite opens the copy. Run with PYTHONPATH=src.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import func, select

from careerforge.config import Settings
from careerforge.db import Database
from careerforge.models import AgentRun, ModelReceipt, SourceRead, StageArtifact
from careerforge.services.knowledge import KnowledgeService
from careerforge.services.project_analysis import ProjectAnalysisService
from careerforge.services.runtime import Runtime
from careerforge.services.sources import SourceService
from careerforge.util import digest, fingerprint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    source = args.evidence_dir.resolve()
    target = args.output_dir.resolve()
    if target.exists():
        raise SystemExit("Use a fresh output directory; prior artifacts are not overwritten.")
    target.mkdir(parents=True)
    staging = target / "copied_input"
    staging.mkdir()
    originals = [path for path in source.glob("run.sqlite*") if path.is_file()]
    hashes = {path.name: digest(path.read_bytes()) for path in originals}
    for path in originals:
        shutil.copy2(path, staging / path.name)
    with sqlite3.connect(staging / "run.sqlite") as copied:
        with sqlite3.connect(target / "replay.sqlite") as backup:
            copied.backup(backup)
    settings = Settings(
        mode="test",
        database_url="sqlite+pysqlite:///" + str(target / "replay.sqlite"),
        state_dir=target,
        allow_fixture_publication=True,
        embedding_dimensions=8,
    )
    database = Database(settings)
    service = ProjectAnalysisService(
        KnowledgeService(database, Runtime(database)), SourceService(database)
    )
    build = json.loads((source / "build_result.json").read_text())
    with database.read() as db:
        run = db.get(AgentRun, build["run_id"])
        result = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == "result"
            )
        ).payload
        before = {
            "run_manifest_hash": fingerprint(run.input_manifest),
            "result_hash": fingerprint(result),
            "model_receipts": db.scalar(select(func.count()).select_from(ModelReceipt)),
            "source_reads": db.scalar(select(func.count()).select_from(SourceRead)),
            "calls_consumed": run.calls_consumed,
        }
        projected = service.project_in(db, run.principal_id, run, result)
        assert projected == service.project_in(db, run.principal_id, run, result)
        after = {
            "run_manifest_hash": fingerprint(run.input_manifest),
            "result_hash": fingerprint(result),
            "model_receipts": db.scalar(select(func.count()).select_from(ModelReceipt)),
            "source_reads": db.scalar(select(func.count()).select_from(SourceRead)),
            "calls_consumed": run.calls_consumed,
        }
    database.engine.dispose()
    assert before == after
    assert hashes == {path.name: digest(path.read_bytes()) for path in originals}
    (target / "before.json").write_text(json.dumps(build, ensure_ascii=False, indent=2) + "\n")
    (target / "after.json").write_text(json.dumps(projected, ensure_ascii=False, indent=2) + "\n")
    report = {
        "kind": "offline_projection_replay_not_new_model_experiment",
        "run_id": build["run_id"],
        "original_database_hashes_unchanged": hashes,
        "before": before,
        "after": after,
        "additional_model_calls": 0,
        "new_source_reads": 0,
        "deterministic_repeated_read": True,
        "historical_result_modified": False,
        "projection_hash": projected["projection"]["content_hash"],
        "dimensions": {
            key: {"status": projected[key]["status"], "claim_count": len(projected[key]["items"])}
            for key in ("modules", "call_chain_edges", "exception_boundaries")
        },
        "goal_version_id": projected["projection"]["goal_version_id"],
        "other_goals_status": projected["projection"]["other_goals_status"],
        "full_repository_understanding": False,
        "runtime_execution_observed": False,
    }
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
