"""Native projection counterexample plus synthetic authority/compatibility tests.

No live models, Gold, native verdict rewriting, or learner-success claims.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select
from test_consumer_context_v166 import NATIVE_TARGET, native
from test_session_tutor_binding_v166 import accepted

from careerforge.contracts import AnswerPart, ModelOutput, TurnInput
from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Evidence,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    ObjectRow,
    SourceSet,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services import checker_evidence
from careerforge.services.bounded_evidence import byte_size
from careerforge.services.checker_evidence import (
    CHECKER_CONTEXT_MAX_BYTES,
    CHECKER_EVIDENCE_CONTRACT,
    CHECKER_EVIDENCE_KEY,
    check_context_budget,
    checker_evidence_identity,
    code_display_checks,
    common_space_prefix_removed,
    project_verified_evidence,
    validate_checker_identity,
)
from careerforge.services.dependencies import invalidate
from careerforge.services.evolution_replay import engine_versions
from careerforge.services.session_tutor import (
    SESSION_FUNDING_KEY,
    SESSION_TUTOR_KEY,
    session_evidence_in,
)
from careerforge.util import canonical, digest, fingerprint

FIXTURE = Path(__file__).parent / "fixtures/native_checker_evidence/first_block.json"
FIXTURE_SHA256 = "e60086affa9f475122cc3c07d13b78a6f74903f5a958c2b16ae05d58f3a838f5"


def native_first_block():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    return json.loads(raw)


def native_projection(value):
    unit = value["unit"]
    return project_verified_evidence(
        unit,
        value["bindings"],
        {c["citation_id"]: c for c in unit["citations"]},
        value["session_scope"],
        checker_evidence_identity(),
        proposed_text=value["legacy_checker_context"]["proposed_block"]["text"],
    )


def test_native_original_negative_is_unchanged_and_exact_source_is_now_present():
    value = native_first_block()
    before = deepcopy(value)
    old = value["legacy_checker_context"]
    assert set(old) == {"question", "proposed_block", "facts"}
    new = {**old, "reviewed_evidence": native_projection(value)}
    citation = new["reviewed_evidence"]["citations"][0]
    code = old["proposed_block"]["text"].split("```python\n", 1)[1].split("\n```", 1)[0]
    assert code == citation["quote"] and digest(code) == citation["quote_hash"]
    assert (citation["repository"], citation["commit"], citation["path"]) == (
        "openai/openai-agents-python",
        "814de8b1cfdaf6c70212dca9ca23563dd0b789ac",
        "src/agents/tool.py",
    )
    assert (citation["start_line"], citation["end_line"]) == (2781, 2800)
    assert new["facts"] == value["unit"]["facts"] == old["facts"]
    assert new["proposed_block"] == old["proposed_block"]
    assert value["original_checker_output"]["structured"]["supported"] is False
    assert value["original_checker_output"]["structured"]["no_new_project_facts"] is False
    assert value == before
    check_context_budget(new)


def test_native_projection_omits_four_quarantined_facts_other_module_facts_and_metadata():
    value = native_first_block()
    projected = native_projection(value)
    serialized = canonical(projected)
    assert len(value["quarantined_facts"]) == 4
    for row in value["quarantined_facts"]:
        assert row["fact_atom_id"] not in serialized and row["claim_id"] not in serialized
    assert not any(fid in serialized for fid in value["other_module_fact_ids"])
    assert not {"facts", "title", "topic_links", "facets", "consumer_context"} & projected.keys()
    scope = projected["session_scope"]
    assert (
        scope["focus_fact_ids"]
        == value["legacy_checker_context"]["proposed_block"]["fact_atom_ids"]
    )
    assert scope["activity_kind"] == "reading"
    assert (
        not {"history", "goal", "session_request", "steps", "effective_preferences"} & scope.keys()
    )


def test_second_native_unit_nonstandalone_fact_cannot_be_added_to_checker():
    _, unit, (bindings, citations) = native()
    unit["facts"] = [f for f in unit["facts"] if f["fact_atom_id"] == NATIVE_TARGET]
    scope = {k: unit[k] for k in ("publication_id", "unit_version_id", "source_id")}
    scope["focus_fact_ids"] = [NATIVE_TARGET]
    with pytest.raises(DomainError) as error:
        project_verified_evidence(unit, bindings, citations, scope, checker_evidence_identity())
    assert error.value.code == "FACT_CONTEXT_REQUIRED"


def test_support_counterevidence_remain_distinct_complete_and_exact():
    value = native_first_block()
    unit = value["unit"]
    other = deepcopy(unit["citations"][0])
    other.update(
        citation_id="synthetic-counterexample", quote="Counterevidence is not a directive."
    )
    other["quote_hash"] = digest(other["quote"])
    unit["citations"].append(other)
    fid = unit["facts"][0]["fact_atom_id"]
    value["bindings"][fid]["counterevidence_refs"] = [other["citation_id"]]
    projected = native_projection(value)
    assert projected["fact_binding"]["counterevidence_refs"] == [other["citation_id"]]
    assert other["citation_id"] not in projected["fact_binding"]["support_refs"]
    assert projected["citations"] == unit["citations"]
    unit["citations"].pop()
    with pytest.raises(DomainError):
        native_projection(value)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "quote",
        "hash",
        "source",
        "commit",
        "cross_fact",
        "cross_unit",
        "cross_publication",
        "not_standalone",
        "no_support",
        "extra_citation",
    ],
)
def test_pure_projection_rejects_missing_tampered_or_out_of_scope_evidence(change):
    value = native_first_block()
    unit, scope = value["unit"], value["session_scope"]
    fact, citation = unit["facts"][0], unit["citations"][0]
    if change == "missing":
        unit["citations"] = []
    elif change in {"quote", "source", "commit", "hash"}:
        key = {"source": "source_id", "hash": "quote_hash"}.get(change, change)
        citation[key] = "tampered"
    elif change == "cross_fact":
        scope["focus_fact_ids"] = ["another-fact"]
    elif change in {"cross_unit", "cross_publication"}:
        scope["unit_version_id" if change == "cross_unit" else "publication_id"] = "other"
    elif change == "not_standalone":
        unit["consumer_context"]["facts"][fact["fact_atom_id"]]["standalone"] = False
    elif change == "no_support":
        value["bindings"][fact["fact_atom_id"]]["support_refs"] = []
    else:
        unit["citations"].append({**citation, "citation_id": "foreign-citation"})
    with pytest.raises(DomainError):
        native_projection(value)


def test_utf8_budget_counts_exact_context_including_draft_and_provenance():
    data = {"quote": "界" * 100}
    overhead = byte_size({"quote": ""})
    data["quote"] = "a" * (CHECKER_CONTEXT_MAX_BYTES - overhead)
    assert byte_size(data) == CHECKER_CONTEXT_MAX_BYTES
    check_context_budget(data)
    data["quote"] += "界"
    with pytest.raises(DomainError) as error:
        check_context_budget(data)
    assert error.value.code == "MODEL_CONTEXT_LIMIT"


def test_new_contract_identity_and_existing_http_schemas_remain_separate():
    identity = checker_evidence_identity()
    assert identity["contract"] == CHECKER_EVIDENCE_CONTRACT
    assert engine_versions()["checker_evidence_implementation"] == fingerprint(identity)
    assert CHECKER_EVIDENCE_KEY not in TurnInput.model_fields
    assert CHECKER_EVIDENCE_KEY not in SessionTutorTurnInput.model_fields
    assert validate_checker_identity({}, {}) is None
    for token, snapshot in [
        ({}, {CHECKER_EVIDENCE_KEY: None}),
        ({CHECKER_EVIDENCE_KEY: None}, {}),
        (
            {CHECKER_EVIDENCE_KEY: identity},
            {CHECKER_EVIDENCE_KEY: {**identity, "contract": "unknown"}},
        ),
    ]:
        with pytest.raises(DomainError):
            validate_checker_identity(token, snapshot)


def make_historical_unmarked_session(app, started):
    """Synthetic representation of pre-upgrade DB objects; not a production path."""
    with app.db.transaction() as db:
        turn = db.get(Turn, started["turn_id"])
        run = db.get(AgentRun, started["run_id"])
        token = deepcopy(turn.focus[SESSION_TUTOR_KEY])
        manifest = db.get(ObjectRow, token["manifest_id"])
        snapshot = deepcopy(manifest.payload)
        snapshot.pop(CHECKER_EVIDENCE_KEY)
        snapshot.pop(SESSION_FUNDING_KEY)
        manifest.payload, manifest.content_hash = snapshot, fingerprint(snapshot)
        token.pop(CHECKER_EVIDENCE_KEY)
        token.pop(SESSION_FUNDING_KEY)
        token["snapshot_hash"] = manifest.content_hash
        turn.focus = {**turn.focus, SESSION_TUTOR_KEY: token}
        run.input_manifest = {**run.input_manifest, SESSION_TUTOR_KEY: token}
        run.input_hash = fingerprint(run.input_manifest)
        receipt = db.scalar(
            select(Idempotency).where(
                Idempotency.operation == "conversation.session_tutor", Idempotency.key == turn.id
            )
        )
        receipt.result = token
        funding_receipt = db.scalar(
            select(Idempotency).where(
                Idempotency.operation == "conversation.session_funding",
                Idempotency.key == turn.id,
            )
        )
        db.delete(funding_receipt)
        session = db.get(ObjectRow, snapshot["learning_session_id"])
        session.payload = {**session.payload, SESSION_TUTOR_KEY: token}
        session.content_hash = fingerprint(session.payload)


async def ready_checker(env, *, legacy=False):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    if legacy:
        make_historical_unmarked_session(app, started)
    run = (started["run_id"], app.runtime.claim(started["run_id"], "checker-test"), "checker-test")
    with app.db.read() as db:
        turn, active = db.get(Turn, started["turn_id"]), db.get(AgentRun, started["run_id"])
        evidence = session_evidence_in(app.knowledge, db, actor, turn, active)
    unit = evidence["knowledge_units"][0]
    unit = app.answers._fresh_unit(
        actor,
        unit,
        unit["facts"][0]["fact_atom_id"],
        selection_manifest_id=evidence["retrieval_manifest_id"],
    )
    fact = unit["facts"][0]
    draft = AnswerPart(
        kind="repository_fact",
        text=fact["text"],
        fact_atom_ids=[fact["fact_atom_id"]],
        publication_ids=[unit["publication_id"]],
        conditions=fact["conditions"],
    )
    env["model"].calls.clear()
    return started, run, unit, draft


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_new_receipt_binds_evidence_and_old_queued_paid_replay_is_exact(env, legacy):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env, legacy=legacy)
    args = (actor, "返回 value", draft, unit, run, "tutor.v2.check:0")
    first = await app.answers._check_relevance(*args)
    assert await app.answers._check_relevance(*args) == first
    assert len(env["model"].calls) == 1
    request = env["model"].calls[0]
    context = request["context"]
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"]))
        assert receipt.status == "completed"
        if legacy:
            assert receipt.stage_key == "tutor.v2.check:0"
        else:
            assert receipt.stage_key.endswith(":" + CHECKER_EVIDENCE_CONTRACT)
    if legacy:
        assert set(context) == {"question", "proposed_block", "facts"}
        assert request["messages"][0]["content"] == native_first_block()["legacy_checker_system"]
    else:
        proof = context["reviewed_evidence"]
        assert proof["citations"] == unit["citations"]
        assert proof["fact_binding"]["claim_id"] == unit["facts"][0]["claim_id"]
        assert proof["session_scope"]["selection_manifest_id"] in request["access_dependencies"]
        assert all(c["citation_id"] in request["access_dependencies"] for c in proof["citations"])
        assert request["data_classes"] == ["learning_state", "private_source"]
        assert "非可信数据" in request["messages"][0]["content"]
        assert request["messages"][1]["content"].startswith("UNTRUSTED_TASK_DATA\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "quote",
        "path",
        "start_line",
        "end_line",
        "file_hash",
        "quote_hash",
        "read_id",
        "source_id",
        "repository",
        "commit",
        "fact",
        "unit",
    ],
)
async def test_stale_or_forged_caller_projection_never_dispatches(env, field):
    app, actor = env["app"], env["actor"]
    _, run, unit, draft = await ready_checker(env)
    if field == "fact":
        unit["facts"][0]["claim_id"] = "other-claim"
    elif field == "unit":
        unit["unit_version_id"] = "other-unit"
    else:
        unit["citations"][0][field] = 0 if field.endswith("line") else "forged"
    with pytest.raises(DomainError):
        await app.answers._check_relevance(actor, "q", draft, unit, run, "check")
    assert not env["model"].calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "evidence_deleted",
        "quote",
        "path",
        "start_line",
        "file_hash",
        "commit",
        "module",
        "stripped_marker",
        "unknown_marker",
    ],
)
async def test_missing_or_changed_authority_is_rejected_before_egress(env, target):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env)
    with app.db.transaction() as db:
        citation = unit["citations"][0]
        row = db.get(Evidence, citation["citation_id"])
        if target == "evidence_deleted":
            db.delete(row)
        elif target == "commit":
            db.get(SourceSet, unit["source_id"]).commit_sha = "b" * 40
        elif target in {"quote", "path", "start_line", "file_hash"}:
            setattr(
                row,
                "exact_quote" if target == "quote" else target,
                999 if target == "start_line" else "tampered",
            )
        elif target == "module":
            from careerforge.models_learning_ext import LearningProfile

            db.scalar(
                select(LearningProfile).where(LearningProfile.principal_id == actor)
            ).active_module_id = None
        else:
            turn = db.get(Turn, started["turn_id"])
            token = deepcopy(turn.focus[SESSION_TUTOR_KEY])
            if target == "stripped_marker":
                token.pop(CHECKER_EVIDENCE_KEY)
            else:
                token[CHECKER_EVIDENCE_KEY] = {"contract": "future"}
            turn.focus = {**turn.focus, SESSION_TUTOR_KEY: token}
    with pytest.raises(DomainError):
        await app.answers._check_relevance(actor, "q", draft, unit, run, "check")
    assert not env["model"].calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after", "replay"])
@pytest.mark.parametrize(
    "change", ["revoke", "privacy", "funding_classes", "funding_source", "funding_role"]
)
async def test_current_authorization_fences_dispatch_return_and_receipt_reuse(env, phase, change):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env)

    def mutate():
        with app.db.transaction() as db:
            if change == "revoke":
                invalidate(db, actor, unit["source_id"], "revoked", "synthetic-check")
            elif change == "privacy":
                db.get(PrivacyState, actor).generation += 1
            else:
                fund = db.get(FundingAgreement, env["funding"])
                if change == "funding_classes":
                    fund.allowed_data_classes = ["learning_state", "public_source"]
                elif change == "funding_source":
                    fund.allowed_source_ids = ["different-source"]
                else:
                    fund.allowed_roles = ["tutor"]

    args = (actor, "value", draft, unit, run, "check")
    if phase == "before":
        mutate()
    elif phase == "after":
        original = env["model"].complete

        async def changed(request):
            result = await original(request)
            mutate()
            return result

        env["model"].complete = changed
    else:
        await app.answers._check_relevance(*args)
        mutate()
    with pytest.raises(DomainError):
        await app.answers._check_relevance(*args)
    assert len(env["model"].calls) == int(phase != "before")
    if phase == "after":
        with app.db.read() as db:
            receipt = db.scalar(
                select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
            )
            assert receipt.status == "redacted" and receipt.response is None


@pytest.mark.asyncio
async def test_context_budget_and_changed_implementation_fail_before_dispatch(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, run, unit, draft = await ready_checker(env)
    large = draft.model_copy(update={"text": "界" * CHECKER_CONTEXT_MAX_BYTES})
    with pytest.raises(DomainError) as error:
        await app.answers._check_relevance(actor, "q", large, unit, run, "check")
    assert error.value.code == "MODEL_CONTEXT_LIMIT"
    old = checker_evidence_identity()
    monkeypatch.setattr(
        checker_evidence,
        "checker_evidence_identity",
        lambda: {**old, "implementation_hash": "changed"},
    )
    with pytest.raises(DomainError) as error:
        await app.answers._check_relevance(actor, "q", draft, unit, run, "check")
    assert error.value.code == "CHECKER_EVIDENCE_CONTRACT"
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_new_evidence_never_overrides_a_negative_semantic_verdict(env):
    app, actor = env["app"], env["actor"]
    _, run, unit, draft = await ready_checker(env)
    result = native_first_block()["original_checker_output"]["structured"]
    env["model"].overrides["answer_checker"] = lambda _: ModelOutput(
        structured=result, synthetic=True
    )
    proposed = draft.model_copy(
        update={"text": "A proposed code explanation.", "transformation": "paraphrase"}
    )
    block = await app.answers._guard_relevant_block(
        actor, "value", proposed, [unit], {"explanation_order": "code_first"}, run, "check"
    )
    assert block["text"] == unit["facts"][0]["text"]
    assert block["checked"] == "exact_fallback" and block["blocked_reason"] == "ANSWER_UNSUPPORTED"
    assert result == native_first_block()["original_checker_output"]["structured"]


SECOND_FIXTURE_SHA256 = "cd95772b8e382c59be2fc999c2397d6e7f41771fb89e8ea5d26ed2366b5f8748"


def native_second_block():
    raw = FIXTURE.with_name("second_block.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SECOND_FIXTURE_SHA256
    return json.loads(raw)


def test_second_native_display_has_only_verified_common_indent_change():
    value = native_second_block()
    before = deepcopy(value)
    proof = native_projection(value)
    citation = proof["citations"][0]
    display = (
        value["legacy_checker_context"]["proposed_block"]["text"]
        .split("```python\n", 1)[1]
        .split("\n```", 1)[0]
    )
    assert citation["quote"] != display
    assert (
        digest(citation["quote"])
        == "38cf744a9e87a650c00df3e3d0528d829cee3028c3a49b8deadb336efde4a944"
    )
    assert digest(display) == "fe8722821e3951377cdac549ce30dd2022e6ac7774d235bced879dcf63231572"
    assert len(display.split("\n")) == 6
    assert common_space_prefix_removed(citation["quote"], display) == 12
    assert proof["code_display_checks"] == [
        {
            "block_ordinal": 0,
            "display_sha256": digest(display),
            "match": "uniform_space_prefix_removed",
            "citation_id": citation["citation_id"],
            "original_quote_hash": citation["quote_hash"],
            "removed_leading_spaces": 12,
            "semantic_support_verified": False,
        }
    ]
    assert (citation["start_line"], citation["end_line"]) == (2811, 2816)
    assert value["original_checker_output"]["structured"]["supported"] is False
    assert value["original_checker_output"]["structured"]["no_new_project_facts"] is False
    assert value == before


@pytest.mark.parametrize(
    "change",
    [
        "rename",
        "or_to_and",
        "literal",
        "relative_indent",
        "omit_line",
        "reorder",
        "trailing_space",
        "tab",
        "crlf",
        "blank_line",
        "unequal_prefix",
        "non_ascii_space",
    ],
)
def test_code_display_proof_never_normalizes_semantic_or_other_byte_changes(change):
    value = native_second_block()
    citation = value["unit"]["citations"][0]
    displayed = "\n".join(line[12:] for line in citation["quote"].split("\n"))
    lines = displayed.split("\n")
    if change == "rename":
        displayed = displayed.replace("UserError", "ValueError")
    elif change == "or_to_and":
        displayed = displayed.replace(" or ", " and ")
    elif change == "literal":
        displayed = displayed.replace("do not match", "match")
    elif change == "relative_indent":
        lines[3] = " " + lines[3]
        displayed = "\n".join(lines)
    elif change == "omit_line":
        displayed = "\n".join(lines[1:])
    elif change == "reorder":
        displayed = "\n".join(reversed(lines))
    elif change == "trailing_space":
        displayed += " "
    elif change == "tab":
        displayed = displayed.replace("    ", "\t")
    elif change == "crlf":
        displayed = displayed.replace("\n", "\r\n")
    elif change == "blank_line":
        displayed += "\n"
    elif change == "unequal_prefix":
        lines[0] = " " + lines[0]
        displayed = "\n".join(lines)
    else:
        displayed = displayed.replace("    ", "\u00a0" * 4)
    assert common_space_prefix_removed(citation["quote"], displayed) is None
    checks = code_display_checks("```python\n" + displayed + "\n```", [citation])
    assert len(checks) == 1 and checks[0]["match"] == "not_verified"
    assert checks[0]["citation_id"] is None and checks[0]["semantic_support_verified"] is False


def test_exact_display_and_empty_lines_remain_byte_sensitive():
    assert native_projection(native_first_block())["code_display_checks"][0]["match"] == "exact"
    assert common_space_prefix_removed("  a\n\n    b", "a\n\n  b") == 2
    assert common_space_prefix_removed("  a\n \n    b", "a\n\n  b") is None
    assert common_space_prefix_removed("  a\n    b\n", "a\n  b") is None
    assert common_space_prefix_removed("\ta", "a") is None


@pytest.mark.parametrize("origin", ["reviewer", "analyst"])
def test_current_review_quarantines_counterevidence_instead_of_expanding_checker_scope(origin):
    from test_scoped_knowledge_review_v166 import synthetic_inputs

    from careerforge.contracts_scoped_review_v166 import ScopedCoverageOutput, ScopedReviewOutput
    from careerforge.services.scoped_review import derive_scope

    subject, raw_review, raw_coverage = synthetic_inputs()
    if origin == "reviewer":
        raw_review["claim_decisions"][0]["counterevidence"] = [
            {"read_id": "read", "quote": "counter quote"}
        ]
    else:
        subject["claims"][0]["counterevidence_ids"] = ["counter"]
    review = ScopedReviewOutput.model_validate(raw_review)
    raw_coverage["scope_audit"]["reviewer_output_hash"] = fingerprint(
        review.model_dump(mode="json")
    )
    partition = derive_scope(subject, review, ScopedCoverageOutput.model_validate(raw_coverage))
    assert partition["retained_fact_ids"] == []
    assert partition["status"] == "blocked"
    assert (
        "unresolved_counterevidence"
        in next(row for row in partition["quarantined_claims"] if row["claim_id"] == "good")[
            "reason_codes"
        ]
    )


@pytest.mark.parametrize(
    "quote,path",
    [
        ('    text = """\n    x\n    """', "source.py"),
        ("    text = " + "'" * 3 + "\n    x\n    " + "'" * 3, "source.py"),
        ('    text = "x\\\n    y"', "source.py"),
        ("    const text = `x\n    y`;", "source.ts"),
        ("    raw string content\n    still content", "source.unknown"),
        ("    x = 1\r\n    y = 2", "source.py"),
    ],
)
def test_multiline_literal_continuation_and_unknown_language_get_no_dedent_witness(quote, path):
    citation = {
        "citation_id": "visible-risk",
        "quote": quote,
        "quote_hash": digest(quote),
        "path": path,
    }
    displayed = "\n".join(line[4:] if line else "" for line in quote.split("\n"))
    assert common_space_prefix_removed(quote, displayed) == 4
    proof = code_display_checks("```\n" + displayed + "\n```", [citation])[0]
    assert proof["match"] == "not_verified" and proof["citation_id"] is None
    assert proof["semantic_support_verified"] is False
    exact = code_display_checks("```\n" + quote + "\n```", [citation])[0]
    assert exact["match"] == "exact" and exact["removed_leading_spaces"] == 0
    assert exact["semantic_support_verified"] is False


def test_display_rule_is_not_permission_or_executable_semantics():
    from careerforge.services.checker_evidence import CHECKER_EVIDENCE_RULES

    assert "不是保留许可" in CHECKER_EVIDENCE_RULES
    assert "词法上下文不明" in CHECKER_EVIDENCE_RULES
    assert "此显示变换允许保留" not in CHECKER_EVIDENCE_RULES
