import pytest
from pydantic import ValidationError

from careerforge.contracts import MechanismReadTarget
from careerforge.contracts_scope_binding_v166 import EvidenceRangeRequest, evidence_request_windows


@pytest.mark.parametrize("start,end", [(111,160),(196,280),(245,275),(1,120),(10,10)])
def test_proposal_is_paginated_without_lost_or_extra_lines(start, end):
    request = EvidenceRangeRequest.model_validate({"issue_index": 1, "target": {
        "path": "src/agent.ts", "symbol": "runLoop", "purpose": "dispatch",
        "start_line": start, "end_line": end}})
    windows = evidence_request_windows(request)
    lines = []
    for window in windows:
        MechanismReadTarget.model_validate(window)
        lines.extend(range(window["start_line"], window["end_line"]+1))
    assert lines == list(range(start,end+1))


@pytest.mark.parametrize("start,end", [(1,121),(10,9),(0,4)])
def test_invalid_or_unbounded_proposal_fails(start,end):
    with pytest.raises(ValidationError):
        EvidenceRangeRequest.model_validate({"issue_index": 1, "target": {
            "path": "src/agent.ts", "symbol": "runLoop", "purpose": "dispatch",
            "start_line": start, "end_line": end}})
