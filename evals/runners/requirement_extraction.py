"""Offline scorer. Gold must be independently annotated; no quality threshold is implied."""

import argparse
import json
from pathlib import Path
from typing import Any


def score(
    predicted: list[dict[str, Any]], gold: list[dict[str, Any]], texts: dict[str, str]
) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            row.get(k)
            for k in (
                "source_snapshot_id",
                "normalized_skill",
                "requirement_type",
                "origin",
                "logical_group",
                "char_start",
                "char_end",
                "text",
            )
        )

    def valid(row: dict[str, Any]) -> bool:
        text = texts.get(row.get("source_snapshot_id", ""))
        start, end = row.get("char_start"), row.get("char_end")
        return (
            text is not None
            and type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(text)
            and text[start:end] == row.get("text")
        )

    expected = {key(r) for r in gold}
    accepted = {key(r) for r in predicted if valid(r)}
    correct = len(accepted & expected)
    return {
        "matching": "strict concept/category/origin/logical-group/support-span",
        "predicted": len(predicted),
        "gold_unique": len(expected),
        "true_positive": correct,
        "invalid_citations": sum(not valid(r) for r in predicted),
        "precision": correct / len(predicted) if predicted else None,
        "recall": correct / len(expected) if expected else None,
        "quality_gate": "not_calibrated",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path, help="JSON with predicted, gold and texts")
    args = parser.parse_args()
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    print(
        json.dumps(
            score(data["predicted"], data["gold"], data["texts"]), ensure_ascii=False, indent=2
        )
    )
