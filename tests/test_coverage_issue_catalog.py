from types import SimpleNamespace

import pytest

from careerforge.errors import DomainError
from careerforge.services.coverage_issues import (
    coverage_issue_catalog,
    validate_local_issue_indices,
)


def review():
    return SimpleNamespace(
        blocking_issues=["missing source", "title", "topic", "unit", "facet"],
        scope_review=SimpleNamespace(
            issue_scopes=[
                SimpleNamespace(issue_index=i, scope=scope, target_ids=[f"target-{i}"])
                for i, scope in enumerate(["claim", "title", "topic_link", "unit", "facet"])
            ]
        ),
    )


def test_catalog_preserves_original_indices_types_and_reasons():
    catalog = coverage_issue_catalog(review())
    assert [r["original_issue_index"] for r in catalog["eligible_local_issues"]] == [2, 4]
    assert [r["scope"] for r in catalog["non_local_issues"]] == ["claim", "title", "unit"]
    assert catalog["eligible_local_issues"][1] == {
        "original_issue_index": 4,
        "scope": "facet",
        "target_ids": ["target-4"],
        "reason": "facet",
    }


@pytest.mark.parametrize("indices", [[], [2], [4], [4, 2]])
def test_valid_subsets_do_not_force_any_approval(indices):
    validate_local_issue_indices(review(), indices)


@pytest.mark.parametrize("indices", [[0], [1], [3], [99], [2, 2], [True], ["2"]])
def test_mixed_types_unknown_duplicate_or_renumbered_indices_fail(indices):
    with pytest.raises(DomainError) as error:
        validate_local_issue_indices(review(), indices)
    assert error.value.code == "SCOPED_REVIEW_BINDING"


def test_absent_scope_allows_only_empty_selection():
    empty = SimpleNamespace(scope_review=None, blocking_issues=[])
    assert coverage_issue_catalog(empty)["eligible_local_issues"] == []
    validate_local_issue_indices(empty, [])
    with pytest.raises(DomainError):
        validate_local_issue_indices(empty, [0])
