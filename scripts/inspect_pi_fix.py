"""Read-only polling and citation verification for the saved Pi repair request."""

import json
import sqlite3
import sys
from pathlib import Path

import httpx

p = Path(sys.argv[1] if len(sys.argv) > 1 else "private/local-validation/pi-live-fix.json")
record = json.loads(p.read_text(encoding="utf-8"))
with httpx.Client(
    base_url="http://127.0.0.1:8001", headers={"X-CareerForge-Desk": "1"}, timeout=45
) as c:
    overview = c.get("/desk/overview")
    overview.raise_for_status()
    job = next(
        j
        for j in overview.json()["jobs"]
        if j.get("request_hash") == record["initial"]["request_hash"]
    )
    record["latest"] = job
    record["status"] = job["status"]
    with sqlite3.connect("private/local-runtime/development.db") as db:
        db.row_factory = sqlite3.Row
        record["runs"] = [
            dict(r)
            for r in db.execute(
                "select id,workflow,status,calls_consumed from agent_runs where funding_id="
                "(select funding_id from agent_runs where id=?)",
                (record["initial"]["run_id"],),
            )
        ]
    if job["status"] != "running":
        detail = c.get("/desk/conversations/" + job["conversation_id"])
        detail.raise_for_status()
        record["conversation"] = detail.json()
        answer_id = job.get("gap_supplement", {}).get("answer_turn_id") or job.get(
            "project_question", {}
        ).get("answer_turn_id")
        if not answer_id:
            answer_id = next(
                (
                    step.get("result", {}).get("turn_id")
                    for step in reversed(job.get("steps", []))
                    if step.get("result", {}).get("turn_id")
                ),
                None,
            )
        turn = next((t for t in record["conversation"]["turns"] if t["id"] == answer_id), None)
        record["citation_checks"] = []
        if turn:
            for event in turn["events"]:
                if event["type"] != "answer.block.validated":
                    continue
                payload = event["payload"]
                for i, _ in enumerate(payload["content"].get("citations", [])):
                    response = c.get(
                        "/desk/navigation/" + payload["block_id"], params={"citation_index": i}
                    )
                    response.raise_for_status()
                    target = response.json()
                    record["citation_checks"].append(
                        {
                            k: target.get(k)
                            for k in ["path", "commit", "server_location_valid", "display"]
                        }
                    )
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": job["status"],
                "reply": job.get("reply"),
                "project": job.get("project_question"),
                "runs": [(r["workflow"], r["status"]) for r in record["runs"]],
                "citations": len(record.get("citation_checks", [])),
            },
            ensure_ascii=False,
        )
    )
