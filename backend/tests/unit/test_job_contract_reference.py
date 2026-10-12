import pytest
from pydantic import ValidationError
from careerforge.schemas.job_contract import (
    JobPostingSnapshotV1,
    RequirementFactV1,
    DemandReportV1,
    validate_requirement_against_snapshot,
)

JD = (
    "工作职责：参与Agent工作流研发。\n任职要求：熟悉 Python 或 Go 任意一种语言；每周至少出勤 4 天。"
)


def snap():
    sec1 = "工作职责：参与Agent工作流研发。"
    sec2 = "任职要求：熟悉 Python 或 Go 任意一种语言；每周至少出勤 4 天。"
    assert JD == sec1 + "\n" + sec2
    return JobPostingSnapshotV1.model_validate(
        {
            "schema_version": "careerforge.job_snapshot.v1",
            "posting_id": "p1",
            "job_snapshot_id": "j1",
            "source": {
                "source_capture_id": "cap1",
                "provider": "manual_import",
                "ingest_method": "manual_import",
                "raw_snapshot_id": "raw1",
                "source_url": None,
                "retrieved_at": "2026-10-12T08:00:00Z",
                "read_status": "full_text",
            },
            "job": {"title_raw": "Agent 应用开发实习生", "employer_name": "合成示例"},
            "document": {
                "canonical_text": JD,
                "sections": [
                    {
                        "section_id": "sec1",
                        "kind": "responsibility",
                        "start": 0,
                        "end": len(sec1),
                        "text": sec1,
                    },
                    {
                        "section_id": "sec2",
                        "kind": "qualification",
                        "start": len(sec1) + 1,
                        "end": len(JD),
                        "text": sec2,
                    },
                ],
            },
            "quality": {
                "normalization_status": "ready",
                "normalizer_version": "norm-v1",
                "dedupe_status": "unique",
            },
        }
    )


def req():
    quote = "熟悉 Python 或 Go 任意一种语言"
    start = JD.index(quote)
    return RequirementFactV1.model_validate(
        {
            "schema_version": "careerforge.requirement.v1",
            "requirement_id": "r1",
            "job_snapshot_id": "j1",
            "requirement_kind": "technical_skill",
            "importance": "required",
            "origin": "explicit",
            "expression": {
                "type": "any_of",
                "children": [
                    {
                        "type": "concept",
                        "concept_id": "technology.python",
                        "concept_kind": "technology",
                    },
                    {
                        "type": "concept",
                        "concept_id": "technology.go",
                        "concept_kind": "technology",
                    },
                ],
            },
            "evidence": {
                "raw_snapshot_id": "raw1",
                "job_snapshot_id": "j1",
                "section_id": "sec2",
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            },
            "review_status": "confirmed",
            "extractor_version": "extract-v1",
            "taxonomy_version": "tax-v1",
        }
    )


def test_or_requirement_and_evidence():
    s, r = snap(), req()
    assert r.expression.type == "any_of"
    validate_requirement_against_snapshot(r, s)


def test_tampered_quote_fails():
    r = req().model_copy(deep=True)
    r.evidence.quote = "熟悉 Python 和 Go 任意一种语言"
    with pytest.raises(ValueError):
        validate_requirement_against_snapshot(r, snap())


def test_invalid_section_span_rejected():
    payload = snap().model_dump(mode="json")
    payload["document"]["sections"][1]["end"] += 1
    with pytest.raises(ValidationError):
        JobPostingSnapshotV1.model_validate(payload)


def test_report_denominator():
    valid = {
        "schema_version": "careerforge.demand_report.v1",
        "report_id": "dr1",
        "goal_version_id": "goal1",
        "scope": {
            "role_families": ["agent_application"],
            "employment_types": ["internship"],
            "locations": ["北京"],
        },
        "included_job_snapshot_ids": ["j1"],
        "denominator": 1,
        "taxonomy_version": "tax-v1",
        "extractor_version": "ex-v1",
        "aggregation_version": "ag-v1",
        "demands": [
            {
                "demand_id": "d1",
                "demand_unit": "expression_group",
                "expression_signature": "any_of(technology.go,technology.python)",
                "required_solo": {"count": 0, "denominator": 1},
                "required_alternative": {"count": 1, "denominator": 1},
                "preferred": {"count": 0, "denominator": 1},
                "responsibility": {"count": 0, "denominator": 1},
                "supporting_requirement_ids": ["r1"],
            }
        ],
    }
    DemandReportV1.model_validate(valid)
    valid["denominator"] = 2
    with pytest.raises(ValidationError):
        DemandReportV1.model_validate(valid)


def test_nested_expression():
    p = req().model_dump(mode="json")
    p["expression"] = {
        "type": "all_of",
        "children": [
            p["expression"],
            {
                "type": "constraint",
                "field": "availability.days_per_week",
                "comparator": "gte",
                "value": 4,
                "unit": "days_per_week",
            },
        ],
    }
    r = RequirementFactV1.model_validate(p)
    assert r.expression.type == "all_of"


def test_naive_retrieved_at_rejected():
    payload = snap().model_dump(mode="json")
    payload["source"]["retrieved_at"] = "2026-10-12T08:00:00"
    with pytest.raises(ValidationError):
        JobPostingSnapshotV1.model_validate(payload)


def test_evidence_raw_source_must_match():
    r = req().model_copy(deep=True)
    r.evidence.raw_snapshot_id = "another_source"
    with pytest.raises(ValueError):
        validate_requirement_against_snapshot(r, snap())


def test_unknown_fields_rejected():
    payload = snap().model_dump(mode="json")
    payload["job"]["pretend_verified"] = True
    with pytest.raises(ValidationError):
        JobPostingSnapshotV1.model_validate(payload)


def test_ready_but_empty_rejected():
    payload = snap().model_dump(mode="json")
    payload["document"] = {"canonical_text": "", "sections": []}
    with pytest.raises(ValidationError):
        JobPostingSnapshotV1.model_validate(payload)


def test_eligibility_requirement_is_not_a_technology_skill():
    s = snap()
    quote = "每周至少出勤 4 天"
    start = JD.index(quote)
    r = RequirementFactV1.model_validate(
        {
            "schema_version": "careerforge.requirement.v1",
            "requirement_id": "r_elig_demo_001",
            "job_snapshot_id": "j1",
            "requirement_kind": "eligibility",
            "importance": "required",
            "origin": "explicit",
            "expression": {
                "type": "constraint",
                "field": "availability.days_per_week",
                "comparator": "gte",
                "value": 4,
                "unit": "days_per_week",
            },
            "evidence": {
                "raw_snapshot_id": "raw1",
                "job_snapshot_id": "j1",
                "section_id": "sec2",
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            },
            "review_status": "confirmed",
            "extractor_version": "extract-v1",
            "taxonomy_version": "tax-v1",
        }
    )
    validate_requirement_against_snapshot(r, s)
    assert r.requirement_kind == "eligibility"
