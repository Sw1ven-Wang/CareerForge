"""Structural regression only: captured failed native output and synthetic app calls.

Application judgments and retained request fields are unchanged; transport/ACL
metadata is omitted from deliverable fixtures. Private originals remain untouched.
Synthetic controls do not establish model quality or authorize an admission.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from support import FILES
from test_scoped_knowledge_review_v166 import synthetic_inputs

from careerforge.adapters.model import strict_schema
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, ModelOutput
from careerforge.contracts_scope_binding_v166 import (
    REVIEW_SCOPE_BINDING_CONTRACT,
    REVIEW_SCOPE_BINDING_FIELD,
    BoundScopedResearchReviewOutput,
    BoundScopedReviewOutput,
    GlobalReviewIssueScope,
    LocalReviewIssueScope,
)
from careerforge.contracts_scoped_review_v166 import (
    REVIEW_READ_COMPLETION,
    SCOPED_REVIEW_VERSION,
    ScopedCoverageOutput,
    ScopedResearchReviewOutput,
    ScopedReviewOutput,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt, Publication, ReviewDecision, RunEvent
from careerforge.services.build import RESEARCH_REVIEW_SYSTEM, REVIEW_SYSTEM, BuildService
from careerforge.services.quote_correction import QUOTE_INSTRUCTIONS
from careerforge.services.scope_binding import (
    SCOPE_BINDING_INSTRUCTIONS,
    scope_binding_enabled,
    validate_reviewer_bindings,
)
from careerforge.services.scoped_review import SCOPED_REVIEW_INSTRUCTIONS, derive_scope
from careerforge.util import fingerprint

NATIVE = Path(__file__).parent / "fixtures/scope_binding_native"


def test_review_identity_schema_pins_hashes_without_overriding_verdict():
    from careerforge.services.scope_binding import reviewer_identity_schema

    identity = {"unit_version_id": "candidate", "content_hash": "a" * 64, "render_hash": "b" * 64}
    schema = reviewer_identity_schema(BoundScopedReviewOutput, {"scope_identity": identity})
    fields = schema["$defs"]["BoundStandaloneClaimReview"]["properties"]
    for key, value in identity.items():
        assert fields[key]["enum"] == [value]
    assert schema["properties"]["decision"] == BoundScopedReviewOutput.model_json_schema()["properties"]["decision"]
    assert "enum" not in BoundScopedReviewOutput.model_json_schema()["$defs"]["BoundStandaloneClaimReview"]["properties"]["render_hash"]


def native(name):
    return json.loads((NATIVE / name).read_text())


def expanded(subject):
    return {
        "scope_identity": {
            key: subject[key] for key in ("unit_version_id", "content_hash", "render_hash")
        },
        "claims": subject["claims"],
        "topic_links": [{"id": key} for key in subject["topic_link_ids"]],
        "facets": [{"id": key} for key in subject["facet_ids"]],
    }


def marker():
    return {
        "knowledge_review_contract_version": SCOPED_REVIEW_VERSION,
        "review_read_completion": deepcopy(REVIEW_READ_COMPLETION),
        REVIEW_SCOPE_BINDING_FIELD: REVIEW_SCOPE_BINDING_CONTRACT,
    }


def test_native_original_is_schema_valid_legacy_but_runtime_rejects_exact_binding():
    manifest = native("manifest.json")
    for name, record in manifest["files"].items():
        assert hashlib.sha256((NATIVE / name).read_bytes()).hexdigest() == record["exported_sha256"]
    original = native("reviewer_response.json")["model_output"]["structured"]
    original_hash = fingerprint(original)
    ScopedResearchReviewOutput.model_validate(original)
    review = ScopedReviewOutput.model_validate(
        {key: value for key, value in original.items() if key != "task_coverage"}
    )
    coverage = ScopedCoverageOutput.model_validate(
        native("coverage_response.json")["model_output"]["structured"]
    )
    binding = native("coverage_request.json")["scope_binding"]
    assert all(getattr(coverage.scope_audit, key) == value for key, value in binding.items())
    assert binding["reviewer_output_hash"] == fingerprint(review.model_dump(mode="json"))
    issue = review.scope_review.issue_scopes[0]
    assert issue.scope == "unit" and issue.target_ids == [review.scope_review.unit_version_id]
    with pytest.raises(DomainError, match="阻断理由指向错误版本") as error:
        derive_scope(native("scope_subject.json"), review, coverage)
    assert error.value.code == "SCOPED_REVIEW_BINDING"
    with pytest.raises(ValidationError):
        BoundScopedResearchReviewOutput.model_validate(original)
    assert fingerprint(original) == original_hash


@pytest.mark.parametrize("scope", ["unit", "title"])
def test_new_global_shape_requires_empty_targets_without_normalization(scope):
    assert GlobalReviewIssueScope(issue_index=0, scope=scope, target_ids=[]).target_ids == []
    value = {"issue_index": 0, "scope": scope, "target_ids": ["actual-unit-id"]}
    before = deepcopy(value)
    with pytest.raises(ValidationError):
        GlobalReviewIssueScope.model_validate(value)
    assert value == before
    schema = GlobalReviewIssueScope.model_json_schema()
    assert schema["properties"]["target_ids"]["maxItems"] == 0


@pytest.mark.parametrize("scope", ["claim", "topic_link", "facet"])
def test_new_local_shape_requires_nonempty_targets(scope):
    with pytest.raises(ValidationError):
        LocalReviewIssueScope(issue_index=0, scope=scope, target_ids=[])
    assert LocalReviewIssueScope(issue_index=0, scope=scope, target_ids=["id"]).target_ids == ["id"]
    assert LocalReviewIssueScope.model_json_schema()["properties"]["target_ids"]["minItems"] == 1


@pytest.mark.parametrize(
    "scope,valid", [("claim", "bad"), ("topic_link", "topic"), ("facet", "facet")]
)
@pytest.mark.parametrize("mutation", ["valid", "foreign", "wrong_kind", "duplicate"])
def test_early_local_target_checks_use_the_actual_candidate(scope, valid, mutation):
    subject, raw, _ = synthetic_inputs()
    targets = [valid]
    if mutation == "foreign":
        targets = ["other-candidate-id"]
    elif mutation == "wrong_kind":
        targets = ["topic" if scope != "topic_link" else "good"]
    elif mutation == "duplicate":
        targets = [valid, valid]
    raw["scope_review"]["issue_scopes"] = [
        {"issue_index": 0, "scope": scope, "target_ids": targets}
    ]
    parsed = BoundScopedReviewOutput.model_validate(raw)
    if mutation == "valid":
        validate_reviewer_bindings(expanded(subject), parsed)
    else:
        with pytest.raises(DomainError) as error:
            validate_reviewer_bindings(expanded(subject), parsed)
        assert error.value.code == "SCOPED_REVIEW_BINDING"
    assert raw["scope_review"]["issue_scopes"][0]["target_ids"] == targets


@pytest.mark.parametrize(
    "mutation", ["identity", "duplicate_issue", "missing_issue", "claim", "positive"]
)
def test_early_identity_and_issue_mapping_fail_closed(mutation):
    subject, raw, _ = synthetic_inputs()
    if mutation == "identity":
        raw["scope_review"]["render_hash"] = "another-version"
    elif mutation == "duplicate_issue":
        raw["scope_review"]["issue_scopes"] *= 2
    elif mutation == "missing_issue":
        raw["scope_review"]["issue_scopes"] = []
    elif mutation == "claim":
        raw["claim_decisions"].pop()
    else:
        raw["scope_review"]["standalone_claim_ids"] = ["other-candidate-claim"]
    with pytest.raises(DomainError):
        validate_reviewer_bindings(expanded(subject), BoundScopedReviewOutput.model_validate(raw))


def test_valid_empty_unit_targets_still_block_all_partial_facts():
    subject, raw, coverage = synthetic_inputs()
    raw["scope_review"]["issue_scopes"] = [{"issue_index": 0, "scope": "unit", "target_ids": []}]
    review = BoundScopedReviewOutput.model_validate(raw)
    validate_reviewer_bindings(expanded(subject), review)
    coverage["scope_audit"]["reviewer_output_hash"] = fingerprint(review.model_dump(mode="json"))
    partition = derive_scope(subject, review, ScopedCoverageOutput.model_validate(coverage))
    assert partition["status"] == "blocked" and not partition["retained_claim_ids"]
    assert all(
        "unit_blocking_issue" in item["reason_codes"] for item in partition["quarantined_claims"]
    )
    assert raw["decision"] == "needs_evidence"


def test_separate_dto_types_preserve_both_new_wire_schemas():
    assert (
        fingerprint(strict_schema(BoundScopedReviewOutput.model_json_schema()))
        == "b50dc87a1899409cb5a7a0b37f8b795a77c43ea10c92a2a2181395ca8761bc35"
    )
    assert (
        fingerprint(strict_schema(BoundScopedResearchReviewOutput.model_json_schema()))
        == "b427cb19bb18817c304e67b730269dbf0799e14d4ce6ea248d4845eed06e1bd2"
    )


def test_new_model_visible_schema_keeps_disjoint_anyof_shapes():
    schema = strict_schema(BoundScopedResearchReviewOutput.model_json_schema())
    text = json.dumps(schema)
    assert "oneOf" not in text and "discriminator" not in text
    issue = schema["$defs"]["BoundStandaloneClaimReview"]["properties"]["issue_scopes"]["items"]
    assert len(issue["anyOf"]) == 2
    assert schema["$defs"]["GlobalReviewIssueScope"]["properties"]["target_ids"]["maxItems"] == 0
    assert schema["$defs"]["LocalReviewIssueScope"]["properties"]["target_ids"]["minItems"] == 1


def test_contract_is_explicit_and_old_contracts_are_not_upgraded():
    assert not scope_binding_enabled({})
    legacy = marker()
    legacy.pop(REVIEW_SCOPE_BINDING_FIELD)
    assert not scope_binding_enabled(legacy)
    assert scope_binding_enabled(marker())
    for value in [None, "future", {}, 1]:
        with pytest.raises(DomainError) as error:
            scope_binding_enabled({**marker(), REVIEW_SCOPE_BINDING_FIELD: value})
        assert error.value.code == "REVIEW_SCOPE_BINDING_CONTRACT"
    with pytest.raises(DomainError):
        scope_binding_enabled({REVIEW_SCOPE_BINDING_FIELD: REVIEW_SCOPE_BINDING_CONTRACT})
    assert BuildService._child_manifest(legacy) == {}
    parent = {**marker(), "execution_contract_version": "test-existing"}
    assert (
        BuildService._child_manifest(parent)[REVIEW_SCOPE_BINDING_FIELD]
        == REVIEW_SCOPE_BINDING_CONTRACT
    )
    parent.pop(REVIEW_SCOPE_BINDING_FIELD)
    assert REVIEW_SCOPE_BINDING_FIELD not in BuildService._child_manifest(parent)


def test_legacy_schema_prompt_and_retained_native_contract_are_unchanged():
    assert (
        fingerprint(strict_schema(ScopedReviewOutput.model_json_schema()))
        == "b70c7bbf868424c76a15ab06bdaab0bb363818e53fbfd9a11d003fe9a6c2b36d"
    )
    assert (
        fingerprint(strict_schema(ScopedResearchReviewOutput.model_json_schema()))
        == "26b5e16eaa0ec0aef326769f11f284bc8a376347ba92fde57ddbc5d09e9a59a7"
    )
    assert (
        fingerprint(REVIEW_SYSTEM + SCOPED_REVIEW_INSTRUCTIONS)
        == "dadf2eff8ea93e83af6ed96692271a8564a46e91a1c29adea0e799e35dbf6173"
    )
    exported = native("reviewer_request.json")
    retained = exported["application_contract"]
    before = deepcopy(retained)
    assert retained["schema"] == strict_schema(ScopedResearchReviewOutput.model_json_schema())
    assert (
        retained["system_prompt"]
        == RESEARCH_REVIEW_SYSTEM + SCOPED_REVIEW_INSTRUCTIONS + QUOTE_INSTRUCTIONS
    )
    assert SCOPE_BINDING_INSTRUCTIONS not in retained["system_prompt"]
    record = native("manifest.json")["files"]["reviewer_request.json"]
    assert fingerprint(retained) == record["retained_payload_sha256"]
    assert len(exported["original_project_request_sha256"]) == 64
    assert retained == before


def test_export_minimizes_metadata_and_preserves_application_values():
    manifest = native("manifest.json")
    for name in ["reviewer_response.json", "coverage_response.json"]:
        value = native(name)
        assert set(value) == {"model_output"}
        record = manifest["files"][name]
        assert fingerprint(value["model_output"]) == record["retained_payload_sha256"]
        assert record["retained_values_unchanged"] is True
        assert record["original_envelope_sha256"] != record["exported_sha256"]
    assert set(native("coverage_request.json")) == {"scope_binding"}
    assert (
        fingerprint(native("coverage_request.json")["scope_binding"])
        == manifest["files"]["coverage_request.json"]["retained_payload_sha256"]
    )
    for path in NATIVE.iterdir():
        if path.is_file():
            text = path.read_text()
            assert "/root/" not in text and "/workspace/" not in text
            assert "native_agent_id" not in text and "access_dependencies" not in text
    assert "synthetic" in manifest["provenance"]["job_description_and_scenarios"].lower()
    assert "only" in manifest["provenance"]["license_scope"]


async def start_one(env, monkeypatch, *, malformed=None, legacy=False):
    app = env["app"]
    if legacy:
        original_prepare = app.build.prepare_in

        def prepare(*args, **kwargs):
            value = original_prepare(*args, **kwargs)
            value.pop(REVIEW_SCOPE_BINDING_FIELD)
            return value

        monkeypatch.setattr(app.build, "prepare_in", prepare)
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        if request["role"] != "reviewer" or not request.get("schema") or not malformed:
            return output
        value = deepcopy(output.structured)
        subject = request["context"]["subject"]
        issue = (
            {"issue_index": 0, "scope": "unit", "target_ids": [subject["unit_version_id"]]}
            if malformed == "global_shape"
            else {"issue_index": 0, "scope": "claim", "target_ids": ["foreign-claim"]}
        )
        value.update(
            decision="needs_evidence",
            blocking_issues=["Synthetic protocol negative control"],
            scope_review={
                **subject["scope_identity"],
                "standalone_claim_ids": [item["claim_id"] for item in subject["claims"]],
                "issue_scopes": [issue],
                "global_blocking_issues": [],
            },
        )
        return output.model_copy(update={"structured": value})

    monkeypatch.setattr(env["model"], "complete", complete)
    source = app.sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/scope-binding",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=source,
        question="Tool Calling 返回值示例",
        topic_keys=["agent.tool_calling"],
        call_cap=10,
        max_units_per_project=1,
        idempotency_key="scope-binding-test",
    )
    run_id = app.build.start(env["actor"], body)
    await app.drain()
    return run_id, body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed,code",
    [("global_shape", "MODEL_SCHEMA"), ("foreign_target", "SCOPED_REVIEW_BINDING")],
)
async def test_new_contract_rejects_before_coverage_and_preserves_receipt(
    env, monkeypatch, malformed, code
):
    run_id, _ = await start_one(env, monkeypatch, malformed=malformed)
    app = env["app"]
    assert not [call for call in env["model"].calls if call["role"] == "coverage"]
    call = next(
        call for call in env["model"].calls if call["role"] == "reviewer" and call["schema"]
    )
    from careerforge.services.scope_binding import reviewer_identity_schema

    assert call["schema"] == strict_schema(reviewer_identity_schema(
        BoundScopedResearchReviewOutput, call["context"]["subject"]
    ))
    assert SCOPE_BINDING_INSTRUCTIONS in call["messages"][0]["content"]
    with app.db.read() as db:
        root = db.get(AgentRun, run_id)
        assert root.input_manifest[REVIEW_SCOPE_BINDING_FIELD] == REVIEW_SCOPE_BINDING_CONTRACT
        child = db.scalar(
            select(AgentRun).where(AgentRun.parent_id == run_id, AgentRun.workflow == "review")
        )
        assert child.status == "failed"
        assert child.input_manifest[REVIEW_SCOPE_BINDING_FIELD] == REVIEW_SCOPE_BINDING_CONTRACT
        event = db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == child.id, RunEvent.event_type == "run.finished"
            )
        )
        assert event.payload["code"] == code
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == child.id, ModelReceipt.stage_key.like("review.final:%")
            )
        )
        assert receipt.status == "completed"
        issue = receipt.response["structured"]["scope_review"]["issue_scopes"][0]
        assert issue["target_ids"]  # Invalid output retained verbatim, never cleared.
        assert not list(db.scalars(select(ReviewDecision)))
        assert not list(db.scalars(select(Publication)))


@pytest.mark.asyncio
async def test_unmarked_legacy_run_keeps_old_wire_and_late_runtime_rejection(env, monkeypatch):
    run_id, body = await start_one(env, monkeypatch, malformed="global_shape", legacy=True)
    app = env["app"]
    calls = env["model"].calls
    call = next(call for call in calls if call["role"] == "reviewer" and call["schema"])
    assert call["schema"] == strict_schema(ScopedResearchReviewOutput.model_json_schema())
    assert (
        call["messages"][0]["content"]
        == RESEARCH_REVIEW_SYSTEM + SCOPED_REVIEW_INSTRUCTIONS + QUOTE_INSTRUCTIONS
    )
    assert len([c for c in calls if c["role"] == "coverage"]) == 1
    with app.db.read() as db:
        child = db.scalar(
            select(AgentRun).where(AgentRun.parent_id == run_id, AgentRun.workflow == "review")
        )
        assert REVIEW_SCOPE_BINDING_FIELD not in child.input_manifest
        event = db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == child.id, RunEvent.event_type == "run.finished"
            )
        )
        assert event.payload["code"] == "SCOPED_REVIEW_BINDING"
    assert app.build.start(env["actor"], body) == run_id
    with app.db.read() as db:
        assert REVIEW_SCOPE_BINDING_FIELD not in db.get(AgentRun, run_id).input_manifest


@pytest.mark.asyncio
async def test_exact_paid_legacy_receipt_replay_uses_no_new_model_call(env):
    app = env["app"]
    subject, raw, _ = synthetic_inputs()
    raw["scope_review"]["issue_scopes"] = [{"issue_index": 0, "scope": "unit", "target_ids": ["v"]}]
    original = deepcopy(raw)
    env["model"].overrides["reviewer"] = lambda request: ModelOutput(
        synthetic=True, structured=deepcopy(raw)
    )
    manifest = marker()
    manifest.pop(REVIEW_SCOPE_BINDING_FIELD)
    run = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "review", manifest, 2, "legacy-paid-scope"
    )
    epoch = app.runtime.claim(run, "legacy-scope-worker")
    args = (
        run,
        epoch,
        "legacy-scope-worker",
        "review.final:v",
        "reviewer",
        REVIEW_SYSTEM + SCOPED_REVIEW_INSTRUCTIONS,
        {"subject": expanded(subject)},
        ScopedReviewOutput,
    )
    first = await app.gateway.call(*args)
    calls_before = deepcopy(env["model"].calls)
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run))
        before = (receipt.id, receipt.request_hash, deepcopy(receipt.response))
        assert receipt.request_hash == fingerprint(calls_before[0])
    again = await app.gateway.call(*args, replay_only=True)
    assert again == first
    assert env["model"].calls == calls_before
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run))
        assert (receipt.id, receipt.request_hash, receipt.response) == before
    assert raw == original and first.structured == original
