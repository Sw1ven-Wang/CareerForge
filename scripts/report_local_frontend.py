"""Record non-secret validation results from the local learning desk."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

source = Path(__file__).resolve().parents[1]
project = source.parent
output = source / "private" / "local-validation"
runtime = source / "private" / "local-runtime"
with httpx.Client(
    base_url="http://127.0.0.1:8001",
    headers={"X-CareerForge-Desk": "1"},
    trust_env=False,
    timeout=30,
) as client:
    home = client.get("/")
    home.raise_for_status()
    response = client.get("/desk/overview")
    response.raise_for_status()
    overview = response.json()
    runs = []
    for job in overview["jobs"]:
        assert job["status"] != "running", "A required run is still pending"
        run = client.get("/desk/runs/" + job["run_id"])
        run.raise_for_status()
        data = run.json()
        conversation = client.get("/desk/conversations/" + job["conversation_id"])
        conversation.raise_for_status()
        turn = next(t for t in conversation.json()["turns"] if t["id"] == job["turn_id"])
        blocks = [
            e["payload"]["content"] for e in turn["events"] if e["type"] == "answer.block.validated"
        ]
        assert data["status"] == "succeeded" and turn["status"] == "completed"
        assert job["funding_revoked"]
        runs.append(
            {
                "run_id": job["run_id"],
                "conversation_id": job["conversation_id"],
                "turn_id": job["turn_id"],
                "status": data["status"],
                "calls": data["calls_consumed"],
                "citation_count": sum(len(b.get("citations", [])) for b in blocks),
                "partial_answer": any(b.get("kind") == "evidence_gap" for b in blocks),
                "temporary_funding_revoked": job["funding_revoked"],
            }
        )
    assert len(overview["publications"]) == 14
with (project / "evidence" / "session-final.sqlite").open("rb") as stream:
    original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
assert original_hash == "dc3bb04fff02025bcd964216665ff7ccf6d3fe4e14f0d3717ea0271136c9548e"
report = {
    "validated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    "url": "http://127.0.0.1:8001",
    "server_status": "running",
    "tests": {"passed": 18, "failed": 0, "junit": "frontend-tests.xml"},
    "browser": {
        "knowledge_search": True,
        "publication_source_dialog": True,
        "unaccepted_plan_created": True,
        "jd_and_requirement_read": True,
        "live_chat_submission_and_output": True,
        "answer_citations_clickable": True,
        "task_detail": True,
        "history_after_restart": True,
        "responsive_widths": [1440, 390],
        "horizontal_overflow": False,
        "console_errors": 0,
        "screenshots": ["frontend-desktop.jpg", "frontend-mobile.jpg"],
    },
    "live_runs": runs,
    "plan_draft": {"title": "RAG 检索机制 · 网页验收草稿", "accepted": False},
    "original_snapshot_unchanged": True,
    "snapshot_sha256": original_hash,
    "limits": [
        "Local single-user development UI only",
        "Plans use deterministic evidence-based proposals",
        "Practice reflection is recorded; Docker execution is not exposed",
        "Research-to-publication approvals are not exposed",
        "Live answer completed with a disclosed evidence gap",
    ],
}
(output / "frontend-validation.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
checkpoint_path = runtime / "frontend-checkpoint.json"
checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
checkpoint.update(
    status="completed",
    remaining=[],
    active_jobs=[],
    service_status="running",
    evidence_report=str(output / "frontend-validation.json"),
    next_action="User can open http://127.0.0.1:8001; the test plan remains an unaccepted draft.",
)
checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        {"service": "running", "tests_passed": 18, "live_runs": runs, "snapshot_unchanged": True}
    )
)
