"""Real ASGI request/response contract; inference/search remain explicitly synthetic."""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
from conftest import publish_fixture

from careerforge import __version__
from careerforge.api import create_app


@pytest.mark.asyncio
async def test_api_full_source_to_sse_and_replay(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    api = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as c:
        me = await c.get("/api/v1/me")
        assert me.status_code == 200
        conv = (await c.post("/api/v1/conversations", json={})).json()
        body = {
            "question": "学习Tool Calling实现",
            "expected_revision": 1,
            "funding_id": env["funding"],
            "idempotency_key": "http-turn",
        }
        t = await c.post(f"/api/v1/conversations/{conv['conversation_id']}/turns", json=body)
        assert t.status_code == 202, t.text
        ids = t.json()
        await app.drain()
        n = len(env["model"].calls)
        duplicate = await c.post(
            f"/api/v1/conversations/{conv['conversation_id']}/turns", json=body
        )
        assert duplicate.json()["turn_id"] == ids["turn_id"]
        res = await c.get(f"/api/v1/turns/{ids['turn_id']}/events")
        assert (
            "answer.block.validated" in res.text
            and "answer.delta" in res.text
            and "answer.completed" in res.text
        )
        assert res.text.index("answer.block.validated") < res.text.index("answer.delta")
        replay = await c.get(
            f"/api/v1/turns/{ids['turn_id']}/events", headers={"Last-Event-ID": "1"}
        )
        assert len(env["model"].calls) == n
        assert "event: turn.accepted" not in replay.text
        sources = (await c.get("/api/v1/knowledge/publications/" + pub)).json()
        assert sources["citations"]
        correction = await c.post(
            "/api/v1/feedback",
            json={
                "turn_id": ids["turn_id"],
                "signal": "explicit_feedback",
                "content": "先看代码",
                "consent": True,
            },
        )
        assert correction.status_code == 201 and correction.json()["global_policy_changed"] is False
        metrics = await c.get("/metrics")
        assert "careerforge_http_seconds" in metrics.text


@pytest.mark.asyncio
async def test_http_auth_and_nonadmin_spending(env):
    api = create_app(env["app"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as c:
        assert (await c.get("/api/v1/me")).status_code == 401
        c.headers["Authorization"] = "Bearer " + env["owner"]["token"]
        user = await c.post("/api/v1/admin/users", json={"name": "api-reader"})
        assert user.status_code == 201
        u = user.json()
        c.headers["Authorization"] = "Bearer " + u["token"]
        assert (await c.get("/metrics")).status_code == 403
        denied = await c.post(
            "/api/v1/funding",
            json={"namespace_id": u["namespace_id"], "call_cap": 10, "confirm": "AUTHORIZE"},
        )
        assert denied.status_code == 403, denied.text
        malformed = await c.post("/api/v1/conversations", json={"owner_id": env["actor"]})
        assert malformed.status_code == 422
        missing = await c.get("/api/v1/runs/unknown")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_all_core_api_schemas_export(env):
    schema = create_app(env["app"]).openapi()
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    expected_version = project["project"]["version"]
    assert __version__ == expected_version
    assert schema["info"]["version"] == expected_version
    for path in [
        "/api/v1/conversations/{identifier}/turns",
        "/api/v1/evolution/candidates/{identifier}/evaluate",
        "/api/v1/commands/{identifier}/execute",
    ]:
        assert path in schema["paths"]
    assert schema["components"]["schemas"]["RetrievalTaskSpec"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_real_loopback_cli_chat_sources_and_feedback(env, tmp_path):
    """Real socket + subprocess Typer CLI + SSE; only external providers are fixtures."""
    import asyncio
    import os
    import socket
    import subprocess
    import sys
    import threading
    from pathlib import Path

    import uvicorn

    app = env["app"]
    await publish_fixture(env)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(app),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
            ws="none",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    state = tmp_path / "cli-state"
    state.mkdir()
    (state / "client.json").write_text(
        json.dumps({**env["owner"], "api_url": f"http://127.0.0.1:{port}"})
    )
    childenv = {
        **os.environ,
        "CAREERFORGE_STATE_DIR": str(state),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "NO_COLOR": "1",
    }
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        for args in [
            ["chat", "--once", "讲解Tool Calling", "--evidence-only"],
            ["sources"],
            ["tasks"],
        ]:
            p = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "careerforge.cli.main", *args],
                env=childenv,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert p.returncode == 0, p.stderr + p.stdout
            if args[0] == "chat":
                assert "返回传入的 value" in p.stdout, p.stdout
            assert env["owner"]["token"] not in p.stdout
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()
