"""Offline adapters over saved payloads. No network access or third-party execution."""

import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

NORMALIZER_VERSION = "job-normalizer-1.1"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def canonical(value: str, is_html=False) -> str:
    if is_html:
        parser = VisibleText()
        parser.feed(
            html.unescape(value) if re.search(r"&lt;/?(?:p|div|ul|li|br)\b", value) else value
        )
        value = "".join(parser.parts)
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def sections(text: str) -> list[dict[str, Any]]:
    result = []
    kind = "other"
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        if re.fullmatch(
            r"(?:优先条件|加分项|优先项|preferred qualifications|preferred|nice.to.have)[:：]?",
            body,
            re.I,
        ):
            kind = "preferred"
        elif re.search(r"任职|岗位要求|职责要求|资格|requirements|qualifications|必须", body, re.I):
            kind = "qualification"
        elif re.search(r"职责|responsibilities|what.you.ll.do", body, re.I):
            kind = "responsibility"
        elif re.search(r"福利|benefits|what.we.offer", body, re.I):
            kind = "benefit"
        if body:
            result.append(
                {
                    "section_id": f"sec-{len(result) + 1}",
                    "kind": kind,
                    "start": offset,
                    "end": offset + len(body),
                    "text": body,
                }
            )
        offset += len(line)
    return result


def aware(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.isoformat() if result.tzinfo else None


def jsonld_object(raw):
    if raw.lstrip().startswith(("{", "[")):
        values = [json.loads(raw)]
    else:
        values = []
        for match in re.finditer(
            r"<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script\s*>",
            raw,
            re.I | re.S,
        ):
            values.append(json.loads(match.group(1)))

    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if "JobPosting" in (types if isinstance(types, list) else [types]):
                yield value
            if "@graph" in value:
                yield from walk(value["@graph"])

    found = [job for value in values for job in walk(value)]
    if len(found) != 1:
        raise ValueError("JSON-LD must contain exactly one selected JobPosting")
    return found[0]


def adapt(provider, raw, title=None, employer=None):
    job = {
        "title_raw": title or "标题待补",
        "employer_name": employer,
        "role_family": None,
        "employment_type": None,
        "internship_type": None,
        "locations": [],
        "published_at": None,
        "provider_updated_at": None,
        "salary_raw": None,
        "published_at_semantics": "unknown",
    }
    external = None
    url = None
    text = raw
    metadata = {}
    if provider != "manual_import":
        payload = jsonld_object(raw) if provider == "json_ld" else json.loads(raw)
        if isinstance(payload, dict) and "jobs" in payload:
            payload = payload["jobs"]
        if isinstance(payload, list):
            if len(payload) != 1:
                raise ValueError("Select one posting from the provider response")
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ValueError("Posting payload must be an object")
        if provider == "greenhouse":
            job["title_raw"] = title or payload.get("title") or "标题待补"
            job["employer_name"] = employer or payload.get("company_name")
            job["locations"] = (
                [payload["location"]["name"]] if payload.get("location", {}).get("name") else []
            )
            text = canonical(payload.get("content", ""), True)
            external = str(payload["id"]) if payload.get("id") is not None else None
            url = payload.get("absolute_url")
            job["provider_updated_at"] = aware(payload.get("updated_at"))
            job["published_at"] = aware(payload.get("first_published"))
            job["published_at_semantics"] = "first_published" if job["published_at"] else "unknown"
            metadata = {
                "internal_job_id": payload.get("internal_job_id"),
                "external_id_semantics": "posting_id",
            }
        elif provider == "lever":
            job["title_raw"] = title or payload.get("text") or "标题待补"
            categories = payload.get("categories") or {}
            job["locations"] = list(
                dict.fromkeys(
                    ([categories["location"]] if categories.get("location") else [])
                    + payload.get("allLocations", [])
                )
            )
            job["employment_type"] = categories.get("commitment")
            text = canonical(
                payload.get("descriptionPlain") or payload.get("description") or "",
                not bool(payload.get("descriptionPlain")),
            )
            for block in payload.get("lists", []):
                visible = canonical(block.get("content", ""), True)
                if visible and visible not in text:
                    text += (
                        "\n" + (block.get("text", "") + "\n" if block.get("text") else "") + visible
                    )
            for key in ["additionalPlain", "closingPlain"]:
                if payload.get(key) and payload[key] not in text:
                    text += "\n" + payload[key]
            external = str(payload["id"]) if payload.get("id") is not None else None
            url = payload.get("hostedUrl")
            job["salary_raw"] = (
                json.dumps(payload["salaryRange"], ensure_ascii=False)
                if payload.get("salaryRange")
                else None
            )
        elif provider == "ashby":
            job["title_raw"] = title or payload.get("title") or "标题待补"
            job["locations"] = [payload["location"]] if payload.get("location") else []
            for loc in payload.get("secondaryLocations", []):
                name = loc.get("location") if isinstance(loc, dict) else loc
                if name and name not in job["locations"]:
                    job["locations"].append(name)
            text = canonical(
                payload.get("descriptionPlain") or payload.get("descriptionHtml") or "",
                not bool(payload.get("descriptionPlain")),
            )
            url = payload.get("jobUrl")
            job["published_at"] = aware(payload.get("publishedAt"))
            job["published_at_semantics"] = "last_published" if job["published_at"] else "unknown"
            job["employment_type"] = payload.get("employmentType")
            job["salary_raw"] = (
                json.dumps(payload["compensation"], ensure_ascii=False)
                if payload.get("compensation")
                else None
            )
            metadata = {"is_remote": payload.get("isRemote")}
        else:
            job["title_raw"] = title or payload.get("title") or "标题待补"
            org = payload.get("hiringOrganization") or {}
            job["employer_name"] = employer or (
                org.get("name") if isinstance(org, dict) else str(org)
            )
            text = canonical(payload.get("description", ""), True)
            for key, heading in [
                ("qualifications", "Qualifications"),
                ("responsibilities", "Responsibilities"),
                ("skills", "Requirements"),
            ]:
                value = payload.get(key)
                if isinstance(value, list):
                    value = "\n".join(str(x) for x in value)
                if value:
                    value = canonical(str(value), True)
                    if value not in text:
                        text += "\n" + heading + "\n" + value
            locations = payload.get("jobLocation") or []
            for loc in locations if isinstance(locations, list) else [locations]:
                address = loc.get("address", {}) if isinstance(loc, dict) else {}
                name = address.get("addressLocality") or (
                    loc.get("name") if isinstance(loc, dict) else str(loc)
                )
                if name:
                    job["locations"].append(name)
            identifier = payload.get("identifier")
            external = (
                str(identifier.get("value"))
                if isinstance(identifier, dict) and identifier.get("value")
                else None
            )
            url = payload.get("url")
            job["published_at"] = aware(payload.get("datePosted"))
            job["published_at_semantics"] = "date_posted" if job["published_at"] else "unknown"
            job["employment_type"] = payload.get("employmentType")
            if isinstance(job["employment_type"], list):
                job["employment_type"] = ",".join(job["employment_type"])
            job["salary_raw"] = (
                json.dumps(payload["baseSalary"], ensure_ascii=False)
                if payload.get("baseSalary")
                else None
            )
    text = canonical(text)
    raw_type = str(job["employment_type"] or "").lower()
    labels = {
        "internship": "internship",
        "intern": "internship",
        "fulltime": "full_time",
        "full_time": "full_time",
        "full-time": "full_time",
        "parttime": "part_time",
        "part-time": "part_time",
        "contract": "contract",
        "temporary": "temporary",
    }
    job["employment_type"] = labels.get(
        raw_type,
        "internship"
        if re.search(r"实习|\bintern(?:ship)?\b", job["title_raw"], re.I)
        else "unknown",
    )
    if re.search(r"agent|智能体", job["title_raw"], re.I):
        job["role_family"] = (
            "agent_post_training"
            if re.search(r"后训练|post.training", job["title_raw"], re.I)
            else "agent_application"
        )
    job["locations"] = list(
        dict.fromkeys(
            job["locations"] or re.findall(r"北京|上海|深圳|广州|杭州|成都|武汉", text[:1000])
        )
    )
    if provider != "manual_import":
        metadata["original_date_fields"] = {
            k: payload[k]
            for k in ["first_published", "updated_at", "publishedAt", "datePosted"]
            if payload.get(k)
        }
        metadata["date_policy"] = "不为缺失时区的日期猜测 UTC；原字段单独保留"
    return job, text, external, url, metadata
