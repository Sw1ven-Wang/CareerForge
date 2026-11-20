from types import SimpleNamespace

import pytest

from careerforge.contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from careerforge.errors import DomainError
from careerforge.models import Turn
from careerforge.services import gap_research_context as context
from careerforge.services.build import BuildService
from careerforge.util import fingerprint


def fixture(monkeypatch):
    assessment = {
        "binding": {"turn_id": "turn"},
        "unsupported_aspects": ["session calls loop"],
        "missing_aspects": [],
    }
    assessment["assessment_hash"] = fingerprint(assessment)
    rows = [
        SimpleNamespace(stage_key=ANSWER_COMPLETION_STAGE, input_hash="fixed", payload=assessment),
        SimpleNamespace(
            stage_key="retrieval",
            input_hash="fixed",
            payload={"retrieval_manifest_id": "selection"},
        ),
    ]
    db = SimpleNamespace(
        get=lambda cls, key: (
            SimpleNamespace(run_id="run", status="completed")
            if cls is Turn
            else SimpleNamespace(id="run", principal_id="owner", input_hash="fixed")
        ),
        scalars=lambda query: rows,
    )
    checked = []
    monkeypatch.setattr(context, "check_closure", lambda db, actor, roots: checked.append(roots))
    return db, rows, checked


def test_gap_snapshot_is_bound_to_assessment_and_child_manifest(monkeypatch):
    db, rows, checked = fixture(monkeypatch)
    snapshot = context.capture(db, "owner", "turn")
    assert snapshot["missing_aspects"] == ["session calls loop"]
    assert snapshot["facts_approved"] is False
    assert checked == [["selection"]]
    assert (
        BuildService._child_manifest(
            {"execution_contract_version": "test", "answer_gap_investigation": snapshot}
        )["answer_gap_investigation"]
        == snapshot
    )
    assert context.validate(db, "owner", snapshot) == snapshot
    snapshot["missing_aspects"] = ["invented objective"]
    with pytest.raises(DomainError, match="固定补证目标"):
        context.validate(db, "owner", snapshot)


def test_changed_assessment_and_wrong_owner_fail_closed(monkeypatch):
    db, rows, _ = fixture(monkeypatch)
    with pytest.raises(DomainError):
        context.capture(db, "other", "turn")
    rows[0].payload["unsupported_aspects"] = ["tampered"]
    with pytest.raises(DomainError):
        context.capture(db, "owner", "turn")
