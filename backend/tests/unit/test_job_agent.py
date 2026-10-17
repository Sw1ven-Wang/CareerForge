import pytest
from careerforge.application.job_agent_service import request_for, strict_schema, validated_facts
from careerforge.domain.job_normalization import sections
from careerforge.schemas.job_agent import AgentExtraction
from careerforge.schemas.job_contract import JobPostingSnapshotV1
from careerforge.schemas.model_io import ModelResult


def snapshot():
    text = "任职要求：熟悉 Python 或 Go。"
    return JobPostingSnapshotV1.model_validate(
        {
            "schema_version": "careerforge.job_snapshot.v1",
            "posting_id": "p",
            "job_snapshot_id": "j",
            "source": {
                "source_capture_id": "r",
                "raw_snapshot_id": "r",
                "provider": "manual_import",
                "ingest_method": "manual_import",
                "retrieved_at": "2026-10-12T00:00:00Z",
                "read_status": "full_text",
            },
            "job": {"title_raw": "Agent实习"},
            "document": {"canonical_text": text, "sections": sections(text)},
            "quality": {
                "normalization_status": "ready",
                "normalizer_version": "test",
                "dedupe_status": "unique",
            },
        }
    )


def candidate(quote):
    return {
        "candidates": [
            {
                "section_id": "sec-1",
                "quote": quote,
                "requirement_kind": "technical_skill",
                "importance": "required",
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
            }
        ],
        "unresolved": [],
    }


def test_model_quote_is_validated_and_never_confirmed():
    snap = snapshot()
    result = ModelResult(
        provider="test", structured=candidate(snap.document.canonical_text), finish_reason="stop"
    )
    facts, _ = validated_facts(snap, result)
    assert facts[0].review_status == "auto_extracted"
    assert facts[0].expression.type == "any_of"
    assert (
        snap.document.canonical_text[facts[0].evidence.start : facts[0].evidence.end]
        == facts[0].evidence.quote
    )
    result.structured = candidate("虚构 Python 或 Go 原文")
    with pytest.raises(ValueError):
        validated_facts(snap, result)


def test_strict_schema_requires_all_properties_and_request_is_bounded():
    original = AgentExtraction.model_json_schema()
    schema = strict_schema(original)
    # The actual vendor rejected the union nested in expression, including
    # recursive children under $defs. Test the real model schema, not a toy one.
    assert "oneOf" in original["$defs"]["AgentCandidate"]["properties"]["expression"]
    union = schema["$defs"]["AgentCandidate"]["properties"]["expression"]["anyOf"]
    assert {branch["$ref"] for branch in union} == {
        "#/$defs/ConceptExpr",
        "#/$defs/ConstraintExpr",
        "#/$defs/BooleanExpr",
    }
    assert schema["$defs"]["BooleanExpr"]["properties"]["children"]["items"]["anyOf"] == union
    assert schema["$defs"]["ConceptExpr"]["properties"]["type"]["const"] == "concept"
    assert schema["$defs"]["ConstraintExpr"]["properties"]["type"]["const"] == "constraint"

    def check(value):
        if isinstance(value, dict):
            assert "oneOf" not in value and "discriminator" not in value
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for v in value.values():
                check(v)
        elif isinstance(value, list):
            for v in value:
                check(v)

    check(schema)
    request = request_for(snapshot(), {"model": "gpt-6.1-sol", "max_output_tokens": 2048})
    assert request.reasoning_effort == "low" and request.max_output_tokens == 2048
    assert request.response_schema == schema
    AgentExtraction.model_validate(candidate("熟悉 Python 或 Go"))
