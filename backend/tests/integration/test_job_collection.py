import hashlib
import json
from uuid import UUID, uuid4

import httpx
import pytest
from careerforge.config.settings import get_settings
from careerforge.domain.job_normalization import sections
from careerforge.infrastructure.collection.network import PublicWeb, robots_allowed
from careerforge.infrastructure.db.models import JobContractArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelResult, ToolCall, Usage
from careerforge.workflows.job_collection import execute
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import post
from test_owner_scope import clients as clients

pytestmark = pytest.mark.infrastructure
URL = "https://careers.tencent.com/jobdesc.html?postId=123456"
RAW = json.dumps(
    {
        "Code": 200,
        "Data": {
            "RecruitPostName": "Agent工程师",
            "PostId": "123456",
            "Responsibility": "开发可追溯的 Agent 工作流及检索系统。" * 4,
            "Requirement": "熟悉 Python、工具调用和数据库，具备故障恢复及工程交付经验。" * 4,
            "LocationName": "深圳",
            "LastUpdateTime": "2026-10-12 09:00:00",
        },
    },
    ensure_ascii=False,
).encode()


def test_robots_query_and_allow_precedence():
    assert sections("职责要求\n熟悉 Python")[1]["kind"] == "qualification"
    rules = "User-agent: *\nDisallow: /*?*\nDisallow: /private/\nAllow: /private/public$"
    assert not robots_allowed(rules, "https://www.liepin.com/jobs?query=Agent")
    assert robots_allowed(rules, "https://www.liepin.com/job/123.shtml")
    assert robots_allowed(rules, "https://www.liepin.com/private/public")
    assert not robots_allowed(rules, "https://www.liepin.com/private/public/extra")


@pytest.mark.parametrize("mode", ["manual", "agent"])
def test_collection_raw_evidence_owner_scope_and_duplicate_delivery(clients, monkeypatch, mode):
    settings = get_settings()
    monkeypatch.setattr(settings, "real_agent_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", SecretStr("offline-placeholder"))
    requests, calls = [], []

    def transport(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path.endswith("/Query"):
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "Data": {"Posts": [{"RecruitPostName": "Agent工程师", "PostId": "123456"}]},
                },
            )
        assert request.url.path.endswith("/ByPostId")
        return httpx.Response(200, content=RAW, headers={"content-type": "application/json"})

    original = PublicWeb.__init__

    def init(self, reserve, client=None):
        original(self, reserve, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.owns_client = True

    async def validate(self, url):
        return None

    monkeypatch.setattr(PublicWeb, "__init__", init)
    monkeypatch.setattr(PublicWeb, "_validate", validate)

    async def complete(self, request):
        calls.append(request)
        name = "search_jobs" if len(calls) == 1 else "read_job"
        args = {"channel": "tencent", "query": "Agent"} if len(calls) == 1 else {"url": URL}
        return ModelResult(
            provider="offline_transport",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id=str(uuid4()), name=name, arguments=args)],
            usage=Usage(input_tokens=10, output_tokens=20, status="known"),
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", complete)
    a, b = clients
    response = post(
        a,
        "/collection/runs",
        {
            "channels": ["tencent"],
            "mode": mode,
            "allow_model_calls": mode == "agent",
            "model_call_limit": 2,
            "limit": 1,
            "urls": [URL] if mode == "manual" else [],
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["data"]["run_id"])
    execute(run_id, "offline-collection")
    execute(run_id, "duplicate-delivery")
    result = a.get(f"/api/v1/collection/runs/{run_id}").json()["data"]
    assert result["status"] == "succeeded", result
    saved = [o for o in result["observations"] if o["status"] == "collected"]
    assert len(saved) == 1 and saved[0]["raw_sha256"] == hashlib.sha256(RAW).hexdigest()
    assert len(requests) == (2 if mode == "manual" else 3)
    assert len(calls) == (0 if mode == "manual" else 2)
    assert result["model_calls"] == len(calls)
    assert b.get(f"/api/v1/collection/runs/{run_id}").status_code == 404
    assert b.get(f"/api/v1/sources/{saved[0]['source_id']}").status_code == 404
    with Session(get_engine()) as db:
        capture = db.scalar(
            select(JobContractArtifact).where(
                JobContractArtifact.source_id == UUID(saved[0]["source_id"]),
                JobContractArtifact.kind == "capture",
            )
        )
        assert capture.raw_bytes == RAW
        assert capture.payload["provider"] == "tencent"
        assert capture.payload["capture_method"] == "anonymous_public_fetch"
        assert capture.payload["provider_metadata"]["original_updated_at"] == "2026-10-12 09:00:00"


@pytest.mark.asyncio
async def test_nowcoder_query_uses_public_form_and_honors_bounds(monkeypatch):
    from careerforge.infrastructure.collection.network import CollectionError
    from careerforge.infrastructure.collection.tools import search_jobs

    reserved = []

    def transport(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        assert request.method == "POST"
        assert request.url.path == "/np-api/u/job/search"
        assert b"query=Agent" in request.content and b"recruitType=1" in request.content
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "totalCount": 2,
                    "datas": [
                        {"id": 469430, "jobName": "AI agent开发工程师"},
                        {"id": 463573, "jobName": "Agent开发工程师"},
                    ],
                },
            },
        )

    async def validate(self, url):
        return None

    monkeypatch.setattr(PublicWeb, "_validate", validate)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        web = PublicWeb(lambda url, method: reserved.append((url, method)), client)
        result = await search_jobs(web, "nowcoder", "Agent", 1)
        assert result["scope"] == "campus" and len(result["candidates"]) == 1
        assert result["candidates"][0]["url"].endswith("/469430")
        assert [method for _, method in reserved] == ["GET", "POST"]
        with pytest.raises(CollectionError, match="只允许"):
            await web.fetch("https://www.nowcoder.com/account", form={"x": "y"})
