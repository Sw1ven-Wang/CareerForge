"""A-package offline regressions: coverage policy, honest evidence levels and source recall."""

from pathlib import Path

import pytest
from careerforge.application.coverage_service import (
    collection_policy,
    coverage,
    require_plan_coverage,
)
from careerforge.application.study_validation import review_matches, tutor_points
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.code_index.source_windows import excerpts
from careerforge.schemas.study import ReviewOutput, TutorOutput

REQ = {"key": "agent", "technology": "Agent", "keywords": ["agent"], "depth": "熟悉"}


def test_filename_cannot_move_body_anchor_to_file_tail():
    lines = ["# filler"] * 120
    lines[4:6] = ["def run_agent():", "    return 1"]
    code = "\n".join(lines)
    plain = excerpts({"core.py": code}, [REQ])["code"][0]
    named = excerpts({"agent.py": code}, [REQ])["code"][0]
    assert (plain["start_line"], plain["end_line"]) == (5, 6)
    assert (named["start_line"], named["end_line"]) == (5, 6)


def test_path_only_matches_are_a_map_not_arbitrary_code_evidence():
    result = excerpts({"agent.py": "def helper():\n    return 1\n"}, [REQ])
    assert not result["code"]
    assert result["repo_map"][0]["symbols"][0]["name"] == "helper"
    assert result["repo_map"][0]["code_read"] is False


def test_disjoint_windows_and_global_read_budget():
    lines = ["# blank"] * 200
    lines[4:6] = ["def agent_one():", "    return 1"]
    lines[170:172] = ["def agent_two():", "    return 2"]
    code = "\n".join(lines)
    result = excerpts({"agent.py": code}, [REQ])
    assert len(result["code"]) == 2
    a, b = sorted(result["code"], key=lambda w: w["start_line"])
    assert a["end_line"] < b["start_line"]
    many = excerpts({f"p{i}.py": code for i in range(10)}, [REQ])["code"]
    assert len(many) == 6
    assert all(w["end_line"] - w["start_line"] < 70 and len(w["content"]) <= 2600 for w in many)


def project(identifier, key="agent"):
    return {"id": identifier, "admitted": True, "highlights": [{"requirement_key": key}]}


def test_coverage_preference_requires_explicit_shortfall_confirmation():
    payload = {"target_count": 5, "collection_policy": "coverage"}
    projects = [project("a"), project("b")]
    with pytest.raises(BusinessError) as exc:
        require_plan_coverage(payload, [REQ], projects, {"agent"}, 2, False)
    assert exc.value.code == "COLLECTION_GAP_CONFIRMATION_REQUIRED"
    scope = require_plan_coverage(payload, [REQ], projects, {"agent"}, 2, True)
    assert scope["shortfall"] == 3 and scope["collection_gap_acknowledged"]


def test_explicit_and_legacy_count_requirements_cannot_be_bypassed():
    for payload in [{"target_count": 5}, {"target_count": 5, "collection_policy": "strict_count"}]:
        assert collection_policy(payload) == "strict_count"
        with pytest.raises(BusinessError) as exc:
            require_plan_coverage(payload, [REQ], [project("a")], {"agent"}, 1, True)
        assert exc.value.code == "PROJECT_TARGET_NOT_MET"


def test_five_unrelated_projects_do_not_satisfy_goal_coverage():
    projects = [project(str(i), "other") for i in range(5)]
    with pytest.raises(BusinessError) as exc:
        require_plan_coverage(
            {"target_count": 5, "collection_policy": "coverage"},
            [REQ],
            projects,
            {"agent"},
            5,
            True,
        )
    assert exc.value.code == "REQUIREMENT_COVERAGE_MISSING"
    report = coverage([REQ], projects)
    assert report["missing_keys"] == ["agent"] and report["semantic_verified"] == 0


def test_true_quote_cannot_certify_an_unrelated_semantic_claim():
    source = "await asyncio.gather(*tasks)"
    output = ReviewOutput(
        verdict="strong",
        summary="模型判断",
        technology_stack=["Python"],
        quality_notes=["静态"],
        matches=[
            {
                "requirement_key": "agent",
                "path": "agent.py",
                "start_line": 999,
                "end_line": 999,
                "quote": source,
                "explanation": "进程重启后可以恢复全部任务",
            }
        ],
    )
    match = review_matches(
        output,
        {"requirements": [REQ]},
        {"code": [{"path": "agent.py", "start_line": 5, "end_line": 5, "content": source}]},
    )[0]
    assert match["start_line"] == 5
    assert match["verification"]["location"]["status"] == "verified"
    assert match["verification"]["semantic_support"]["status"] == "unreviewed"
    assert match["verification"]["runtime"]["status"] == "not_observed"
    tutor = TutorOutput(
        status="grounded",
        blocks=[{"kind": "text", "text": "教学说明"}],
        points=[
            {
                "title": "模型解释",
                "citation_id": "read-1",
                "quote": source,
                "explanation": "这就是持久化恢复",
            }
        ],
    )
    point = tutor_points(
        tutor,
        [
            {
                "citation_id": "read-1",
                "quote": source,
                "start_line": 5,
                "path": "agent.py",
                "commit": "a" * 40,
            }
        ],
    )[0]
    assert point["verification"]["semantic_support"]["status"] == "unreviewed"


# Developer-selected known-symbol examples; not an independent mechanism-retrieval benchmark.
NAMED_LOCATIONS = [
    ("application/study_validation.py", "normalize_depths"),
    ("application/study_validation.py", "review_matches"),
    ("application/study_service.py", "start"),
    ("application/study_service.py", "recover_extraction"),
    ("application/knowledge_service.py", "search"),
    ("application/knowledge_service.py", "related"),
    ("application/repository_service.py", "code_citation"),
    ("application/studio_service.py", "command_argv"),
    ("application/run_service.py", "leased"),
    ("workflows/registry.py", "execute_registered"),
]


@pytest.mark.parametrize("path,name", NAMED_LOCATIONS)
def test_ten_curated_named_code_locations(path, name):
    root = Path(__file__).resolve().parents[3] / "backend/src/careerforge"
    content = (root / path).read_text(encoding="utf-8")
    needle = f"def {name}("
    anchor = next(i + 1 for i, line in enumerate(content.splitlines()) if needle in line)
    reading = excerpts({path: content}, [{"technology": needle, "keywords": []}])
    assert any(w["start_line"] <= anchor <= w["end_line"] for w in reading["code"])
