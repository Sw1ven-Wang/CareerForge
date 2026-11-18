from dataclasses import replace

import pytest
from pydantic import ValidationError

from careerforge.contracts import KnowledgeBuildInput, QueryPlan
from careerforge.services.research_query import (
    PUBLIC_CHANNELS,
    PublicQueryPlan,
    query_contract_context,
    query_plan_type,
)


def manifest(env, monkeypatch, token=""):
    app = env["app"]
    monkeypatch.setattr(app.repositories, "synthetic", False)
    app.db.settings = replace(app.settings, github_token=token)
    with app.db.transaction() as db:
        return app.build.prepare_in(
            db,
            env["actor"],
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=env["funding"],
                question="Tool Calling",
                topic_keys=["agent.tool_calling"],
                call_cap=20,
                max_projects=1,
                max_units_per_project=1,
                idempotency_key="cold-capabilities",
            ),
        )


def test_public_cold_plan_exposes_only_available_channels(env, monkeypatch):
    frozen = manifest(env, monkeypatch)
    assert frozen["research_discovery_contract"] == "repository-first-v1"
    assert query_plan_type(frozen) is PublicQueryPlan
    assert query_contract_context(frozen)["query_contract"]["allowed_channels"] == list(
        PUBLIC_CHANNELS
    )
    assert query_contract_context(frozen)["query_contract"]["code_search_available"] is False


@pytest.mark.parametrize("channel", ["code", "documentation"])
def test_unavailable_channel_rejected_without_rewriting(channel):
    value = {
        "queries": [
            {
                "channel": channel,
                "query": "repo:example/project",
                "question": "Where is the implementation?",
                "topic_keys": [],
            }
        ]
    }
    with pytest.raises(ValidationError):
        PublicQueryPlan.model_validate(value)
    assert value["queries"][0]["channel"] == channel


def test_authorized_and_historical_plans_retain_original_schema(env, monkeypatch):
    frozen = manifest(env, monkeypatch, token="test-only-not-a-real-credential")
    assert query_plan_type(frozen) is QueryPlan
    assert query_plan_type({}) is QueryPlan
    assert "code_search_available" not in query_contract_context(frozen)["query_contract"]
    assert "test-only-not-a-real-credential" not in str(frozen)
