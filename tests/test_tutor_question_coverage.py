"""Synthetic allocation/authority regressions; live quality is verified separately."""

from types import SimpleNamespace

import pytest
from test_bounded_fact_evidence_v166 import pack

from careerforge.contracts import AnswerPart
from careerforge.contracts_streaming_ext import RelevantSemanticCheck
from careerforge.errors import DomainError
from careerforge.services.answer_completion import AnswerCompletionMixin
from careerforge.services.answer_relevance import RelevantAnswerMixin
from careerforge.services.bounded_evidence import byte_size, select_bounded_facts
from careerforge.services.question_coverage import (
    QUESTION_COVERAGE_CONTRACT,
    FactCoverageGroup,
    interleave_groups,
    needs_coverage_selection,
)


def test_subquestions_get_a_representative_before_background_additions():
    groups = [
        FactCoverageGroup(question_part="rank", fact_ids=["fusion", "weights"]),
        FactCoverageGroup(question_part="clip", fact_ids=["finalize", "prioritize"]),
        FactCoverageGroup(question_part="external guarantee", fact_ids=[]),
    ]
    ordered, required = interleave_groups(groups)
    assert ordered == ["fusion", "finalize", "weights", "prioritize"]
    assert required == ["fusion", "finalize"]
    assert groups[-1].fact_ids == []


@pytest.mark.parametrize(
    "question",
    [
        "https://github.com/earendil-works/pi   我要学这个的架构",
        "Explain this project's architecture",
        "讲解这个系统的整体设计",
    ],
)
def test_single_architecture_question_needs_semantic_coverage(question):
    assert needs_coverage_selection(question)


def test_repository_url_alone_does_not_trigger_architecture_coverage():
    assert not needs_coverage_selection("https://github.com/example/architecture 请定位 parse 函数")


def test_new_completion_gap_names_missing_requirements_without_rewriting_legacy_text():
    service = SimpleNamespace(action=lambda key, reasons: {"kind": "evidence_gap"})
    assessment = {
        "append_gap": True,
        "status": "partial",
        "gap_category": "evidence",
        "reason_codes": ["incomplete"],
        "binding_hash": "hash",
        "binding": {},
        "unsupported_aspects": ["CrossEncoder 是否使用"],
    }
    old = AnswerCompletionMixin._completion_gap(service, assessment)
    assert "CrossEncoder" not in old["text"]
    assessment["binding"]["question_coverage_contract"] = QUESTION_COVERAGE_CONTRACT
    new = AnswerCompletionMixin._completion_gap(service, assessment)
    assert "CrossEncoder 是否使用" in new["text"]
    assert "不表示项目没有" in new["text"]


def allocate(value, support, **extra):
    return select_bounded_facts(
        {"p": value},
        {"p": support},
        question="Alpha circuit",
        variants=["Alpha circuit"],
        limit=12000,
        per_project_limit=1,
        max_project_samples=1,
        selection_manifest_id="test-selection",
        **extra,
    )


def test_semantic_preference_recovers_a_nonlexical_fact_without_trimming_its_support():
    value, support = pack("p")
    units, trace = allocate(
        value,
        support,
        coverage_contract=QUESTION_COVERAGE_CONTRACT,
        preferred_fact_ids=["p-b", "p-a"],
    )
    assert {f["fact_atom_id"] for f in units[0]["facts"]} == {"p-a", "p-b"}
    assert trace["facts_approved"] is False
    assert trace["selected_bytes"] == sum(byte_size(u) for u in units) <= 12000
    assert {c["citation_id"] for c in units[0]["citations"]} == {"p-e0", "p-e1", "p-e2"}
    assert trace["units"][0]["fact_support"]["p-a"]["counterevidence_refs"] == ["p-e2"]
    legacy, _ = allocate(value, support)
    assert "p-b" not in {f["fact_atom_id"] for u in legacy for f in u["facts"]}


@pytest.mark.parametrize("ids", [["foreign"], ["p-a", "p-a"]])
def test_model_preferences_cannot_widen_fact_identity_scope(ids):
    value, support = pack("p")
    with pytest.raises(DomainError):
        allocate(
            value, support, coverage_contract=QUESTION_COVERAGE_CONTRACT, preferred_fact_ids=ids
        )


def test_preference_does_not_override_the_original_zero_byte_budget():
    value, support = pack("p")
    units, _ = select_bounded_facts(
        {"p": value},
        {"p": support},
        question="Alpha",
        variants=[],
        limit=0,
        per_project_limit=1,
        max_project_samples=1,
        selection_manifest_id="test",
        coverage_contract=QUESTION_COVERAGE_CONTRACT,
        preferred_fact_ids=["p-a"],
    )
    assert units == []
    assert needs_coverage_selection("A是什么？B如何工作？")
    assert not needs_coverage_selection("A是什么？")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relevant,contract,kind",
    [(True, True, "repository_fact"), (True, False, "evidence_gap"), (False, True, "evidence_gap")],
)
async def test_partial_fact_requires_independent_relevance_and_preserves_legacy_behavior(
    relevant, contract, kind
):
    value, _ = pack("p")
    value["facts"] = value["facts"][:1]

    async def check(*args):
        return RelevantSemanticCheck(
            evidence_relevant=relevant,
            supported=True,
            conditions_preserved=True,
            quantifiers_preserved=True,
            no_new_project_facts=True,
            reasons=[],
        )

    service = SimpleNamespace(
        _fresh_unit=lambda actor, unit, fid: unit,
        _check_relevance=check,
        action=lambda action, details: {"kind": "evidence_gap", "details": details},
    )
    block = AnswerPart(
        kind="evidence_gap",
        text="Untrusted model gap text",
        fact_atom_ids=["p-a"],
        publication_ids=["p"],
        conditions=value["facts"][0]["conditions"],
        transformation="paraphrase",
    )
    result = await RelevantAnswerMixin._guard_relevant_block(
        service,
        "owner",
        "Alpha?",
        block,
        [value],
        {},
        ("run", 1, "worker"),
        "check",
        scope_focus={"question_coverage_contract": QUESTION_COVERAGE_CONTRACT} if contract else {},
    )
    assert result["kind"] == kind
    if kind == "repository_fact":
        assert result["text"] == value["facts"][0]["text"]
        assert result["conditions"] == value["facts"][0]["conditions"]
        assert result["citations"] == value["citations"]
