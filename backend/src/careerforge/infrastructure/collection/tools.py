"""Shared search/read tools for manual collection and the model-controlled agent."""

import json
from urllib.parse import quote

import httpx

from careerforge.config.settings import get_settings
from careerforge.infrastructure.collection.extractors import extract_job, tencent_detail_url
from careerforge.infrastructure.collection.network import CollectionError
from careerforge.infrastructure.collection.portals import PORTALS, identify


async def search_jobs(web, channel, query, limit, allow_search_api=False):
    portal = PORTALS[channel]
    browser_url = portal[3].format(query=quote(query))
    if channel == "tencent":
        url = (
            "https://careers.tencent.com/tencentcareer/api/post/Query?keyword="
            + quote(query)
            + "&pageIndex=1&pageSize="
            + str(limit)
            + "&language=zh-cn"
        )
        page = await web.fetch(url)
        try:
            value = json.loads(page.text)
            if value.get("Code") != 200:
                raise ValueError()
            posts = (value.get("Data") or {}).get("Posts") or []
            candidates = [
                {
                    "title": p["RecruitPostName"],
                    "url": "https://careers.tencent.com/jobdesc.html?postId=" + str(p["PostId"]),
                }
                for p in posts[:limit]
            ]
        except (ValueError, KeyError, TypeError):
            raise CollectionError("schema_changed", "腾讯公开查询结构已变化。", url) from None
        return {
            "status": "searched",
            "channel": channel,
            "query": query,
            "candidates": candidates,
            "browser_url": browser_url,
        }
    if allow_search_api:
        settings = get_settings()
        key = settings.tavily_api_key.get_secret_value()
        if not key:
            raise CollectionError("search_unconfigured", "未配置跨站搜索服务。")
        web.reserve("https://api.tavily.com/search", "POST")
        try:
            async with httpx.AsyncClient(
                timeout=20, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "query": query + " 招聘",
                        "include_domains": portal[2],
                        "include_domains_mode": "restrict",
                        "max_results": min(limit, 10),
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_usage": True,
                    },
                )
                if response.is_error or response.is_redirect:
                    raise CollectionError("search_service_error", "跨站搜索服务未成功返回结果。")
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CollectionError(
                "search_service_error", "跨站搜索服务暂不可用，本轮不自动重试。"
            ) from None
        candidates = []
        for item in payload.get("results", []):
            try:
                if identify(item["url"]) == channel:
                    candidates.append(
                        {"title": str(item.get("title", ""))[:500], "url": item["url"]}
                    )
            except (ValueError, KeyError):
                continue
        return {
            "status": "searched",
            "channel": channel,
            "query": query,
            "candidates": candidates,
            "browser_url": browser_url,
            "search_usage": payload.get("usage"),
            "snippet_is_evidence": False,
        }
    # A real limitation rather than silently querying disallowed paths or inventing matches.
    return {
        "status": "browser_search_required",
        "channel": channel,
        "query": query,
        "candidates": [],
        "browser_url": browser_url,
        "message": "请在浏览器查询后粘贴详情链接，或配置跨站搜索服务。",
    }


async def read_job(web, url):
    channel = identify(url)
    effective = tencent_detail_url(url) if channel == "tencent" else url
    page = await web.fetch(effective)
    return extract_job(channel, page, url), page
