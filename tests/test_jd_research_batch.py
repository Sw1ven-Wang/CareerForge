from copy import deepcopy

import pytest

from careerforge.errors import DomainError
from careerforge.services.jd_research_batch import first_batch, validate_batch
from careerforge.util import fingerprint


def snapshot():
    return {
        "goal_version_id": "full-goal",
        "requirements": [
            {
                "requirement_id": f"r-{i}",
                "paragraph_id": f"p-{i // 12}",
                "span_start": i,
                "topic_key": f"topic.{i}",
            }
            for i in range(41)
        ],
    }


def test_batch_preserves_all_requirements_and_language_policy_without_mutating_goal():
    goal = snapshot()
    old = deepcopy(goal)
    profile = {"architecture_language_constraint": None, "backend_languages": ["python"]}
    batch = first_batch(goal, profile)
    assert len(validate_batch(goal, batch)) == 12
    assert set(batch["requirement_ids"]) | set(batch["remaining_requirement_ids"]) == {
        r["requirement_id"] for r in goal["requirements"]
    }
    assert not set(batch["requirement_ids"]) & set(batch["remaining_requirement_ids"])
    assert batch["language_profile"] == profile
    assert goal == old


@pytest.mark.parametrize("change", ["drop", "replace", "goal"])
def test_batch_rejects_changed_backlog_or_goal_even_if_hash_recomputed(change):
    goal = snapshot()
    batch = first_batch(goal)
    if change == "drop":
        batch["remaining_requirement_ids"].pop()
    elif change == "replace":
        batch["requirement_ids"][0] = "unknown"
    else:
        goal["goal_version_id"] = "another-goal"
    batch["batch_hash"] = fingerprint({k: v for k, v in batch.items() if k != "batch_hash"})
    with pytest.raises(DomainError):
        validate_batch(goal, batch)


def test_batch_respects_old_per_task_topic_and_requirement_limits():
    goal = snapshot()
    for row in goal["requirements"]:
        row["paragraph_id"] = "same"
    batch = first_batch(goal)
    assert len(batch["topic_keys"]) == 15
    assert len(batch["remaining_requirement_ids"]) == 26


def test_candidate_view_does_not_remove_candidates_or_truncate_source_evidence():
    from careerforge.services.candidate_selection import bounded_catalog_view

    entries = [
        {
            "candidate_id": "a",
            "source_reads": [{"read_id": "real", "content": "code" * 2000}],
            "observations": [
                {
                    "observation_id": "o",
                    "candidate_observations": [
                        {
                            "name": "org/a",
                            "url": "https://github.com/org/a",
                            "description": "metadata" * 1000,
                            "huge_provider_field": "unused" * 10000,
                        }
                    ],
                    "source_leads": [{"excerpt": "description" * 1000}],
                }
            ],
        }
    ]
    original = deepcopy(entries)
    view = bounded_catalog_view(entries)
    assert entries == original
    assert view[0]["candidate_id"] == "a"
    assert view[0]["source_reads"] == original[0]["source_reads"]
    assert view[0]["observations"][0]["observation_id"] == "o"
    assert view[0]["observations"][0]["source_leads"][0]["excerpt_truncated"]
