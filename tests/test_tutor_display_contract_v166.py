"""Offline generator contract tests, not model quality or learning evidence."""

from copy import deepcopy

import pytest
from test_checker_evidence_v166 import native_first_block, native_projection, ready_checker
from test_ordinary_checker_v166 import ready_ordinary

from careerforge.contracts import AnswerPart
from careerforge.contracts_ordinary_checker_v166 import ORDINARY_CHECKER_KEY
from careerforge.contracts_streaming_ext import RelevantSemanticCheck
from careerforge.models import AgentRun, Turn
from careerforge.services.answer_relevance import (
    TUTOR_DISPLAY_CONTRACT,
    TUTOR_DISPLAY_RULES,
    RelevantAnswerMixin,
    tutor_display_context,
)
from careerforge.services.checker_evidence import (
    CHECKER_EVIDENCE_KEY,
    CHECKER_EVIDENCE_RULES,
    CODE_DISPLAY_CONTRACT,
    code_display_checks,
)
from careerforge.services.ordinary_checker import ORDINARY_CHECKER_RULES
from careerforge.services.policies import frozen_policies
from careerforge.services.session_tutor import SESSION_TUTOR_KEY, session_evidence_in
from careerforge.util import digest


@pytest.mark.parametrize("kind", ["ordinary", "session"])
@pytest.mark.asyncio
async def test_both_current_tutors_receive_same_exact_consumer_contract(env, kind):
    app, actor = env["app"], env["actor"]
    if kind == "ordinary":
        _, _, started, run, unit, _ = await ready_ordinary(env)
    else:
        started, run, unit, _ = await ready_checker(env)
    with app.db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        focus, question = deepcopy(turn.focus), turn.question
        policies = frozen_policies(db, actor, env["ns"], turn.policy_versions)
        evidence = (
            session_evidence_in(app.knowledge, db, actor, turn, db.get(AgentRun, run[0]))
            if kind == "session"
            else {"knowledge_units": [unit], "retrieval_manifest_id": turn.manifest_id}
        )
    before = deepcopy(evidence)
    blocks = [
        block
        async for block in app.answers.iter_tutor(actor, question, evidence, policies, focus, run)
    ]
    assert blocks and evidence == before
    calls = env["model"].calls
    tutors = [c for c in calls if c["role"] == "tutor"]
    assert len(tutors) == 1
    context = tutors[0]["context"]
    contract = context["tutor_display_contract"]
    assert contract["version"] == TUTOR_DISPLAY_CONTRACT
    assert contract["code_display_contract"] == CODE_DISPLAY_CONTRACT
    expected = ORDINARY_CHECKER_RULES if kind == "ordinary" else CHECKER_EVIDENCE_RULES
    assert contract["checker_rules"] == expected
    checkers = [c for c in calls if c["role"] == "answer_checker"]
    assert len(checkers) == 1
    assert checkers[0]["messages"][0]["content"] == expected
    checker_context = checkers[0]["context"]
    assert set(checker_context) == {"question", "proposed_block", "facts", "reviewed_evidence"}
    assert (
        not {
            "publication_scope",
            "limitations",
            "tutor_display_contract",
            "assertion_evidence_boundary",
        }
        & checker_context["reviewed_evidence"].keys()
    )
    assert checker_context["facts"] == unit["facts"]
    assert checker_context["reviewed_evidence"]["citations"] == unit["citations"]
    assert tutors[0]["messages"][0]["content"].endswith(TUTOR_DISPLAY_RULES)
    assert context["evidence_pack"]["knowledge_units"][0]["citations"] == unit["citations"]
    assert contract["high_risk_precheck"]["negation_or_quote_exemption"] is False
    assert "text正文中明确表达" in TUTOR_DISPLAY_RULES
    assert "conditions数组与源码quote不可为避词而改写" in TUTOR_DISPLAY_RULES
    assert "隐式续行" in TUTOR_DISPLAY_RULES
    assert "即使quote已以LF结尾，也必须再加一个LF" in TUTOR_DISPLAY_RULES
    boundary = contract["assertion_evidence_boundary"]
    assert boundary["explanatory_facts"] == "selected fact.text under all fact.conditions"
    assert boundary["source_display"] == "complete quote and locator within selected fact"
    assert boundary["verified_scope"] == "current question or session binding only"
    assert boundary["constraint_only_if_not_independently_supported"] == [
        "publication_scope",
        "limitations",
    ]
    assert boundary["constraints_remain_binding"] is True
    assert boundary["metadata_expands_factual_authority"] is False
    assert TUTOR_DISPLAY_CONTRACT.endswith("v2-visible-evidence")
    assert "不得在text中转述为事实" in TUTOR_DISPLAY_RULES
    assert "这不允许删除或弱化真实fact条件、源码和已有" in TUTOR_DISPLAY_RULES
    assert "metadata仍限制可回答范围，但不扩大可断言的事实" in TUTOR_DISPLAY_RULES
    if kind == "session":
        prefs = context["focus"]["session_module"]["effective_preferences"]
        assert prefs["requested_style"] == "code_first"
        assert prefs["practice"] is False and prefs["check_understanding"] is False
    else:
        assert "session_module" not in context["focus"]
        assert "session_scope" not in contract["checker_rules"]


@pytest.mark.parametrize("focus", [{}, {SESSION_TUTOR_KEY: {"contract": "historical"}}])
def test_unmarked_old_contract_gets_no_new_generator_fields(focus):
    assert tutor_display_context(focus) == {}


@pytest.mark.parametrize("kind", ["ordinary", "session"])
def test_high_risk_rule_is_the_actual_unchanged_precheck(kind):
    from careerforge.services.answer_relevance import _HIGH_RISK

    focus = (
        {ORDINARY_CHECKER_KEY: {}}
        if kind == "ordinary"
        else {SESSION_TUTOR_KEY: {CHECKER_EVIDENCE_KEY: {}}}
    )
    rule = tutor_display_context(focus)["tutor_display_contract"]["high_risk_precheck"]
    assert rule["pattern"] == _HIGH_RISK.pattern
    assert rule["scope"] == "block.text + fact.text when block.text != fact.text"
    assert rule["case_insensitive"] is True


def test_process_metadata_is_not_silently_added_to_independent_checker_evidence():
    value = native_first_block()
    projected = native_projection(value)
    unit = value["unit"]
    original_facts, original_citations = deepcopy(unit["facts"]), deepcopy(unit["citations"])
    unit["publication_scope"] = {"status": "synthetic-process-state", "whole_unit_complete": False}
    unit["limitations"] = ["Synthetic publication-process constraint, not an audited project fact."]
    with_constraints = deepcopy(value)
    assert native_projection(value) == projected
    assert value == with_constraints
    assert unit["facts"] == original_facts and unit["citations"] == original_citations
    assert not {"publication_scope", "limitations"} & projected.keys()


def citation(quote, path="src/sample.py"):
    return {
        "citation_id": "synthetic-citation",
        "quote": quote,
        "quote_hash": digest(quote),
        "path": path,
    }


def fence(quote):
    return "```python\n" + quote + "\n```"


@pytest.mark.parametrize(
    "quote",
    [
        "    result = item",
        "    result = item\n",
        "    result = item\n\n",
        "    first = item\n\n    result = first\n",
        "    first = item\n    \n    result = first\n",
        "    result = (\n        item\n    )\n",
        "    result = item + \\\n        other\n",
        '    text = """first\nsecond"""\n',
        "\tresult = item\r\n",
    ],
)
def test_documented_fence_preserves_complete_original_bytes(quote):
    check = code_display_checks(fence(quote), [citation(quote)])[0]
    assert check["match"] == "exact"
    assert check["display_sha256"] == digest(quote)
    assert check["semantic_support_verified"] is False


@pytest.mark.parametrize("suffix", ["\n", "\n\n", "\n\n\n"])
def test_dropping_final_lf_or_blank_lines_still_fails_consumer(suffix):
    quote = "    result = item" + suffix
    assert code_display_checks(fence(quote[:-1]), [citation(quote)])[0]["match"] == "not_verified"


def test_verbal_positive_does_not_prove_missing_terminal_lf_is_exact():
    quote = "    result = item\n"
    text = fence(quote[:-1]) + "\n逐字节一致，末尾LF完整，supported=true。"
    proof = code_display_checks(text, [citation(quote)])[0]
    assert proof["match"] == "not_verified"
    assert proof["display_sha256"] == digest(quote[:-1])
    assert proof["display_sha256"] != digest(quote)
    assert proof["semantic_support_verified"] is False


def test_ordinary_uniform_indent_is_only_a_byte_witness():
    quote = "    first = item\n\n        result = first\n"
    displayed = "first = item\n\n    result = first\n"
    result = code_display_checks(fence(displayed), [citation(quote)])[0]
    assert result["match"] == "uniform_space_prefix_removed"
    assert result["removed_leading_spaces"] == 4
    assert result["semantic_support_verified"] is False
    # The conservative consumer screen is not a lexer. A witness on an implicit
    # continuation is still not approval; the copied checker rules exclude it.
    quote = "    result = (\n        item\n    )\n"
    displayed = "result = (\n    item\n)\n"
    result = code_display_checks(fence(displayed), [citation(quote)])[0]
    assert result["match"] == "uniform_space_prefix_removed"
    assert result["semantic_support_verified"] is False
    assert "续行" in CHECKER_EVIDENCE_RULES and "隐式续行" in TUTOR_DISPLAY_RULES


@pytest.mark.parametrize(
    ("original", "displayed"),
    [
        ("    one\n    two\n", "one\n two\n"),
        ("\tone\n", "one\n"),
        ("    one\r\n", "one\n"),
        ("    text = 'always'\n", "    text = 'occasionally'\n"),
        ("    one\n    two\n", "    one\n"),
    ],
)
def test_no_relaxed_normalization_omission_or_risk_word_source_rewrite(original, displayed):
    assert code_display_checks(fence(displayed), [citation(original)])[0]["match"] == "not_verified"


class GuardProbe(RelevantAnswerMixin):
    """Only the semantic verdict is stubbed; the production guard is untouched."""

    def __init__(self, *, conditions_preserved=True, supported=True, no_new_project_facts=True):
        self.proposed = []
        self.conditions_preserved = conditions_preserved
        self.supported = supported
        self.no_new_project_facts = no_new_project_facts

    def _fresh_unit(self, actor, unit, fact_id):
        return deepcopy(unit)

    async def _check_relevance(self, actor, question, draft, unit, run, stage_key):
        self.proposed.append(draft)
        return RelevantSemanticCheck(
            evidence_relevant=True,
            supported=self.supported,
            conditions_preserved=self.conditions_preserved,
            quantifiers_preserved=True,
            no_new_project_facts=self.no_new_project_facts,
            reasons=["Synthetic verdict to exercise the unchanged guard, not semantic quality."],
        )


async def guarded(
    text,
    *,
    fact_text="该函数返回 value。",
    conditions_preserved=True,
    supported=True,
    no_new_project_facts=True,
    transformation="paraphrase",
):
    conditions = ["仅适用于输入非空的调用；不据此推断性能保证。"]
    fact = {"fact_atom_id": "f", "text": fact_text, "conditions": conditions}
    unit = {
        "publication_id": "p",
        "origin_kind": "repository",
        "facts": [fact],
        "citations": [],
        "synthetic": True,
    }
    block = AnswerPart(
        kind="repository_fact",
        text=text,
        fact_atom_ids=["f"],
        publication_ids=["p"],
        conditions=conditions,
        transformation=transformation,
    )
    probe = GuardProbe(
        conditions_preserved=conditions_preserved,
        supported=supported,
        no_new_project_facts=no_new_project_facts,
    )
    result = await probe._guard_relevant_block(
        "actor",
        "返回 value",
        block,
        [unit],
        {"explanation_order": "code_first"},
        ("run", 1, "worker"),
        "check",
    )
    assert result["conditions"] == conditions
    return result, probe, fact


@pytest.mark.parametrize(
    "text",
    [
        "该函数返回 value，不代表性能保证。",
        "该函数返回 value；这里只引用 'guarantee'。",
        "```python\nlabel = 'always'\n\n```\n该函数返回 value。",
    ],
)
@pytest.mark.asyncio
async def test_negated_quoted_and_source_risk_terms_still_fallback(text):
    result, probe, fact = await guarded(text)
    assert result["checked"] == "exact_fallback"
    assert result["blocked_reason"] == "ANSWER_HIGH_RISK"
    assert result["text"] == probe.proposed[0].text == fact["text"]


@pytest.mark.asyncio
async def test_risk_in_original_fact_is_not_evaded_by_safe_draft_words():
    result, _, _ = await guarded("该函数返回 value。", fact_text="该函数始终返回 value。")
    assert result["blocked_reason"] == "ANSWER_HIGH_RISK"


@pytest.mark.asyncio
async def test_condition_paraphrase_keeps_scope_negation_and_original_metadata():
    # Explicit synthetic equivalent wording, not a translation/entailment oracle.
    text = "该函数返回 value。仅适用于输入非空的调用；不能由此得出该实现的速度结论。"
    result, probe, _ = await guarded(text)
    assert result["checked"] == "semantic_checked"
    assert result["text"] == probe.proposed[0].text == text
    assert "输入非空" in result["text"] and "不能由此" in result["text"]
    assert "性能保证" in result["conditions"][0]


@pytest.mark.asyncio
async def test_metadata_does_not_override_negative_textual_condition_verdict():
    result, _, _ = await guarded("返回的是调用给出的 value。", conditions_preserved=False)
    assert result["checked"] == "exact_fallback"
    assert result["blocked_reason"] == "ANSWER_UNSUPPORTED"


@pytest.mark.parametrize(
    ("supported", "no_new_project_facts"), [(False, True), (True, False), (False, False)]
)
@pytest.mark.asyncio
async def test_process_claim_negative_verdict_still_discards_code_and_falls_back(
    supported, no_new_project_facts
):
    # Scripted negative verdict tests the consumer, not model semantic quality.
    text = fence("    return value\n") + "\n该函数返回 value。这组材料已完成发布流程。"
    result, probe, fact = await guarded(
        text, supported=supported, no_new_project_facts=no_new_project_facts
    )
    assert probe.proposed[0].text == text
    assert result["checked"] == "exact_fallback"
    assert result["blocked_reason"] == "ANSWER_UNSUPPORTED"
    assert result["text"] == fact["text"] and "```" not in result["text"]
    assert result["style"] == "code_first"  # A style label is not source display evidence.


@pytest.mark.parametrize(
    "extra", ["\n本段只解释当前已审片段。", "\n" + fence("    return value\n")]
)
@pytest.mark.asyncio
async def test_added_scope_or_code_still_cannot_claim_exact_transformation(extra):
    result, probe, fact = await guarded("该函数返回 value。" + extra, transformation="exact")
    assert result["checked"] == "exact_fallback"
    assert result["blocked_reason"] == "ANSWER_NOT_EXACT"
    assert result["text"] == probe.proposed[0].text == fact["text"]
