from copy import deepcopy

import pytest
from pydantic import ValidationError

from careerforge.contracts_linked_analysis import LinkedAnalysisOutput


def payload():
    units = []
    for key, kind in [("arch", "architecture"), ("impl", "implementation")]:
        units.append(
            {
                "subject_key": key,
                "unit_type": kind,
                "title": key,
                "claims": [
                    {
                        "key": "claim",
                        "statement": "明确范围内的局部源码观察",
                        "support": [
                            {"read_id": "actual-read", "quote": "exact unique source quote"}
                        ],
                    }
                ],
                "topics": [{"topic_key": "agent_runtime", "relation": "illustrates"}],
            }
        )
    return {
        "units": units,
        "pairs": [
            {
                "architecture_subject_key": "arch",
                "implementation_subject_key": "impl",
                "implementation_language": "python",
            }
        ],
    }


def test_linked_drafts_require_exact_local_unit_roles_and_python():
    value = LinkedAnalysisOutput.model_validate(payload())
    assert value.pairs[0].implementation_language == "python"
    for field, replacement in [
        ("architecture_subject_key", "impl"),
        ("implementation_subject_key", "missing"),
        ("implementation_language", "typescript"),
    ]:
        broken = deepcopy(payload())
        broken["pairs"][0][field] = replacement
        with pytest.raises(ValidationError):
            LinkedAnalysisOutput.model_validate(broken)


def test_architecture_only_is_explicit_without_invented_python():
    value = payload()
    value["units"] = value["units"][:1]
    value["pairs"][0].update(implementation_subject_key=None, implementation_language=None)
    assert LinkedAnalysisOutput.model_validate(value).pairs[0].implementation_subject_key is None
    value["pairs"][0]["implementation_language"] = "python"
    with pytest.raises(ValidationError):
        LinkedAnalysisOutput.model_validate(value)


def test_unpaired_or_duplicate_units_cannot_be_silently_stored():
    value = payload()
    value["units"].append({**deepcopy(value["units"][0]), "subject_key": "orphan"})
    with pytest.raises(ValidationError):
        LinkedAnalysisOutput.model_validate(value)
    value = payload()
    value["pairs"].append(deepcopy(value["pairs"][0]))
    with pytest.raises(ValidationError):
        LinkedAnalysisOutput.model_validate(value)


def test_snapshot_paths_are_closed_before_paid_request():
    from careerforge.services.build import BuildService

    schema = BuildService._analysis_schema([], LinkedAnalysisOutput, source_paths=["src/core.py"])
    assert schema["$defs"]["UnitDraft"]["properties"]["dependency_paths"]["items"]["enum"] == [
        "src/core.py"
    ]


def test_static_preview_cannot_claim_runtime_observation():
    value = payload()
    value["units"][0]["claims"][0]["kind"] = "runtime_observation"
    with pytest.raises(ValidationError):
        LinkedAnalysisOutput.model_validate(value)


def test_preview_quote_correction_has_its_own_bounded_contract():
    from careerforge.errors import DomainError
    from careerforge.services.build_execution import QUOTE_CORRECTION_CONTRACT
    from careerforge.services.quote_correction import quote_correction_enabled

    manifest = {
        "linked_preview_contract": "linked-architecture-python-analysis-preview-v1",
        "quote_correction_contract": dict(QUOTE_CORRECTION_CONTRACT),
    }
    assert quote_correction_enabled(manifest)
    manifest["quote_correction_contract"]["max_attempts"] = 2
    with pytest.raises(DomainError):
        quote_correction_enabled(manifest)


def test_explicit_python_pair_schema_cannot_return_architecture_only():
    from careerforge.contracts_linked_analysis import FullyPairedAnalysisOutput

    assert FullyPairedAnalysisOutput.model_validate(payload()).pairs[0].implementation_language == "python"
    value = payload()
    value["units"] = value["units"][:1]
    value["pairs"][0].update(implementation_subject_key=None, implementation_language=None)
    with pytest.raises(ValidationError):
        FullyPairedAnalysisOutput.model_validate(value)
    # Architecture-only remains a valid choice for explicitly cross-language previews.
    assert LinkedAnalysisOutput.model_validate(value).pairs[0].implementation_subject_key is None


def test_explicit_python_schema_has_no_nullable_implementation_escape():
    from careerforge.contracts_linked_analysis import FullyPairedAnalysisOutput

    pair = FullyPairedAnalysisOutput.model_json_schema()["$defs"]["PythonKnowledgePair"]
    assert pair["properties"]["implementation_subject_key"]["type"] == "string"
    assert pair["properties"]["implementation_language"]["const"] == "python"


@pytest.mark.parametrize("architecture_only, required", [(True, True), (False, "yes")])
def test_python_pair_scope_conflicts_are_rejected_before_reading_or_billing(
    architecture_only, required
):
    import asyncio
    from types import SimpleNamespace

    from careerforge.errors import DomainError
    from careerforge.services.linked_analysis import CONTRACT, analyze_preview

    run = SimpleNamespace(
        principal_id="actor",
        input_manifest={
            "linked_preview_contract": CONTRACT,
            "source_ids": ["source"],
            "max_units": 4,
            "read_targets": [{}],
            "architecture_only": architecture_only,
            "require_python_pairs": required,
        },
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(analyze_preview(None, run, 1, "worker"))
    assert error.value.code == "LINKED_ANALYSIS_SCOPE"
