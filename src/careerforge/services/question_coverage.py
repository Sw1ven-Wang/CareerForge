"""Question coverage proposals rank already-authorized facts; they approve nothing."""

import re

from pydantic import Field

from ..contracts import Contract
from ..errors import DomainError

QUESTION_COVERAGE_CONTRACT = "question-coverage-allocation-v4"


def needs_coverage_selection(question):
    # A single architecture request has several evidence needs even without
    # punctuation: structure, responsibilities and connections. Repository names
    # alone must not win lexical ranking over those reviewed implementation facts.
    text = re.sub(r"https?://\S+", "", question)
    return bool(re.search(r"架构|architecture|architectural|系统设计|整体设计", text, re.I)) or len(
        [p for p in re.split(r"[?？;；]", question) if p.strip()]
    ) > 1


class FactCoverageGroup(Contract):
    question_part: str = Field(min_length=1, max_length=800)
    fact_ids: list[str] = Field(max_length=2)


class FactCoverageProposal(Contract):
    groups: list[FactCoverageGroup] = Field(max_length=6)
    reason: str = Field(max_length=800)


def interleave_groups(groups):
    ordered = []
    for index in range(2):
        for group in groups:
            if len(group.fact_ids) > index and group.fact_ids[index] not in ordered:
                ordered.append(group.fact_ids[index])
    required = list(dict.fromkeys(g.fact_ids[0] for g in groups if g.fact_ids))
    return ordered[:6], required


def validate_preferred_facts(identifiers, allowed):
    if len(identifiers) != len(set(identifiers)) or not set(identifiers) <= set(allowed):
        raise DomainError("QUESTION_COVERAGE_SCOPE", "覆盖提案含重复或未授权的事实ID。", 422)
    return identifiers
