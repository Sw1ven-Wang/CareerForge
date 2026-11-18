from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from careerforge.contracts_research_v163 import ResearchAnalysisOutput
from careerforge.errors import DomainError
from careerforge.services.analysis_anchor_correction import (
    ANCHOR_CORRECTION,
    call_with_anchor_correction,
)


def test_patch_cannot_rewrite_original_claims_or_assessment_verdict():
    from careerforge.services.analysis_anchor_correction import AnchorPatches, apply_anchor_patches
    original = {"units": [{"claims": [{"key": "real", "statement": "immutable source fact"}]}],
                "task_coverage": {"question": {"status": "partial", "explanation": "unchanged", "anchors": []}}}
    patches = AnchorPatches.model_validate({"patches": [{"assessment_index": 0, "anchors": [{"subject_key": "unit", "claim_keys": ["real"]}]}]})
    result = apply_anchor_patches(original, patches)
    assert result["units"] == original["units"]
    assert result["task_coverage"]["question"]["status"] == "partial"
    assert original["task_coverage"]["question"]["anchors"] == []
    with pytest.raises(ValidationError):
        AnchorPatches.model_validate({"patches": [], "units": []})
    with pytest.raises(DomainError):
        apply_anchor_patches(original, AnchorPatches.model_validate({"patches": [{"assessment_index": 9, "anchors": []}]}))


@pytest.mark.parametrize("changed_claim", [False, True])
async def test_one_metered_correction_preserves_claims(changed_claim):
    original = {"units": [{"subject_key": "unit", "claims": [{"key": "real", "statement": "original"}]}],
                "task_coverage": {"question": {"status": "partial", "anchors": [{"subject_key": "unit", "claim_keys": ["wrong"]}]}}}
    corrected = deepcopy(original)
    corrected["task_coverage"]["question"]["anchors"][0]["claim_keys"] = ["real"]
    if changed_claim:
        corrected["units"][0]["claims"][0]["statement"] = "invented"
    error = DomainError("MODEL_SCHEMA", "invalid")
    error.__cause__ = ValidationError.from_exception_data("ResearchAnalysisOutput", [{
        "type": "value_error", "loc": (), "input": original,
        "ctx": {"error": ValueError("Research coverage references an unknown candidate claim")},
    }])
    gateway = SimpleNamespace(call=AsyncMock(side_effect=[error, SimpleNamespace(structured=corrected)]))
    receipt = SimpleNamespace(status="completed", response={"structured": original})
    db = SimpleNamespace(scalar=lambda _: receipt)
    build = SimpleNamespace(gateway=gateway, runtime=SimpleNamespace(db=SimpleNamespace(read=lambda: nullcontext(db))))
    run = SimpleNamespace(id="run", input_manifest={"analysis_anchor_correction": ANCHOR_CORRECTION})
    call = call_with_anchor_correction(build, run, 1, "worker", "analysis.final:source", "analysis", "system", {}, ResearchAnalysisOutput)
    if changed_claim:
        with pytest.raises(DomainError, match="原事实"):
            await call
    else:
        assert (await call).structured == corrected
    assert gateway.call.await_count == 2
    assert gateway.call.call_args.args[3].endswith(":anchor-correction:1")
    assert original["units"][0]["claims"][0]["statement"] == "original"


@pytest.mark.parametrize("code", ["MODEL_HTTP_ERROR", "MODEL_RESULT_UNCERTAIN", "MODEL_SCHEMA"])
async def test_does_not_retry_transport_or_other_schema_errors(code):
    gateway = SimpleNamespace(call=AsyncMock(side_effect=DomainError(code, "blocked")))
    build = SimpleNamespace(gateway=gateway)
    run = SimpleNamespace(id="run", input_manifest={"analysis_anchor_correction": ANCHOR_CORRECTION})
    with pytest.raises(DomainError):
        await call_with_anchor_correction(build, run, 1, "worker", "key", "analysis", "system", {}, ResearchAnalysisOutput)
    assert gateway.call.await_count == 1
