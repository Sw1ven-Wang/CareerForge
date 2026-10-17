"""Bounded GitHub public API research. No arbitrary host, credentials, execution or retry."""

import re
import time
from datetime import UTC, datetime
from typing import cast

import httpx
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json

NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")


def get(client: httpx.Client, path: str, params: Json | None = None) -> Json:
    try:
        with client.stream("GET", "https://api.github.com/" + path, params=params) as response:
            if response.status_code in {403, 429}:
                raise BusinessError(
                    429, "GITHUB_RATE_LIMIT", "GitHub 请求受限；本轮停止，请稍后重试。"
                )
            if response.status_code != 200:
                raise BusinessError(502, "GITHUB_RESEARCH_REJECTED", "GitHub 未返回可用公开资料。")
            data = bytearray()
            started = time.monotonic()
            for part in response.iter_bytes(65536):
                data.extend(part)
                if len(data) > 1_000_000 or time.monotonic() - started > 25:
                    raise BusinessError(413, "GITHUB_RESPONSE_LIMIT", "GitHub 响应超过读取上限。")
            import json

            return cast(Json, json.loads(data))
    except (httpx.HTTPError, ValueError) as exc:
        raise BusinessError(502, "GITHUB_RESEARCH_FAILED", "GitHub 查询失败，未自动重试。") from exc


def client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(15, connect=6),
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CareerForge-study",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )


def search(queries: list[Json]) -> Json:
    candidates, observations = {}, []
    with client() as http:
        for query in queries[:3]:
            data = get(
                http,
                "search/repositories",
                {"q": query["query"] + " archived:false fork:false", "per_page": 5},
            )
            observations.append(
                {
                    "query": query,
                    "incomplete_results": data.get("incomplete_results", True),
                    "total_count": data.get("total_count"),
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            )
            for item in data.get("items", [])[:5]:
                name = item.get("full_name", "")
                if (
                    not NAME.fullmatch(name)
                    or item.get("private")
                    or item.get("fork")
                    or item.get("archived")
                ):
                    continue
                if name in candidates:
                    continue
                candidates[name] = {
                    "full_name": name,
                    "github_url": "https://github.com/" + name,
                    "description": (item.get("description") or "")[:1500],
                    "stars": item.get("stargazers_count"),
                    "language": item.get("language"),
                    "license": (item.get("license") or {}).get("spdx_id"),
                    "pushed_at": item.get("pushed_at"),
                    "archived": False,
                    "fork": False,
                    "origin": "github_search",
                    "query": query,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
    return {"candidates": list(candidates.values())[:10], "observations": observations}


def pin(candidate: Json) -> Json:
    name = candidate["full_name"]
    if not NAME.fullmatch(name):
        raise BusinessError(422, "GITHUB_NAME_INVALID", "仓库名称无效。")
    with client() as http:
        metadata = get(http, "repos/" + name)
        branch = metadata.get("default_branch", "")
        from urllib.parse import quote

        value = get(http, "repos/" + name + "/commits/" + quote(branch, safe=""))
    sha = value.get("sha", "")
    if not SHA.fullmatch(sha):
        raise BusinessError(422, "GITHUB_COMMIT_INVALID", "无法固定仓库版本。")
    return {
        **candidate,
        "commit": sha,
        "archived": bool(metadata.get("archived")),
        "fork": bool(metadata.get("fork")),
        "license": (metadata.get("license") or {}).get("spdx_id"),
        "pushed_at": metadata.get("pushed_at"),
        "observed_at": datetime.now(UTC).isoformat(),
    }
