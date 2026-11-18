"""Readiness and cursor validation must fail closed without leaking DB errors."""

import httpx
import pytest

from careerforge.api import create_app
from careerforge.application import Application
from careerforge.config import Settings


@pytest.mark.asyncio
async def test_unmigrated_database_is_not_ready(tmp_path):
    service = Application(
        Settings(
            mode="development", database_url="sqlite+pysqlite:///" + str(tmp_path / "empty.db")
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            result = await client.get("/health/ready")
            assert result.status_code == 503
            assert result.json() == {"database": "unavailable_or_unmigrated"}
            assert str(tmp_path) not in result.text
    finally:
        service.db.engine.dispose()


@pytest.mark.asyncio
async def test_migrated_schema_ready_without_creating_account(tmp_path):
    service = Application(
        Settings(
            mode="development", database_url="sqlite+pysqlite:///" + str(tmp_path / "schema.db")
        )
    )
    service.db.create_schema()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
    finally:
        service.db.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query,header", [("?after=-1", None), ("", "-1"), ("", "abc"), ("", "+1")])
async def test_negative_or_malformed_sse_cursor_is_rejected(env, query, header):
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    if header is not None:
        headers["Last-Event-ID"] = header
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])),
        base_url="http://test",
        headers=headers,
    ) as client:
        result = await client.get("/api/v1/turns/missing/events" + query)
        assert result.status_code == 422
