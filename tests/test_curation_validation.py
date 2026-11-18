from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from careerforge.contracts_curation_validation import CuratedReviewOutput, CuratedTutorOutput
from careerforge.errors import DomainError
from careerforge.services import curation_validation as service
from careerforge.util import fingerprint


def review():
    return {
        "decisions": [
            {
                "unit_version_id": "unit",
                "claim_key": key,
                "outcome": "supported_within_scope",
                "explanation": "局部源码支持此声明",
                "independent_reads": [{"read_id": "fresh", "quote": "exact source quote"}],
                "required_correction": None,
            }
            for key in ["one", "two"]
        ],
        "mechanism_boundary": "只审查固定窗口内的机制",
        "unresolved_questions": [],
    }


UNITS = [{"unit_version_id": "unit", "claims": [{"key": "one"}, {"key": "two"}]}]


def test_review_requires_every_claim_once():
    assert service.validate_decisions(UNITS, CuratedReviewOutput.model_validate(review())) == [
        "unit"
    ]
    for decisions in [review()["decisions"][:1], review()["decisions"] * 2]:
        value = {**review(), "decisions": decisions}
        with pytest.raises(DomainError, match="逐条"):
            service.validate_decisions(UNITS, CuratedReviewOutput.model_validate(value))


@pytest.mark.parametrize("change", [{"independent_reads": []}, {"required_correction": "需修改"}])
def test_supported_claim_cannot_skip_evidence_or_keep_a_correction(change):
    value = review()
    value["decisions"][0].update(change)
    with pytest.raises(DomainError):
        service.validate_decisions(UNITS, CuratedReviewOutput.model_validate(value))


def test_one_unproven_claim_blocks_unit_promotion():
    value = review()
    value["decisions"][1].update(outcome="insufficient_evidence", independent_reads=[])
    assert service.validate_decisions(UNITS, CuratedReviewOutput.model_validate(value)) == []


def test_tutor_requires_same_effective_view_hash(monkeypatch):
    monkeypatch.setattr(service, "effective_units", lambda *args: deepcopy(UNITS))
    payload = {
        "curation_id": "curated",
        "semantic_reviewed_unit_ids": ["unit"],
        "effective_unit_hashes": {"unit": fingerprint(UNITS[0])},
    }
    monkeypatch.setattr(service, "require_object", lambda *args: SimpleNamespace(payload=payload))
    service.require_reviewed(None, "actor", "curated", ["unit"], ["review"])
    payload["effective_unit_hashes"]["unit"] = "stale"
    with pytest.raises(DomainError) as error:
        service.require_reviewed(None, "actor", "curated", ["unit"], ["review"])
    assert error.value.code == "CURATION_NOT_REVIEWED"
    payload["curation_id"] = "different"
    with pytest.raises(DomainError) as error:
        service.require_reviewed(None, "actor", "curated", ["unit"], ["review"])
    assert error.value.code == "CURATION_REVIEW_BINDING"


def test_model_self_report_cannot_claim_learner_mastery():
    value = {
        "answer": "这是有源码范围的候选试讲回答",
        "citations": [{"unit_version_id": "unit", "read_id": "fresh", "quote": "source quote"}],
        "reading_check": {"question": "请指出哪条分支", "expected_points": ["定位证据"]},
        "limitations": ["没有学习者作答"],
        "learner_mastery_demonstrated": False,
    }
    CuratedTutorOutput.model_validate(value)
    value["learner_mastery_demonstrated"] = True
    with pytest.raises(ValidationError):
        CuratedTutorOutput.model_validate(value)


def test_unchanged_review_can_be_inherited_but_changed_claims_cannot(monkeypatch):
    monkeypatch.setattr(service, "effective_units", lambda *args: deepcopy(UNITS))
    review_payload = {
        "curation_id": "old",
        "semantic_reviewed_unit_ids": ["unit"],
        "effective_unit_hashes": {"unit": fingerprint(UNITS[0])},
    }

    def get_object(db, actor, identifier, kind):
        if kind == "curation_semantic_review":
            return SimpleNamespace(payload=review_payload)
        return SimpleNamespace(
            payload={"previous_curation_id": "old"} if identifier == "new" else {}
        )

    monkeypatch.setattr(service, "require_object", get_object)
    service.require_reviewed(None, "actor", "new", ["unit"], ["review"])
    changed = deepcopy(UNITS)
    changed[0]["claims"][0]["statement"] = "changed meaning"
    monkeypatch.setattr(service, "effective_units", lambda *args: changed)
    with pytest.raises(DomainError) as error:
        service.require_reviewed(None, "actor", "new", ["unit"], ["review"])
    assert error.value.code == "CURATION_NOT_REVIEWED"


def test_actual_source_dependencies_are_required_before_model_call(monkeypatch):
    import asyncio
    from contextlib import nullcontext

    units = [{**UNITS[0], "source_id": "source", "analysis_run_id": "author-run"}]
    monkeypatch.setattr(service, "effective_units", lambda *args: units)
    monkeypatch.setattr(service, "check_closure", lambda *args: None)
    app = SimpleNamespace(
        runtime=SimpleNamespace(stage=lambda *args: None),
        db=SimpleNamespace(read=lambda: nullcontext(None)),
    )
    run = SimpleNamespace(
        id="new-review",
        principal_id="actor",
        input_manifest={
            "curation_validation_contract": service.CONTRACT,
            "phase": "semantic_review",
            "curation_id": "curated",
            "unit_version_ids": ["unit"],
            "effective_units_hash": fingerprint(units),
            "access_dependencies": ["curated", "unit"],
        },
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(service.execute(app, run, 1, "worker"))
    assert error.value.code == "CURATION_DEPENDENCIES"
