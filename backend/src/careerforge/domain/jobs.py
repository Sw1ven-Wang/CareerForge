import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DICTIONARY_VERSION = "skills-v1"
SKILLS = {
    "python": ["python"],
    "java": ["java"],
    "langchain": ["langchain"],
    "agent": ["agent", "智能体"],
    "rag": ["rag", "检索增强"],
    "sql": ["sql"],
    "postgresql": ["postgresql", "postgres"],
    "redis": ["redis"],
    "docker": ["docker"],
    "evaluation": ["评估", "evaluation"],
    "workflow_state": ["状态追踪", "状态管理", "持久化"],
    "exception_handling": ["异常处理", "故障恢复"],
}
TOOL_SKILLS = {"python", "java", "langchain", "sql", "postgresql", "redis", "docker"}


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"spm", "from"}
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), "")
    )


def normalize_skill(value: str) -> str:
    for concept, aliases in SKILLS.items():
        if value.casefold() in [concept, *aliases]:
            return concept
    return value.strip().casefold()


def offline_extract(content: str, title: str | None) -> dict[str, Any]:
    """Transparent narrow rule baseline, never evidence of real LLM quality."""
    category = None
    requirements = []
    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if re.search(r"职责要求|任职资格|任职要求|岗位要求|requirements", stripped, re.I):
            category = "must"
        elif re.search(r"工作职责|岗位职责|responsibilities", stripped, re.I):
            category = "responsibility"
        elif re.search(r"申请职位|招聘帮助|关于百度", stripped):
            category = None
        if category and stripped and not re.search(r"无需|不要求|not required", stripped, re.I):
            for concept, aliases in SKILLS.items():
                matches = [
                    a
                    for a in aliases
                    if re.search(
                        r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])"
                        if a.isascii()
                        else re.escape(a),
                        stripped,
                        re.I,
                    )
                ]
                if matches:
                    start = offset + line.index(stripped)
                    requirements.append(
                        {
                            "requirement_type": "preferred"
                            if re.search(r"优先|加分|preferred", stripped, re.I)
                            else category,
                            "origin": "explicit",
                            "normalized_skill": concept,
                            "skill_kind": "tool" if concept in TOOL_SKILLS else "capability",
                            "text": stripped,
                            "char_start": start,
                            "char_end": start + len(stripped),
                            "logical_group": stripped
                            if re.search(r"至少一种|任意一种|至少一门|任选|或|either", stripped)
                            else None,
                        }
                    )
        offset += len(line)
    header = content[:1000]
    job_type = "internship" if re.search(r"实习|internship", header, re.I) else None
    if job_type is None and re.search(r"社会招聘|社招|SOCIAL", header):
        job_type = "experienced"
    regions = sorted(set(re.findall(r"北京|上海|深圳|广州|杭州|成都|武汉", header)))
    return {
        "title": title,
        "job_type": job_type,
        "regions": regions,
        "experience": None,
        "requirements": requirements,
        "unresolved": ["离线规则仅提取字典命中项，不能替代完整语义提取。"],
    }


def groups(items: list[dict[str, Any]], decisions: list[dict[str, str]]) -> list[list[str]]:
    parent = {i["id"]: i["id"] for i in items}

    def find(key: str) -> str:
        while parent[key] != key:
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        parent[find(right)] = find(left)

    for n, left in enumerate(items):
        for right in items[n + 1 :]:
            if (
                left["source_id"] == right["source_id"]
                or left["content_hash"] == right["content_hash"]
                or left.get("url")
                and left["url"] == right.get("url")
            ):
                union(left["id"], right["id"])
    for d in decisions:
        if d["left"] not in parent or d["right"] not in parent:
            raise ValueError("Decision outside frozen inputs")
        if d["decision"] == "duplicate":
            union(d["left"], d["right"])
    for d in decisions:
        if d["decision"] == "distinct" and find(d["left"]) == find(d["right"]):
            raise ValueError("Contradictory duplicate groups")
    result: dict[str, list[str]] = {}
    for item in items:
        result.setdefault(find(item["id"]), []).append(item["id"])
    return list(result.values())
