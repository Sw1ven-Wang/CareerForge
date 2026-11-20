import pytest

from careerforge.errors import DomainError
from careerforge.services.analysis_topic_completion import (
    TopicAdditions,
    apply_topics,
    missing_topics,
)


def test_only_missing_candidate_topics_can_be_added():
    original = {
        "units": [
            {
                "topics": [{"topic_key": "agent.loop", "relation": "illustrates"}],
                "facets": [{"topic_key": "agent.memory"}],
                "claims": [{"text": "immutable"}],
            }
        ]
    }
    patch = TopicAdditions.model_validate(
        {
            "additions": [
                {"unit_index": 0, "topic": {"topic_key": "agent.memory", "relation": "illustrates"}}
            ]
        }
    )
    result = apply_topics(original, patch)
    assert not missing_topics(result)
    assert result["units"][0]["claims"] == original["units"][0]["claims"]
    assert len(original["units"][0]["topics"]) == 1
    with pytest.raises(DomainError):
        apply_topics(result, patch)
    with pytest.raises(DomainError):
        apply_topics(original, TopicAdditions(additions=[]))
