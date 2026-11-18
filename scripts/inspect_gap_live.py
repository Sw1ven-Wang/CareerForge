"""Read-only, resumable observation of the saved real desk request."""

import json
import sqlite3
import sys
from pathlib import Path

import httpx

path = Path(sys.argv[1] if len(sys.argv) > 1 else "private/local-validation/gap-supplement-live.json")
record = json.loads(path.read_text(encoding="utf-8"))
with httpx.Client(base_url="http://127.0.0.1:8001", headers={"X-CareerForge-Desk": "1"}, timeout=45) as client:
    response = client.get("/desk/overview")
    response.raise_for_status()
    job = next(j for j in response.json()["jobs"] if j.get("request_hash") == record["initial"]["request_hash"])
    record["latest"] = job
    with sqlite3.connect("private/local-runtime/development.db") as db:
        db.row_factory = sqlite3.Row
        record["runs"] = [dict(r) for r in db.execute(
            "select id,workflow,status,calls_consumed from agent_runs where funding_id="
            "(select funding_id from agent_runs where id=?)", (record["initial"]["run_id"],))]
    if job["status"] != "running":
        response = client.get("/desk/conversations/" + job["conversation_id"])
        response.raise_for_status()
        record["conversation"] = response.json()
        answer_id = job.get("gap_supplement", {}).get("answer_turn_id")
        answer = next((t for t in record["conversation"]["turns"] if t["id"] == answer_id), None)
        record["citation_checks"] = []
        if answer:
            record["original_question_preserved"] = answer["question"] == record["request"]["question"]
            for event in answer["events"]:
                if event["type"] != "answer.block.validated":
                    continue
                payload = event["payload"]
                for index, citation in enumerate(payload["content"].get("citations", [])):
                    result = client.get(f"/desk/navigation/{payload['block_id']}", params={"citation_index": index})
                    result.raise_for_status()
                    target = result.json()
                    record["citation_checks"].append({
                        "path": target["path"], "commit": target["commit"],
                        "valid": target["server_location_valid"],
                        "complete": target["display"]["content_complete"],
                        "text": target["display"]["text"],
                    })
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": job["status"], "runs": [(r["workflow"], r["status"]) for r in record["runs"]],
                      "resolved": job.get("gap_supplement", {}).get("resolved"),
                      "citations": len(record.get("citation_checks", []))}, ensure_ascii=False))
