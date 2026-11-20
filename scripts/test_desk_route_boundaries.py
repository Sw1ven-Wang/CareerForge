"""Isolated actual desk ingress: rule misrouting, without remote model calls."""

import asyncio
import json
from pathlib import Path

import httpx

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.local_web import create_local_web


async def main():
    root = Path("private/local-validation/agent-routing-20261116/desk-boundary-runtime")
    root.mkdir(parents=True, exist_ok=True)
    result = root.parent / "desk-boundaries.json"
    if result.exists():
        print("Existing completed boundary report retained")
        return
    app = Application(
        Settings(
            mode="development",
            database_url="sqlite+pysqlite:///" + str((root / "test.db").resolve()),
            state_dir=root,
        )
    )
    owner = app.bootstrap("route-boundary-test")
    (root / "client.json").write_text(json.dumps(owner), encoding="utf-8")
    rows = []
    web = create_local_web(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web),
        base_url="http://127.0.0.1:8001",
        headers={"X-CareerForge-Desk": "1"},
    ) as c:
        for i, q in enumerate(
            [
                "请解释岗位要求中的 RAG 是什么。",
                "岗位要求是如何提取成技术需求的？",
                "请解释字符串“岗位职责”的含义，不要导入。",
            ]
        ):
            r = await c.post(
                "/desk/messages",
                json={"question": q, "request_id": f"boundary-{i}", "use_model": False},
            )
            body = r.json()
            detail = (
                (await c.get("/desk/conversations/" + body["conversation_id"])).json()
                if body.get("conversation_id")
                else None
            )
            rows.append(
                {"question": q, "http_status": r.status_code, "job": body, "conversation": detail}
            )
            print(q, r.status_code, body.get("reply"), flush=True)
    result.write_text(
        json.dumps({"status": "completed", "cases": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    app.db.engine.dispose()


asyncio.run(main())
