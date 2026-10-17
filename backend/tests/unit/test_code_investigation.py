"""Focused B-package checks; synthetic source is parsed/read, never imported or executed."""

import pytest
from careerforge.application.review_evidence import claims_for_review
from careerforge.application.study_validation import partial_review
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.code_index.investigation import CodeInvestigation
from careerforge.infrastructure.code_index.source_windows import excerpts
from careerforge.schemas.model_io import ModelResult, ToolCall
from careerforge.schemas.study import ReviewOutput
from careerforge.workflows.code_investigation import investigate

REQUIREMENTS = [
    {
        "key": "tools",
        "technology": "Agent",
        "keywords": ["agent", "tool", "execute_agent"],
        "depth": "熟悉",
    }
]
ENTRY = "def execute_agent(tool, arguments):\n    return persist(tool, arguments)\n"
FILES = {
    "a_entry.py": ENTRY,
    **{f"b_decoy{i}.py": f"def agent_tool_{i}():\n    return {i}\n" for i in range(5)},
    "z_storage.py": "def persist(tool, arguments):\n    return tool(**arguments)\n",
    "tests/test_entry.py": "def test_entry():\n    assert execute_agent(str, {}) is not None\n",
}


def test_same_six_read_budget_finds_related_implementation_and_tests():
    baseline = excerpts(FILES, REQUIREMENTS)
    session = CodeInvestigation(FILES, REQUIREMENTS)
    session.bootstrap()
    reading = session.result()
    assert len(baseline["code"]) <= 6 and len(reading["code"]) <= 6
    assert "z_storage.py" not in {r["path"] for r in baseline["code"]}
    assert {"z_storage.py", "tests/test_entry.py"} <= {r["path"] for r in reading["code"]}
    assert reading["investigation"]["test_reads"] >= 1
    assert reading["investigation"]["executed"] is False
    assert "not a resolved" in reading["investigation"]["support_scope"]


def test_discovered_scope_read_limits_and_tool_limits():
    session = CodeInvestigation(
        {f"unit{i}.py": "def unit():\n" + "    value = '" + "x" * 3000 + "'\n" for i in range(9)},
        REQUIREMENTS,
    )
    assert (
        session.tool("read_span", {"target_id": "outside-snapshot"})["status"]
        == "invalid_arguments"
    )
    hits = session.tool("find_symbol", {"query": "unit"})["results"]
    assert all(not item["code_read"] for item in hits)
    for item in hits[:6]:
        result = session.tool("read_span", {"target_id": item["target_id"]})
        assert result["status"] == "read"
        assert len(result["source"]["content"]) <= 2600
        assert result["source"]["end_line"] - result["source"]["start_line"] < 70
        assert result["source"]["truncated"]
    assert session.tool("read_span", {"target_id": hits[6]["target_id"]})["status"] == "read_budget"
    for _ in range(10):
        session.tool("find_symbol", {"query": "nothing"})
    assert session.tool_count == 12 and session.stop_reason == "tool_budget"
    original = "def raw():\r\n    return 42\r\n"
    raw = CodeInvestigation({"raw.py": original}, REQUIREMENTS)
    target = raw.tool("find_symbol", {"query": "raw"})["results"][0]
    assert (
        raw.tool("read_span", {"target_id": target["target_id"]})["source"]["content"] == original
    )
    with pytest.raises(BusinessError):
        CodeInvestigation({"../escape.py": ENTRY}, REQUIREMENTS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,choice,expected,count",
    [
        (1, "unused", "model_budget", 0),
        (3, "empty", "evidence_sufficient", 1),
        (3, "missing", "no_new_evidence", 1),
        (3, "unsupported", "unsupported", 1),
    ],
)
async def test_stop_reasons_reserve_final_call(limit, choice, expected, count):
    decisions = []

    async def decide(key, context, tools):
        decisions.append(key)
        calls = (
            []
            if choice == "empty"
            else [
                ToolCall(
                    id="choice",
                    name="find_symbol" if choice == "missing" else "finish_investigation",
                    arguments={"query": "definitely_absent"}
                    if choice == "missing"
                    else {"reason": "unsupported", "explanation": "Only dependency mentions"},
                )
            ]
        )
        return ModelResult(
            provider="offline",
            finish_reason="tool_calls" if calls else "stop",
            tool_calls=calls,
            synthetic=True,
        )

    reading = await investigate(FILES, REQUIREMENTS, limit, decide)
    assert len(decisions) == count
    assert reading["investigation"]["stop_reason"] == expected
    assert reading["investigation"]["final_call_reserved"] is True
    assert reading["investigation"]["decision_calls"] < limit


def test_overlapping_reads_counterevidence_and_false_support_stay_unverified():
    text = "def entry():\n    return tool(**arguments)\n# only in-memory state\n"
    reading = {
        "code": [
            {"path": "entry.py", "start_line": 1, "content": text},
            {"path": "entry.py", "start_line": 2, "content": text.split("\n", 1)[1]},
        ]
    }
    point = {
        "requirement_key": "tools",
        "path": "entry.py",
        "start_line": 99,
        "end_line": 99,
        "quote": "return tool(**arguments)",
        "explanation": "This survives process restarts",
    }
    output = ReviewOutput.model_validate(
        {
            "verdict": "strong",
            "summary": "model proposal",
            "technology_stack": ["Python"],
            "quality_notes": ["static"],
            "matches": [point],
            "counterevidence": [
                {
                    **point,
                    "quote": "# only in-memory state",
                    "explanation": "No persistence evidence",
                }
            ],
        }
    )
    output, matched, checked = partial_review(output, {"requirements": REQUIREMENTS}, reading, True)
    assert output.verdict == "uncertain" and matched[0]["start_line"] == 2
    report = claims_for_review(matched, checked["counterevidence"], "a" * 40)
    assert report["claims"][0]["counterevidence_refs"]
    assert report["claims"][0]["review_status"] == "needs_semantic_review"
    assert all(
        e["verification"]["semantic_support"]["status"] == "unreviewed" for e in report["evidence"]
    )
    assert all(e["verification"]["runtime"]["status"] == "not_observed" for e in report["evidence"])
    output, matched, _ = partial_review(
        output.model_copy(update={"verdict": "strong", "counterevidence": []}),
        {"requirements": REQUIREMENTS},
        {"code": []},
        True,
    )
    assert output.verdict == "uncertain" and not matched
