from uuid import UUID

import pytest
from careerforge.config.settings import get_settings
from careerforge.infrastructure.db.models import (
    AgentRun,
    MarketReport,
    ModelCallReceipt,
    StageArtifact,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, Usage
from careerforge.workflows.market_agent import execute
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure


@pytest.mark.parametrize("invalid_quote", [False, True])
def test_real_market_report_citations_and_accounting(clients, monkeypatch, invalid_quote):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-placeholder"))
    a, b = clients
    target = goal(a)
    imported = post(
        a,
        "/job-data/import",
        {"title": "Agent 实习", "raw_payload": "任职要求：熟悉 Python 或 Go。"},
    ).json()["data"]
    response = post(
        a,
        "/market-research-runs",
        {
            "goal_version_id": target,
            "source_snapshot_ids": [imported["source_snapshot_id"]],
            "analysis_mode": "real_agent",
        },
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
                        "quote": "不存在的原文" if invalid_quote else "熟悉 Python 或 Go",
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
        assert run.status == ("failed" if invalid_quote else "succeeded")
        assert run.calls_reserved == 1
        receipt = db.scalar(select(ModelCallReceipt).where(ModelCallReceipt.run_id == run_id))
        assert receipt.status == "completed"
        result = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        report = db.get(MarketReport, UUID(result.payload["market_report_id"]))
        rid = str(report.id)
    response = a.get(f"/api/v1/market-reports/{rid}")
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    analysis = report["agent_analysis"]
    assert analysis["candidate_count"] == (0 if invalid_quote else 1)
    assert report["job_contract"]["report"]["denominator"] == 0
    assert analysis["model"] == "gpt-6.1-sol"
    if not invalid_quote:
        fact = analysis["jobs"][0]["facts"][0]
        assert fact["review_status"] == "auto_extracted"
        assert fact["evidence"]["quote"] == "熟悉 Python 或 Go"
    else:
        assert analysis["jobs"][0]["error"] == "invalid_candidate"
    assert b.get(f"/api/v1/market-reports/{rid}").status_code == 404
