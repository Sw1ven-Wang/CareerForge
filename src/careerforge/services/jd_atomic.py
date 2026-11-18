"""Versioned JD granularity checks; literal hints are not semantic Gold or mastery."""

import re

from pydantic import Field

from ..contracts_jd_v166 import RequirementDraft, RequirementExtractionOutput, RequirementGroupDraft
from ..errors import DomainError
from .jd_topics import topic_links
from .taxonomy import term_matches

ATOMIC_JD_CONTRACT = "atomic-jd-extraction-v1"


class AtomicRequirementExtractionOutput(RequirementExtractionOutput):
    requirements: list[RequirementDraft] = Field(default_factory=list, max_length=80)
    groups: list[RequirementGroupDraft] = Field(default_factory=list, max_length=80)


ATOMIC_INSTRUCTIONS = """
本轮使用 atomic-jd-extraction-v1：逐个拆分可独立学习和验收的技术/工程能力。
禁止用“Agent工程化”“机制设计”“评估管控”等概括标签代替段落中明确列出的子能力。
一个 technology 只表达一个独立概念；并列模块、工具、管道步骤、发布实践分别建项。
先逐句列举所有明确技术名词，再检查每项是否有对应需求；同一能力重复出现可合并，
不同限定或不同技术不能合并。同一父主题下不同能力也要区分，例如不同记忆类型、
不同RAG处理阶段、不同工程指标。每个明确列出的指标分别保留，不把指标值或已掌握程度编造出来。
technology 尽量采用原文字面术语，让名称和引文中的技术一致；引文优先选含该术语及必要限定的
最小连续片段，不能截掉“优先/加分/不要求/如”等修饰。需要上下文才能理解时可引用完整分句。
模型接收的 named_capability_hints 只是授权词表与原文的字面命中索引，不是答案或岗位必需性裁决。
命中项可能是举例、否定或限定内容，必须依原文解释；明确说不是岗位要求的部分不要提为要求。
角色举例不等于必须有固定数量的独立Agent；协作团队名单不等于要求候选人精通每种岗位。
学历、城市和组织背景不混入技术需求。没有指定的语言、框架和产品绝不能自行补充。
不为增加数量凑项；最多80条。原文职责中的所有独立能力均要保留，不能只选择注册过的Topic。
未注册概念仍输出独立需求、topic_key=null，交给后续显式确认；不要为了映射把概念替换成近似产品。
AND/OR/NOT和深度继续遵守原合同。并列都要求时可用AND组，也可用SINGLE保留各自独立的明确要求；
任何替代或否定限定必须建立原合同规定的组。职责动词“设计/优化/负责”不能自动换算成“精通”。
"""


def named_capability_hints(document, topics):
    """List bounded longest literal catalog matches, excluding metadata/headings."""
    hits = []
    for paragraph in document["paragraphs"]:
        text = paragraph["text"]
        if text.lstrip().startswith(("#", "**岗位名称", "**工作地点", "**学历要求")):
            continue
        if re.match(r"^\s*\*\*\d+[.、]", text):
            continue
        if re.search(r"(?:以下|下列|这些).{0,12}(?:不是|并非).{0,8}岗位要求", text):
            continue
        for topic in topics:
            for alias in topic["aliases"]:
                if len(alias.strip()) < 3 or not term_matches(text, alias):
                    continue
                for match in re.finditer(re.escape(alias), text, re.I):
                    # term_matches checks the whole paragraph, so verify this exact occurrence too.
                    left = text[match.start() - 1 : match.start()] if match.start() else ""
                    right = text[match.end() : match.end() + 1]
                    if (
                        alias[0].isascii()
                        and alias[0].isalnum()
                        and re.match(r"[A-Za-z0-9_]", left)
                    ):
                        continue
                    if (
                        alias[-1].isascii()
                        and alias[-1].isalnum()
                        and re.match(r"[A-Za-z0-9_]", right)
                    ):
                        continue
                    hits.append(
                        {
                            "topic_key": topic["key"],
                            "term": alias,
                            "paragraph_id": paragraph["paragraph_id"],
                            "span_start": paragraph["span_start"] + match.start(),
                            "span_end": paragraph["span_start"] + match.end(),
                        }
                    )
    specific = [
        hit
        for hit in hits
        if not any(
            other["topic_key"] != hit["topic_key"]
            and other["span_start"] <= hit["span_start"]
            and hit["span_end"] <= other["span_end"]
            and other["span_end"] - other["span_start"] > hit["span_end"] - hit["span_start"]
            for other in hits
        )
    ]
    # One hint per catalog identity prevents synonyms from becoming duplicate obligations.
    unique = {}
    for hit in sorted(specific, key=lambda row: (row["span_start"], -len(row["term"]))):
        unique.setdefault(hit["topic_key"], hit)
    return list(unique.values())


def validate_atomic_coverage(document, draft, topics):
    """Reject lost named catalog capabilities; never invent or approve a requirement."""
    for row in draft.requirements:
        if re.search(r"、|,|，|；|;|与|和|及|/|\band\b", row.technology, re.I):
            hits = []
            for topic in topics:
                for alias in topic["aliases"]:
                    if len(alias.strip()) >= 3 and term_matches(row.technology, alias):
                        hits.extend(
                            (m.start(), m.end())
                            for m in re.finditer(re.escape(alias), row.technology, re.I)
                        )
            spans = {
                span
                for span in hits
                if not any(
                    other[0] <= span[0] and span[1] <= other[1] and other != span for other in hits
                )
            }
            if len(spans) > 1:
                raise DomainError(
                    "REQUIREMENT_GRANULARITY", "并列独立能力须分别建项：" + row.technology, 422
                )
    versions = {topic["key"]: topic["version"] for topic in topics}
    aliases = {topic["key"]: topic["aliases"] for topic in topics}
    facets = {topic["key"]: topic.get("facet_keys", []) for topic in topics}
    links = topic_links(
        [{**row.model_dump(), "requirement_id": row.key} for row in draft.requirements],
        versions,
        aliases,
        facets,
    )
    represented = {link["topic_key"] for row in links for link in row["topics"]}
    missing = [
        hint
        for hint in named_capability_hints(document, topics)
        if hint["topic_key"] not in represented
    ]
    if missing:
        terms = ", ".join(hit["term"] + " (" + hit["topic_key"] + ")" for hit in missing[:30])
        raise DomainError(
            "REQUIREMENT_GRANULARITY", "独立技术能力未形成可追溯需求，请拆分后补全：" + terms, 422
        )
