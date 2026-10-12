"""Create an offline, explicitly labelled local demo for an existing account."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from careerforge.application.goal_service import save_goal
from careerforge.application.market_report_service import read_report, start
from careerforge.application.source_service import import_source
from careerforge.infrastructure.db.models import AgentRun, MarketReport, User
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.queue.outbox_dispatcher import dispatch_once
from careerforge.schemas.job_extraction import MarketRunInput
from careerforge.schemas.resources import GoalInput, SourceImport
from sqlalchemy import select
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True, help="Existing local demo account")
    parser.add_argument("--joblearn", type=Path, required=True, help="Read-only personal workspace")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = root / "evals/datasets/manual_case"
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    files = []
    for record in manifest["personal_evidence"]["files"]:
        # Read explicitly authorized files only; do not import, execute or publish their body.
        body = (args.joblearn / record["source_path"]).read_bytes()
        files.append(
            {
                "path": record["source_path"],
                "sha256": hashlib.sha256(body).hexdigest(),
                "matches_s00": hashlib.sha256(body).hexdigest() == record["sha256"],
            }
        )
    with Session(get_engine()) as db, db.begin():
        owner = db.scalar(select(User).where(User.email == args.email))
        if owner is None:
            raise SystemExit("Existing account required; no account/password is created")
        target = save_goal(
            db,
            owner.id,
            GoalInput(
                role_direction="Agent开发（S03 离线实习样本演示）",
                job_type="internship",
                user_objectives=["演示范围，不代表本人求职类别已确认"],
            ),
            "s03-demo-goal-v1",
        )
        snapshots = []
        for record in manifest["job_sources"]:
            text = (base / record["content_path"]).read_text(encoding="utf-8")
            assert hashlib.sha256(text.encode()).hexdigest() == record["sha256"]
            title = next(line for line in text.splitlines() if record["id"] in line)
            value = import_source(
                db,
                owner.id,
                SourceImport(
                    source_type="job_posting",
                    title=title,
                    source_url=record["source_url"],
                    content=text,
                    declared_provenance="S00 保存的官方 JD 正文；未重新联网；发布时间待人工确认",
                ),
                "s03-demo-source-" + record["id"],
            )
            snapshots.append(value["source_snapshot_id"])
        note = import_source(
            db,
            owner.id,
            SourceImport(
                source_type="user_note",
                title="joblearn 个人材料（只读静态核验）",
                content=json.dumps(
                    {
                        "project": "joblearn",
                        "files": files,
                        "runtime_verified": False,
                        "authorship_verified": False,
                        "ability_verdict": "unknown",
                    },
                    ensure_ascii=False,
                ),
                declared_provenance="用户授权读取；仅记录清单和哈希，未据此认定已掌握技能",
            ),
            "s03-demo-personal-v1",
        )
        snapshots.append(note["source_snapshot_id"])
        result = start(
            db,
            owner.id,
            MarketRunInput(
                goal_version_id=target["goal_version_id"], source_snapshot_ids=snapshots
            ),
            "s03-demo-run-v2",
        )
        run_id, owner_id = UUID(result["run_id"]), owner.id
    dispatch_once(only_run_id=run_id)
    for _ in range(200):
        with Session(get_engine()) as db:
            run = db.get(AgentRun, run_id)
            if run is not None and run.status in {"succeeded", "partially_succeeded", "failed"}:
                report = db.scalar(select(MarketReport).where(MarketReport.run_id == run_id))
                if report is None:
                    raise SystemExit(f"Run ended without report: {run.status}")
                report = read_report(db, owner_id, report.id)
                payload = {"market_report_id": str(report.id), **report.payload}
                destination = root / "docs/samples/S03_OFFLINE_REPORT.json"
                destination.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "report_id": str(report.id),
                            "status": run.status,
                            "counts": report.payload["counts"],
                            "eligible": len(report.payload["eligible_posting_version_ids"]),
                            "personal_files": len(files),
                            "all_personal_hashes_match_s00": all(f["matches_s00"] for f in files),
                        },
                        ensure_ascii=False,
                    )
                )
                return
        time.sleep(0.1)
    raise SystemExit("Worker did not finish; keep the run_id and inspect run events")


if __name__ == "__main__":
    main()
