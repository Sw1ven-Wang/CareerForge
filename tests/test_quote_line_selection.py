from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from careerforge.errors import DomainError
from careerforge.services.quote_correction import (
    QuoteSpans,
    extract_quote_spans,
    numbered_reads,
    span_schema,
)


def sample(content="first\r\n  exact code\r\nlast"):
    return {
        "reads": [
            {
                "read_id": "own-read",
                "path": "a.ts",
                "start_line": 11,
                "end_line": 13,
                "content": content,
            }
        ]
    }


def spans(start=12, end=12, read="own-read", pointer="/ref"):
    return QuoteSpans.model_validate(
        {"patches": [{"pointer": pointer, "read_id": read, "start_line": start, "end_line": end}]}
    )


def test_extract_preserves_whitespace_crlf_and_immutable_report():
    original = {"ref": {"read_id": "own-read", "quote": "wrong"}, "claim_id": "immutable"}
    result = extract_quote_spans(original, spans(), ["/ref"], sample())
    assert result["ref"]["quote"] == "  exact code\r\n"
    assert result["claim_id"] == "immutable" and original["ref"]["quote"] == "wrong"
    assert numbered_reads(sample())[0]["lines"][1]["line"] == 12


@pytest.mark.parametrize(
    "start,end,read,pointer",
    [
        (10, 12, "own-read", "/ref"),
        (12, 14, "own-read", "/ref"),
        (13, 12, "own-read", "/ref"),
        (11, 12, "analysis-read", "/ref"),
        (11, 12, "own-read", "/claim_id"),
    ],
)
def test_selection_fails_closed_on_scope_violation(start, end, read, pointer):
    with pytest.raises(DomainError):
        extract_quote_spans(
            {"ref": {"read_id": "own-read", "quote": "wrong"}},
            spans(start, end, read, pointer),
            ["/ref"],
            sample(),
        )


def test_no_model_quote_field_and_no_duplicate_or_missing_patches():
    schema = span_schema(["/ref"], sample())
    valid = spans().model_dump(mode="json")
    assert Draft202012Validator(schema).is_valid(valid)
    bad = deepcopy(valid)
    bad["patches"][0]["quote"] = "invented"
    assert not Draft202012Validator(schema).is_valid(bad)
    for patches in [[], valid["patches"] * 2]:
        with pytest.raises(DomainError):
            extract_quote_spans(
                {"ref": {"read_id": "own-read", "quote": "wrong"}},
                QuoteSpans.model_validate({"patches": patches}),
                ["/ref"],
                sample(),
            )


def test_duplicate_excerpt_and_unsupported_newline_rejected():
    for content in ["same\nsame\nsame\n", "first\rexact\rlast"]:
        with pytest.raises(DomainError):
            extract_quote_spans(
                {"ref": {"read_id": "own-read", "quote": "wrong"}},
                spans(),
                ["/ref"],
                sample(content),
            )
