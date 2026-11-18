"""Read-only audit of the delivered native run, without model calls or credentials."""

import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
r = json.loads((root / "business-result.json").read_text(encoding="utf-8"))
assert r["status"] == "business_loop_settled_without_promotion"
s = r["stages"]
assert s["jd_extracted"]["goal_version_id"] == s["after_evidence_retry"]["goal_version_id"]
assert s["after_evidence_retry"]["publication_count"] == 2
assert s["after_evidence_retry"]["coverage"] == "partial"
for key in ("tutor_initial", "tutor_followup"):
    assert s[key]["status"] == "completed"
    assert any(
        e["type"] == "answer.block.validated" and e["payload"]["content"].get("citations")
        for e in s[key]["events"]
    )
assessment = s["assessment_result"]["assessment_candidate"]
assert assessment["synthetic_model"] is False
assert assessment["competence_verified"] is False
assert assessment["help_conditions"]["agent_assistance"] is True
assert all(
    x["outcome"] == "unknown"
    for x in s["learner_observation"]["action_receipts"][0]["rubric_results"]
)
evaluation_id = s["development_replay_result"]["result"]["evaluation_id"]
p = (root / "sanitized-workflow.sqlite").resolve()
with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True) as db:
    assert db.execute("pragma integrity_check").fetchone() == ("ok",)
    assert not db.execute("select id from principals where active != 0").fetchall()
    decision, raw, run = db.execute(
        "select decision,report,run_id from evolution_evaluations where id=?", (evaluation_id,)
    ).fetchone()
    report = json.loads(raw)
    assert decision == "inconclusive"
    assert report["reason"] == "development_probe_is_not_independent_gold"
    assert db.execute("select status from agent_runs where id=?", (run,)).fetchone() == (
        "succeeded",
    )
    traces = json.loads(
        db.execute(
            "select trace from policy_replay_manifests where evaluation_id=?", (evaluation_id,)
        ).fetchone()[0]
    )
    assert len(traces) == 4
    assert {t["arm"] for t in traces} == {"baseline", "candidate"}
    assert all(t["model_calls"] > 0 and not t["error_code"] for t in traces)
    assert not db.execute("select id from policy_heads where revision != 1").fetchall()
print(
    "PASS: native lineage, cited Tutor, assisted assessment, four real replay arms, no policy promotion."
)
