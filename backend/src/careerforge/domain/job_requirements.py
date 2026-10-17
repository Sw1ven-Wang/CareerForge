"""Conservative clause parser and deterministic expression-aware aggregation."""

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from careerforge.domain.json_types import JsonObject as Json
from careerforge.schemas.job_contract import (
    DemandReportV1,
    JobPostingSnapshotV1,
    RequirementFactV1,
    validate_requirement_against_snapshot,
)

EXTRACTOR_VERSION = "job-rules-1.1"
TAXONOMY_VERSION = "job-concepts-1.1"
AGGREGATION_VERSION = "job-demand-1.1"
CONCEPTS = {
    "technology.python": ["Python"],
    "technology.go": ["Go", "Golang"],
    "technology.java": ["Java"],
    "technology.pytorch": ["PyTorch"],
    "technology.langchain": ["LangChain"],
    "technology.sql": ["SQL"],
    "technology.postgresql": ["PostgreSQL", "Postgres"],
    "technology.redis": ["Redis"],
    "technology.docker": ["Docker", "容器部署"],
    "technology.vue": ["Vue"],
    "capability.rag": ["RAG", "检索增强", "引用溯源"],
    "capability.workflow": ["工作流", "持久化", "状态管理"],
    "capability.reliability": ["异常处理", "故障恢复", "幂等", "可靠性"],
    "capability.evaluation": ["评测", "评估", "evaluation", "失败分析"],
    "capability.tool_calling": ["工具调用", "权限边界", "tool calling"],
    "capability.llm_contract": ["模型接口", "结构化输出"],
    "capability.async": ["异步任务", "Celery"],
    "capability.database": ["数据库建模", "版本管理"],
    "capability.frontend": ["前端产品交互"],
    "knowledge.agent": ["Agent", "智能体"],
}
CONSTRAINT_FIELDS = {
    "availability.days_per_week",
    "availability.months",
    "experience.years",
    "education.degree",
    "graduation.year",
    "location.allowed",
}


def signature(expr: Json) -> str:
    if expr["type"] in {"any_of", "all_of"}:
        return cast(
            str, expr["type"] + "(" + ",".join(sorted(signature(c) for c in expr["children"])) + ")"
        )
    if expr["type"] == "concept":
        return cast(str, expr["concept_id"])
    return json.dumps(expr, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate_expression(expr: Json, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("Expression nesting exceeds limit")
    if expr["type"] == "concept" and expr["concept_id"] not in CONCEPTS:
        raise ValueError("Concept is not in the frozen taxonomy")
    if expr["type"] == "concept" and expr["concept_kind"] != expr["concept_id"].split(".")[0]:
        raise ValueError("Concept kind does not match taxonomy")
    if expr["type"] == "constraint" and expr["field"] not in CONSTRAINT_FIELDS:
        raise ValueError("Constraint field is not allowlisted")
    for child in expr.get("children", []):
        validate_expression(child, depth + 1)


def concept_leaves(expr: Json, alternative: bool = False) -> Iterator[tuple[str, bool]]:
    if expr.get("type") == "concept":
        yield expr["concept_id"], alternative
    for child in expr.get("children", []):
        yield from concept_leaves(child, alternative or expr["type"] == "any_of")


def constraints(expr: Json) -> Iterator[Json]:
    if expr["type"] == "constraint":
        yield expr
    for child in expr.get("children", []):
        yield from constraints(child)


def parse_concepts(quote: str) -> Json | None:
    hits = []
    for concept, aliases in CONCEPTS.items():
        found: list[re.Match[str]] = []
        for alias in aliases:
            pattern = (
                r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])"
                if alias.isascii()
                else re.escape(alias)
            )
            found.extend(re.finditer(pattern, quote, re.I))
        if found:
            match = min(found, key=lambda m: m.start())
            hits.append(
                (
                    match.start(),
                    match.end(),
                    {
                        "type": "concept",
                        "concept_id": concept,
                        "concept_kind": concept.split(".")[0]
                        if concept.startswith(("technology.", "knowledge."))
                        else "capability",
                    },
                )
            )
    hits.sort(key=lambda x: x[0])
    if not hits:
        return None

    def combine(nodes: list[Json], op: str) -> Json:
        return nodes[0] if len(nodes) == 1 else {"type": op, "children": nodes}

    # Parentheses delimit subexpressions; within one phrase alternatives bind before AND.
    def parse_range(start: int, end: int) -> Json | None:
        atoms: list[Json] = []
        gaps = []
        cursor = start
        index = start
        while index < end:
            if quote[index] in "(（":
                depth = 1
                closing = index + 1
                while closing < end and depth:
                    if quote[closing] in "(（":
                        depth += 1
                    elif quote[closing] in ")）":
                        depth -= 1
                    closing += 1
                if depth == 0:
                    node = parse_range(index + 1, closing - 1)
                    if node:
                        if atoms:
                            gaps.append(quote[cursor:index])
                        atoms.append(node)
                        cursor = closing
                    index = closing
                    continue
            hit = next((h for h in hits if h[0] == index and h[1] <= end), None)
            if hit:
                if atoms:
                    gaps.append(quote[cursor:index])
                atoms.append(hit[2])
                cursor = hit[1]
                index = hit[1]
            else:
                index += 1
        if not atoms:
            return None
        groups = [[atoms[0]]]
        for gap, node in zip(gaps, atoms[1:], strict=True):
            if re.search(r"或|/|\bor\b|任选", gap, re.I) and not re.search(
                r"并|且|以及|\band\b|\+", gap, re.I
            ):
                groups[-1].append(node)
            else:
                groups.append([node])
        return combine([combine(g, "any_of") for g in groups], "all_of")

    return parse_range(0, len(quote))


def extract(snapshot: JobPostingSnapshotV1) -> list[RequirementFactV1]:
    result = []
    for section in snapshot.document.sections:
        if section.kind == "benefit":
            continue
        for match in re.finditer(r"[^；;。\n]+", section.text):
            quote = match.group().strip()
            if not quote:
                continue
            start = section.start + match.start() + len(match.group()) - len(match.group().lstrip())
            importance = (
                "preferred"
                if re.search(r"优先|加分|preferred|nice.to.have", quote, re.I)
                or section.kind == "preferred"
                else "responsibility"
                if section.kind == "responsibility"
                else "required"
                if section.kind == "qualification"
                else "unknown"
            )
            conditions = []
            constraint = None
            for pattern, field, unit in [
                (
                    r"每周(?:至少|不低于)?(?:出勤|到岗)?\s*(\d+)\s*天",
                    "availability.days_per_week",
                    "days_per_week",
                ),
                (r"(?:实习|持续)(?:至少|不少于)?\s*(\d+)\s*个月", "availability.months", "months"),
                (r"(\d+)\s*年(?:以上)?(?:工作|开发|相关)?经验", "experience.years", "years"),
            ]:
                m = re.search(pattern, quote)
                if m:
                    constraint = {
                        "type": "constraint",
                        "field": field,
                        "comparator": "gte",
                        "value": int(m.group(1)),
                        "unit": unit,
                    }
                    conditions.append(constraint)
            if degree_match := re.search(r"本科|硕士|博士", quote):
                constraint = {
                    "type": "constraint",
                    "field": "education.degree",
                    "comparator": "eq",
                    "value": degree_match.group(),
                }
                conditions.append(constraint)
            if year_match := re.search(r"(20\d{2})届", quote):
                constraint = {
                    "type": "constraint",
                    "field": "graduation.year",
                    "comparator": "eq",
                    "value": int(year_match.group(1)),
                }
                conditions.append(constraint)
            constraint = (
                conditions[0]
                if len(conditions) == 1
                else {"type": "all_of", "children": conditions}
                if conditions
                else None
            )
            expression = parse_concepts(quote)
            if constraint and expression:
                expression = {"type": "all_of", "children": [expression, constraint]}
            elif constraint:
                expression = constraint
            if not expression:
                continue
            uncertain = importance == "unknown" or bool(
                re.search(r"不要求|无需|not required|不必|无需同时|并非", quote, re.I)
            )
            if not list(concept_leaves(expression)):
                kind = "eligibility"
            elif any(c.startswith("capability.") for c, _ in concept_leaves(expression)):
                kind = "engineering_capability"
            elif all(c.startswith("knowledge.") for c, _ in concept_leaves(expression)):
                kind = "knowledge"
            else:
                kind = "technical_skill"
            identifier = str(
                uuid5(
                    NAMESPACE_URL, f"{snapshot.job_snapshot_id}:{EXTRACTOR_VERSION}:{start}:{quote}"
                )
            )
            value = RequirementFactV1.model_validate(
                {
                    "schema_version": "careerforge.requirement.v1",
                    "requirement_id": identifier,
                    "job_snapshot_id": snapshot.job_snapshot_id,
                    "requirement_kind": kind,
                    "importance": importance,
                    "origin": "explicit",
                    "expression": expression,
                    "evidence": {
                        "raw_snapshot_id": snapshot.source.raw_snapshot_id,
                        "job_snapshot_id": snapshot.job_snapshot_id,
                        "section_id": section.section_id,
                        "start": start,
                        "end": start + len(quote),
                        "quote": quote,
                    },
                    "review_status": "pending_review" if uncertain else "auto_extracted",
                    "extractor_version": EXTRACTOR_VERSION,
                    "taxonomy_version": TAXONOMY_VERSION,
                }
            )
            validate_requirement_against_snapshot(value, snapshot)
            result.append(value)
    return result


def aggregate(
    report_id: str,
    goal_id: str,
    snapshots: list[JobPostingSnapshotV1],
    facts: list[RequirementFactV1],
    scope: Json,
    exclusions: Json | None = None,
) -> DemandReportV1:
    exclusions = dict(exclusions or {})
    for snapshot in snapshots:
        if (
            snapshot.source.read_status != "full_text"
            or snapshot.quality.normalization_status != "ready"
        ):
            exclusions[snapshot.job_snapshot_id] = "incomplete_source"
        elif snapshot.quality.dedupe_status != "unique":
            exclusions[snapshot.job_snapshot_id] = "duplicate_or_pending_review"
    included = [s for s in snapshots if s.job_snapshot_id not in exclusions]
    posting_ids = {}
    for s in included:
        if s.posting_id in posting_ids:
            raise ValueError("Select exactly one fixed snapshot per posting")
        posting_ids[s.posting_id] = s.job_snapshot_id
    included_ids = {s.job_snapshot_id for s in included}
    groups: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    refs = defaultdict(set)
    expressions = {}
    eligibility = []
    for fact in facts:
        if (
            fact.job_snapshot_id not in included_ids
            or fact.review_status != "confirmed"
            or fact.origin != "explicit"
        ):
            continue
        value = fact.expression.model_dump(mode="json")
        sig = signature(value)
        if list(constraints(value)):
            eligibility.append(fact.requirement_id)
        if fact.requirement_kind == "eligibility" or fact.importance == "unknown":
            continue
        leaves = list(concept_leaves(value))
        if not leaves:
            continue
        expression_key = ("expression_group", sig)
        expressions[expression_key] = sig
        bucket = (
            "required_alternative"
            if fact.importance == "required" and any(alt for _, alt in leaves)
            else "required_solo"
            if fact.importance == "required"
            else fact.importance
        )
        groups[expression_key][bucket].add(fact.job_snapshot_id)
        refs[expression_key].add(fact.requirement_id)
        for concept, alternative in leaves:
            key = ("concept", concept)
            role = (
                "required_alternative"
                if fact.importance == "required" and alternative
                else "required_solo"
                if fact.importance == "required"
                else fact.importance
            )
            groups[key][role].add(fact.job_snapshot_id)
            refs[key].add(fact.requirement_id)
    denominator = len(included)
    demands = []
    for key, buckets in sorted(groups.items()):
        demands.append(
            {
                "demand_id": str(uuid5(NAMESPACE_URL, report_id + ":" + ":".join(key))),
                "demand_unit": key[0],
                "expression_signature": key[1],
                "concept_id": key[1] if key[0] == "concept" else None,
                **{
                    bucket: {"count": len(buckets[bucket]), "denominator": denominator}
                    for bucket in [
                        "required_solo",
                        "required_alternative",
                        "preferred",
                        "responsibility",
                    ]
                },
                "supporting_requirement_ids": sorted(refs[key]),
            }
        )
    return DemandReportV1.model_validate(
        {
            "schema_version": "careerforge.demand_report.v1",
            "report_id": report_id,
            "goal_version_id": goal_id,
            "scope": scope,
            "included_job_snapshot_ids": sorted(included_ids),
            "excluded_job_snapshot_ids": sorted(exclusions),
            "excluded_reasons": exclusions,
            "eligibility_requirement_ids": sorted(set(eligibility)),
            "denominator": denominator,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "aggregation_version": AGGREGATION_VERSION,
            "demands": demands,
        }
    )


def validate_dedup_decisions(decisions: list[Json]) -> None:
    parent = {i: i for d in decisions for i in [d["left"], d["right"]]}

    def root(i: str) -> str:
        while parent[i] != i:
            i = parent[i]
        return i

    for d in decisions:
        if d["decision"] == "duplicate":
            parent[root(d["right"])] = root(d["left"])
    for d in decisions:
        if d["decision"] == "distinct" and root(d["left"]) == root(d["right"]):
            raise ValueError("Contradictory transitive duplicate decisions")
