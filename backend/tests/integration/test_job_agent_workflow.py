from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients

from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import AgentRun, ModelCallReceipt, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, Usage
from careerforge.workflows.job_agent import execute

pytestmark = pytest.mark.infrastructure


def test_accounted_agent_workflow_stores_unconfirmed_candidates(clients, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-placeholder"))
    a, b = clients
    imported = post(
        a,
        "/job-data/import",
        {"title": "Agent 实习", "raw_payload": "任职要求：熟悉 Python 或 Go。"},
    ).json()["data"]
    response = post(
        a, "/job-data/extraction-runs", {"job_snapshot_id": imported["contract_artifact_id"]}
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["data"]["run_id"])
    calls = []

    async def complete(self, request):
        calls.append(request)
        return ModelResult(
            provider="mock_transport",
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=20, status="known"),
            structured={
                "candidates": [
                    {
                        "section_id": "sec-1",
                        "quote": "熟悉 Python 或 Go",
                        "importance": "required",
                        "requirement_kind": "technical_skill",
                        "expression": {
                            "type": "any_of",
                            "children": [
                                {
                                    "type": "concept",
                                    "concept_id": "technology.python",
                                    "concept_kind": "technology",
                                },
                                {
                                    "type": "concept",
                                    "concept_id": "technology.go",
                                    "concept_kind": "technology",
                                },
                            ],
                        },
                    }
                ],
                "unresolved": [],
            },
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    execute(run_id, "mock-agent-test")
    execute(run_id, "duplicate-delivery")
    assert len(calls) == 1 and calls[0].reasoning_effort == "low"
    with Session(get_engine()) as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "succeeded" and run.calls_reserved == 1
        receipt = db.scalar(select(ModelCallReceipt).where(ModelCallReceipt.run_id == run_id))
        assert receipt.status == "completed"
        result = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        ids = result.payload["requirement_ids"]
    assert len(ids) == 1
    fact = a.get(f"/api/v1/job-data/artifacts/{ids[0]}").json()["data"]
    assert fact["fact"]["review_status"] == "auto_extracted"
    assert fact["generation"]["model"] == "gpt-6.1-sol"
    assert b.get(f"/api/v1/job-data/artifacts/{ids[0]}").status_code == 404
