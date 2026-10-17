"""Observed public formats; never execute scripts or infer hidden job text."""

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

from careerforge.domain.job_normalization import adapt, canonical
from careerforge.infrastructure.collection.network import CollectionError, Page

ADAPTER_VERSION = "public-jobs-1.0"


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[list[Any]] = []
        self.headings: list[list[str]] = []
        self.script: list[Any] | None = None
        self.heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.script = [dict(attrs), ""]
        if tag in {"h1", "h2", "title"}:
            self.heading = [tag, ""]

    def handle_data(self, text: str) -> None:
        if self.script is not None:
            self.script[1] += text
        if self.heading is not None:
            self.heading[1] += text

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None:
            self.scripts.append(self.script)
            self.script = None
        if self.heading is not None and tag == self.heading[0]:
            self.headings.append(self.heading)
            self.heading = None


@dataclass
class CollectedJob:
    channel: str
    url: str
    title: str
    body: str
    employer: str | None = None
    locations: list[str] = field(default_factory=list)
    external_id: str | None = None
    salary: str | None = None
    provider_updated_at: str | None = None
    availability: str = "unknown"
    employment_type: str | None = None


def assigned_json(parser: PageParser, marker: str) -> Any:
    for _, body in parser.scripts:
        if marker in body:
            return json.JSONDecoder().raw_decode(body.split(marker, 1)[1].lstrip())[0]
    raise ValueError("Missing structured state")


def extract_job(channel: str, page: Page, source_url: str | None = None) -> CollectedJob:
    url = source_url or page.url
    if channel == "tencent" and "application/json" in page.content_type:
        value = json.loads(page.text)
        data = value.get("Data") or {}
        if value.get("Code") != 200 or not data.get("RecruitPostName"):
            raise CollectionError("job_unavailable", "官网未返回有效岗位详情。", url)
        body = (
            "岗位职责\n"
            + canonical(data.get("Responsibility", ""), True)
            + "\n岗位要求\n"
            + canonical(data.get("Requirement", ""), True)
        )
        return CollectedJob(
            channel,
            url,
            data["RecruitPostName"],
            body,
            "腾讯",
            [data["LocationName"]] if data.get("LocationName") else [],
            str(data["PostId"]),
            provider_updated_at=data.get("LastUpdateTime"),
        )
    parser = PageParser()
    parser.feed(page.text)
    if channel == "nowcoder" and re.search(r"/jobs/detail/\d+", url):
        try:
            state = assigned_json(parser, "window.__INITIAL_STATE__=")["store"]["jobDetail"]
            data = state["detail"]
            ext = json.loads(data["ext"]) if isinstance(data["ext"], str) else data["ext"]
            body = (
                "岗位职责\n"
                + canonical(ext.get("infos", ""), True)
                + "\n岗位要求\n"
                + canonical(ext.get("requirements", ""), True)
            )
            visible = canonical(page.text, True)
            return CollectedJob(
                channel,
                url,
                data["jobName"],
                body,
                state.get("jobCompany", {}).get("companyName"),
                data.get("jobCityList") or [],
                str(data["id"]),
                availability="closed_observed" if "已结束" in visible else "unknown",
                employment_type={1: "full_time", 2: "internship", 3: "full_time"}.get(
                    data.get("recruitType")
                ),
            )
        except (KeyError, ValueError, TypeError):
            raise CollectionError(
                "schema_changed", "牛客岗位结构已变化，需更新适配器。", url
            ) from None
    for attrs, body in parser.scripts:
        if attrs.get("type", "").lower() != "application/ld+json":
            continue
        try:
            meta, text, external, _, _ = adapt("json_ld", body)
        except (ValueError, KeyError, TypeError):
            continue
        if text and meta["title_raw"] != "标题待补":
            visible = canonical(page.text, True)
            return CollectedJob(
                channel,
                url,
                meta["title_raw"],
                text,
                meta["employer_name"],
                meta["locations"],
                external,
                meta["salary_raw"],
                availability="closed_observed" if "浏览原职位详情" in visible else "unknown",
                employment_type=meta["employment_type"],
            )
    if channel == "baidu" and "/jobs/detail/" in url:
        visible = canonical(page.text, True)
        start = visible.find("工作职责")
        end = visible.find("该岗位支持", start)
        text = visible[start : end if end > start else None]
        title = next(
            (body.strip() for tag, body in parser.headings if tag == "h1" and body.strip()), None
        )
        if not title:
            title = next(
                (
                    line
                    for line in reversed(visible[:start].splitlines())
                    if re.search(r"工程师|架构师|研究员|实习生|经理", line)
                ),
                None,
            )
        if title and start >= 0 and "职责要求" in text:
            return CollectedJob(
                channel, url, title, text, "百度", external_id=urlparse(url).path.rsplit("/", 1)[-1]
            )
    visible = canonical(page.text, True)
    if any(
        word in visible[:2500]
        for word in ["安全验证", "访问验证", "访问异常", "请完成验证", "页面找不到"]
    ):
        raise CollectionError("access_restricted", "站点返回验证或错误页，未获取岗位正文。", url)
    raise CollectionError(
        "browser_required", "页面没有可验证的单岗位正文，可能需要浏览器渲染或登录。", url
    )


def tencent_detail_url(url: str) -> str:
    post_id = parse_qs(urlparse(url).query).get("postId", [""])[0]
    if not post_id.isdigit():
        raise CollectionError("invalid_job_url", "腾讯岗位链接需要有效 postId。", url)
    return (
        "https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId="
        + post_id
        + "&language=zh-cn"
    )
