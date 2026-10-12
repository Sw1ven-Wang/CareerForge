"""Bounded anonymous reads with checked redirects, robots and a durable audit hook."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from careerforge.infrastructure.collection.portals import identify

USER_AGENT = "CareerForgeResearch/1.0"
MAX_BYTES = 1000000


class CollectionError(Exception):
    def __init__(self, code, message, url=None):
        super().__init__(message)
        self.code, self.message, self.url = code, message, url


@dataclass
class Page:
    url: str
    raw: bytes
    text: str
    status: int
    content_type: str


def robots_allowed(text, url):
    groups, agents, rules = [], [], []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        if key.lower() == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key.lower() in {"allow", "disallow"} and agents:
            rules.append((key.lower(), value))
    if agents:
        groups.append((agents, rules))
    specific = [(a, rs) for a, rs in groups if any(x != "*" and x in USER_AGENT.lower() for x in a)]
    active = specific or [(a, rs) for a, rs in groups if "*" in a]
    parsed = urlparse(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    matches = []
    for _, entries in active:
        for kind, pattern in entries:
            if not pattern:
                continue
            end = pattern.endswith("$")
            expression = (
                "^"
                + re.escape(pattern[:-1] if end else pattern).replace(r"\*", ".*")
                + ("$" if end else "")
            )
            if re.search(expression, target):
                matches.append((len(pattern.replace("*", "")), kind == "allow"))
    return max(matches, default=(0, True))[1]


class PublicWeb:
    def __init__(self, reserve, client=None):
        self.reserve = reserve
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), trust_env=False, follow_redirects=False
        )
        self.owns_client = client is None
        self.robots = {}
        self.trace = []
        self.last_access = {}
        self.denied_hosts = set()

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def _validate(self, url):
        identify(url)
        host = urlparse(url).hostname
        if host in self.denied_hosts:
            raise CollectionError("host_stopped", "该站点本轮已受限，停止重复访问。", url)
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM
            )
        except OSError:
            raise CollectionError("dns_error", "招聘域名暂时无法解析。", url) from None
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise CollectionError("unsafe_address", "招聘域名解析到非公网地址。", url)

    async def _request(self, url, maximum=MAX_BYTES):
        for _ in range(4):
            await self._validate(url)
            host = urlparse(url).hostname
            now = asyncio.get_running_loop().time()
            wait = 0.8 - (now - self.last_access.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self.reserve(url, "GET")
            self.last_access[host] = asyncio.get_running_loop().time()
            try:
                async with self.client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/json",
                        "Cookie": "",
                    },
                ) as response:
                    self.trace.append({"url": url, "status": response.status_code})
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers.get("location", ""))
                        # Never fetch a redirected path before checking its robots.
                        if urlparse(target).netloc != urlparse(url).netloc:
                            raise CollectionError(
                                "redirected_origin",
                                "转到其他站点，请核对新地址后直接采集。",
                                target,
                            )
                        origin = "https://" + urlparse(url).netloc
                        if origin in self.robots and not robots_allowed(
                            self.robots[origin], target
                        ):
                            raise CollectionError(
                                "robots_denied", "最终页面不允许自动读取。", target
                            )
                        url = target
                        continue
                    if response.status_code in {401, 403, 429}:
                        self.denied_hosts.add(host)
                        raise CollectionError(
                            "access_restricted", "站点要求登录、验证或限制访问；停止本轮访问。", url
                        )
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > maximum:
                            raise CollectionError(
                                "response_too_large", "页面超过采集大小限制，未保存截断正文。", url
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    return Page(
                        url,
                        raw,
                        raw.decode(response.encoding or "utf-8", errors="replace"),
                        response.status_code,
                        response.headers.get("content-type", ""),
                    )
            except httpx.TimeoutException:
                raise CollectionError("timeout", "站点读取超时，本轮不自动重试。", url) from None
            except httpx.RequestError:
                raise CollectionError("network_error", "公开页面暂时无法访问。", url) from None
        raise CollectionError("redirect_limit", "站点重定向次数超限。", url)

    async def fetch(self, url):
        await self._validate(url)
        origin = "https://" + urlparse(url).netloc
        if origin not in self.robots:
            page = await self._request(origin + "/robots.txt", 65536)
            if (
                page.status in {404, 410}
                or "<html" in page.text[:500].lower()
                or "<!doctype" in page.text[:500].lower()
            ):
                self.robots[origin] = ""
            elif page.status >= 400:
                raise CollectionError("robots_unavailable", "无法确认站点抓取规则。", url)
            else:
                self.robots[origin] = page.text
        if not robots_allowed(self.robots[origin], url):
            raise CollectionError(
                "robots_denied", "robots 不允许自动读取该路径，请在浏览器查看后手动导入。", url
            )
        page = await self._request(url)
        if page.status >= 400:
            raise CollectionError("http_error", "岗位页面返回错误状态。", url)
        return page
