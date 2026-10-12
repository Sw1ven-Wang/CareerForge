import runpy
from pathlib import Path


def test_strict_scoring_rejects_wrong_category_duplicate_and_invalid_quote():
    score = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "evals/runners/requirement_extraction.py")
    )["score"]
    gold = {
        "source_snapshot_id": "a",
        "normalized_skill": "python",
        "requirement_type": "must",
        "origin": "explicit",
        "logical_group": None,
        "char_start": 0,
        "char_end": 6,
        "text": "Python",
    }
    output = [gold, gold, {**gold, "requirement_type": "preferred"}, {**gold, "char_end": 99}]
    result = score(output, [gold], {"a": "Python 编程"})
    assert result["precision"] == 0.25
    assert result["recall"] == 1
    assert result["invalid_citations"] == 1
    assert score([], [], {})["precision"] is None
