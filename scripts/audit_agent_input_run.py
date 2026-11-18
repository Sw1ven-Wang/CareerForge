"""Audit a settled development chain without provider calls or owner credentials."""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# A source-checkout entry point must never silently load an older installed CareerForge.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_input_runtime import InputFlow


def audit(root, inputs):
    root = Path(root).resolve()
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "business_loop_settled_without_promotion", result.get("error")
    flow = InputFlow(inputs, result["flow_id"])
    assert flow.fingerprint == result["input_fingerprint"]
    assert set(result["pipeline_stages"]) == {f"S{i:02}" for i in range(1, 19)}
    stages = result["stages"]
    assert (
        stages["jd_extracted"]["goal_version_id"]
        == stages["after_evidence_retry"]["goal_version_id"]
    )
    source_id = stages["source_imported"]["source_id"]
    read_id_citations = []
    for key in ("tutor_initial", "tutor_followup"):
        assert stages[key]["status"] == "completed"
        blocks = [
            e["payload"]["content"]
            for e in stages[key]["events"]
            if e["type"] == "answer.block.validated"
        ]
        assert blocks and any(b.get("citations") for b in blocks)
        read_id_citations.extend(c["read_id"] for b in blocks for c in b.get("citations", []))
    assessment = stages["assessment_result"]["assessment_candidate"]
    assert assessment["synthetic_model"] is False and assessment["competence_verified"] is False
    assert assessment["help_conditions"]["agent_assistance"] is True
    assert all(
        r["outcome"] == "unknown"
        for r in stages["learner_observation"]["action_receipts"][0]["rubric_results"]
    )
    evaluation_id = stages["development_replay_result"]["result"]["evaluation_id"]
    with sqlite3.connect((root / "workflow.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("pragma integrity_check").fetchone() == ("ok",)
        assert not db.execute("pragma foreign_key_check").fetchall()
        assert not db.execute(
            "select id from agent_runs where status not in ('succeeded','partially_succeeded','failed','cancelled')"
        ).fetchall()
        assert not db.execute(
            "select id from model_call_receipts where status!='completed'"
        ).fetchall()
        assert not db.execute("select id from funding_agreements where calls_held!=0").fetchall()
        repo, commit = db.execute(
            "select repository_key,commit_sha from source_sets where id=?", (source_id,)
        ).fetchone()
        assert (
            repo == flow.payload("S06")["repository_key"]
            and commit == flow.payload("S06")["commit_sha"]
        )
        source_text = db.execute(
            "select source_text from career_goal_versions where id=?",
            (stages["jd_extracted"]["goal_version_id"],),
        ).fetchone()[0]
        assert source_text == flow.payload("S01")["raw_text"]
        pubs = db.execute(
            "select p.id,r.reviewed_content_hash,u.content_hash,r.reviewed_render_hash,u.render_hash,r.reviewer_run_id,u.analysis_run_id from publications p join review_decisions r on p.review_id=r.id join knowledge_unit_versions u on p.unit_version_id=u.id"
        ).fetchall()
        assert pubs
        assert all(
            ch == uch and rh == urh and reviewer != analyst
            for _, ch, uch, rh, urh, reviewer, analyst in pubs
        )
        for read_id in read_id_citations:
            assert db.execute(
                "select id from source_reads where id=? and source_id=?", (read_id, source_id)
            ).fetchone()
        assert (
            db.execute("select count(*) from source_reads where role='reviewer'").fetchone()[0] > 0
        )
        native_attempt = stages["assessment_result"]["attempt_id"]
        assert native_attempt == stages["practice_submitted"]["action_receipts"][0]["attempt_id"]
        assisted, help_used, response = db.execute(
            "select assistance_mode,external_help,response from learning_attempts where id=?",
            (native_attempt,),
        ).fetchone()
        assert assisted == "assisted" and help_used == "used" and response
        raw, run_id = db.execute(
            "select report,run_id from evolution_evaluations where id=?", (evaluation_id,)
        ).fetchone()
        evaluation = json.loads(raw)
        assert evaluation["decision"] != "passed"
        assert db.execute("select status from agent_runs where id=?", (run_id,)).fetchone() == (
            "succeeded",
        )
        traces = json.loads(
            db.execute(
                "select trace from policy_replay_manifests where evaluation_id=?", (evaluation_id,)
            ).fetchone()[0]
        )
        assert len(traces) == 4 and {t["arm"] for t in traces} == {"baseline", "candidate"}
        assert all(t["model_calls"] > 0 for t in traces)
        assert not db.execute("select id from policy_heads where revision!=1").fetchall()
        rows = db.execute("select status,usage from model_call_receipts").fetchall()
        read_roles = dict(
            db.execute("select role,count(*) from source_reads group by role").fetchall()
        )
    summary = {
        "status": "PASS_NATIVE_DEVELOPMENT_CHAIN",
        "flow_id": flow.flow_id,
        "pipeline_stages": 18,
        "bindings": len(result["bindings"]),
        "publications": len(pubs),
        "model_receipts": dict(Counter(row[0] for row in rows)),
        "source_read_roles": read_roles,
        "evaluation_id": evaluation_id,
        "development_replay": {
            k: evaluation.get(k)
            for k in [
                "decision",
                "reason",
                "baseline_score",
                "candidate_score",
                "improvement",
                "model_calls",
                "hard_gate_passed",
                "safety_violations",
                "incomplete_execution",
            ]
        },
        "independent_gold": "NOT_RUN",
        "production_promotion": False,
        "human_learning_effect": "NOT_EVALUATED",
        "audit_model_calls": 0,
    }
    (root / "native-chain-audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--agent-input-root", required=True)
    args = p.parse_args()
    audit(args.state_dir, args.agent_input_root)
