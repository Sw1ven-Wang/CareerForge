import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_subscription_bridge as bridge

from careerforge.contracts import ModelOutput


@pytest.mark.asyncio
async def test_bridge_validates_authentication_and_schema_without_invoking_cli(monkeypatch):
    calls = []

    async def complete(self, request):
        calls.append(request)
        return ModelOutput(text='{"ok":true}', structured={"ok": True}, usage={"input_tokens": 1})

    monkeypatch.setattr(bridge.WorkflowTerra, "complete", complete)
    with TestClient(bridge.bridge("x" * 32, "unused")) as client:
        body = {
            "model": bridge.MODEL,
            "messages": [{"role": "user", "content": "test"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": {"type": "object"}},
            },
        }
        assert client.post("/v1/chat/completions", json=body).status_code == 401
        assert not calls
        headers = {"Authorization": "Bearer " + "x" * 32}
        response = client.post("/v1/chat/completions", json=body, headers=headers)
        assert response.status_code == 200 and len(calls) == 1
        assert response.json()["choices"][0]["message"]["content"] == '{"ok":true}'
        assert (
            client.post(
                "/v1/chat/completions", json={**body, "stream": True}, headers=headers
            ).status_code
            == 422
        )
        assert len(calls) == 1
        assert client.get("/health").json()["effort"] == "medium"


def test_bridge_requires_a_nonempty_local_credential():
    with pytest.raises(ValueError):
        bridge.bridge("", "unused")


def test_subscription_subprocess_does_not_inherit_application_or_github_credentials(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CAREERFORGE_SUBSCRIPTION_BRIDGE_KEY",
    ):
        monkeypatch.setenv(name, "integration-test-secret")
    provider = bridge.CodexTerraProvider("unused", 1)
    assert not any(
        name in provider.env
        for name in (
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "CAREERFORGE_SUBSCRIPTION_BRIDGE_KEY",
        )
    )
