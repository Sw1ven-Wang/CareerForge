import pytest
from test_quote_correction_v164 import (
    corrected_output,
    final,
    install_model,
    preflight,
    setup_analysis,
)

from careerforge.errors import DomainError


@pytest.mark.parametrize("enabled", [False, True])
async def test_missing_quote_requires_opt_in_and_actual_model_correction(env, enabled):
    fixture = setup_analysis(env, missing=enabled)
    fixture["output"]["units"][0]["claims"][0]["support"][0]["quote"] = "return invented_value"
    install_model(env, fixture)
    if not enabled:
        with pytest.raises(DomainError, match="不在本次已读原文"):
            await final(env, fixture)
    else:
        diagnostics = preflight(env, fixture)
        assert diagnostics["issues"][0]["match_count"] == 0
        assert diagnostics["issues"][0]["candidates"] == []
        result = await final(env, fixture)
        assert result.structured == corrected_output(fixture)
        assert preflight(env, fixture, result.structured)["issues"] == []


async def test_missing_quote_cannot_remain_unmatched_after_correction(env):
    fixture = setup_analysis(env, missing=True)
    fixture["output"]["units"][0]["claims"][0]["support"][0]["quote"] = "return invented_value"
    install_model(env, fixture, corrected=fixture["output"])
    with pytest.raises(DomainError):
        await final(env, fixture)
