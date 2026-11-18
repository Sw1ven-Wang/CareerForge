"""Generic lossless JD and source-bound requirement regressions, not model evaluation."""

from copy import deepcopy
from hashlib import sha256

import pytest
from pydantic import ValidationError

from careerforge.contracts import ModelOutput
from careerforge.contracts_jd_v166 import (
    JDExtractionInput,
    JDImportInput,
    RequirementExtractionOutput,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ObjectRow
from careerforge.services.jd import JDService, freeze_requirements, import_document
from careerforge.services.security import create_principal
from careerforge.util import fingerprint


def document(text="  熟悉队列。\r\n\r\n理解事务🙂。  ", **kwargs):
    return import_document(
        JDImportInput(namespace_id="private", jd_id="position", raw_text=text, **kwargs)
    )


def extraction(doc, **changes):
    paragraph = doc["paragraphs"][0]
    value = {
        "key": "queue",
        "technology": "队列",
        "keywords": ["队列"],
        "quote": paragraph["text"],
        "paragraph_id": paragraph["paragraph_id"],
        "span_start": paragraph["span_start"],
        "span_end": paragraph["span_end"],
        "depth": "熟悉",
        "depth_basis": "explicit",
        "modality": "required",
        "group_operator": "SINGLE",
        "group_key": None,
    }
    return {"requirements": [{**value, **changes}], "groups": [], "limitations": []}


def test_import_keeps_exact_utf8_bytes_whitespace_crlf_and_nullable_metadata():
    raw = "  熟悉队列。\r\n\r\n理解事务🙂。  "
    value = document(raw)
    assert value["raw_text"] == raw
    assert value["raw_sha256"] == sha256(raw.encode("utf-8")).hexdigest()
    assert value["raw_sha256"] != fingerprint(raw)
    assert value["raw_text_bytes"] == len(raw.encode("utf-8"))
    assert (
        value["job_title"] is None and value["source"] is None and value["source_version"] is None
    )
    assert value["offset_unit"] == "unicode_codepoints"
    assert [row["text"] for row in value["paragraphs"]] == ["  熟悉队列。", "理解事务🙂。  "]
    for row in value["paragraphs"]:
        assert raw[row["span_start"] : row["span_end"]] == row["text"]
        assert raw.encode()[row["byte_start"] : row["byte_end"]].decode() == row["text"]
        assert row["offset"] == row["span_start"]


def test_supplied_repeated_paragraphs_get_distinct_exact_positions():
    value = document(
        "熟悉队列。\n熟悉队列。",
        paragraphs=[
            {"paragraph_id": "first", "text": "熟悉队列。", "index": 0},
            {"paragraph_id": "second", "text": "熟悉队列。", "index": 1},
        ],
    )
    assert [p["span_start"] for p in value["paragraphs"]] == [0, 6]
    proposal = extraction(value)
    proposal["requirements"][0].update(paragraph_id="second", span_start=6, span_end=11)
    frozen = freeze_requirements(value, RequirementExtractionOutput.model_validate(proposal))
    assert frozen["requirements"][0]["span_start"] == 6


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raw_text_sha256": "0" * 64},
        {"raw_text_bytes": 1},
        {"paragraphs": [{"paragraph_id": "p", "text": "missing"}]},
        {"paragraphs": [{"paragraph_id": "p", "text": "队列"}]},
        {"paragraphs": [{"paragraph_id": "p", "text": "熟悉队列。", "offset": 1}]},
        {"paragraphs": [{"paragraph_id": "p", "text": "熟悉队列。", "text_sha256": "0" * 64}]},
    ],
)
def test_import_refuses_hash_offset_or_coverage_conflicts(kwargs):
    with pytest.raises((DomainError, ValidationError)):
        document("熟悉队列。", **kwargs)


def test_requirement_offsets_identity_and_unknown_depth_are_preserved():
    doc = document("对队列有理解即可，具体熟练程度不限定。")
    proposal = extraction(doc, depth=None, depth_basis="unspecified", modality="unspecified")
    frozen = freeze_requirements(doc, RequirementExtractionOutput.model_validate(proposal))
    req = frozen["requirements"][0]
    assert req["depth"] is None
    assert req["user_mastery_unchanged"] is True
    assert req["raw_sha256"] == doc["raw_sha256"]
    assert req["paragraph_id"] == doc["paragraphs"][0]["paragraph_id"]
    again = freeze_requirements(doc, RequirementExtractionOutput.model_validate(deepcopy(proposal)))
    assert again == frozen


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": "掌握队列。"},
        {"span_start": 1},
        {"span_end": 999},
        {"paragraph_id": "not-the-paragraph"},
        {"depth": "掌握"},
        {"topic_key": "invented.topic"},
        {"user_mastery_unchanged": False},
    ],
)
def test_extraction_refuses_fabricated_provenance_or_mastery(changes):
    doc = document("熟悉队列。")
    with pytest.raises((DomainError, ValidationError)):
        freeze_requirements(
            doc, RequirementExtractionOutput.model_validate(extraction(doc, **changes))
        )


@pytest.mark.parametrize(
    "text", ["加分：熟悉队列。", "不要求熟悉队列。", '请勿将引号内文本视为岗位要求："熟悉队列"。']
)
def test_explicit_nonmandatory_or_excluded_text_cannot_become_required(text):
    doc = document(text)
    with pytest.raises(DomainError):
        freeze_requirements(doc, RequirementExtractionOutput.model_validate(extraction(doc)))


def test_persistent_import_idempotency_scope_and_tampering(env):
    service = JDService(env["app"].db)
    body = JDImportInput(namespace_id=env["ns"], jd_id="generic-position", raw_text="熟悉队列。")
    first = service.import_jd(env["actor"], body)
    assert service.import_jd(env["actor"], body) == first
    changed = service.import_jd(env["actor"], body.model_copy(update={"raw_text": "了解队列。"}))
    assert changed["jd_revision_id"] != first["jd_revision_id"]
    assert service.read(env["actor"], first["jd_revision_id"])["raw_text"] == "熟悉队列。"
    with env["app"].db.transaction() as db:
        other, _, _ = create_principal(db, "unrelated-user")
    with pytest.raises(DomainError):
        service.read(other.id, first["jd_revision_id"])
    with env["app"].db.transaction() as db:
        row = db.get(ObjectRow, first["jd_revision_id"])
        row.payload = {**row.payload, "raw_text": "tampered"}
    with pytest.raises(DomainError):
        service.read(env["actor"], first["jd_revision_id"])


def test_import_never_allows_public_or_foreign_namespace(env):
    service = JDService(env["app"].db)
    with pytest.raises(DomainError):
        service.import_jd(
            env["actor"],
            JDImportInput(
                namespace_id=env["owner"]["public_namespace_id"],
                jd_id="position",
                raw_text="熟悉队列。",
            ),
        )


def grouped_extraction(doc, operator="OR", modality="required"):
    base = extraction(doc)["requirements"][0]
    span = {key: base[key] for key in ("quote", "paragraph_id", "span_start", "span_end")}
    return RequirementExtractionOutput.model_validate(
        {
            "requirements": [
                {
                    **base,
                    "key": key,
                    "technology": key,
                    "modality": modality,
                    "group_operator": operator,
                    "group_key": "alternatives",
                }
                for key in ["quartz", "nats"]
            ],
            "groups": [
                {
                    **span,
                    "key": "alternatives",
                    "operator": operator,
                    "member_keys": ["quartz", "nats"],
                }
            ],
        }
    )


def test_or_group_is_not_erased_or_promoted_to_and_during_research():
    from test_research_coverage_v163 import coverage, evaluate

    from careerforge.services.research_coverage import freeze_research_task

    doc = document("熟悉 Quartz 或 NATS。")
    frozen = freeze_requirements(doc, grouped_extraction(doc))
    task = freeze_research_task("Explain queue choices", scope="focused", requirements=frozen)
    assert all(row.group_operator == "OR" for row in task.requirements)
    partial = coverage(task)
    partial["requirements"] = partial["requirements"][:1]
    result = evaluate(task, analysis=partial, review=partial)
    assert result["complete"] is True
    assert result["requirement_groups"][0]["operator"] == "OR"
    assert sum(row["complete"] for row in result["requirements"]) == 1
    with pytest.raises(DomainError):
        freeze_requirements(doc, grouped_extraction(doc, "AND"))


@pytest.mark.parametrize("wide_quote", [False, True])
def test_and_group_with_local_members_preserves_unrelated_alternative_clause(wide_quote):
    doc = document("熟悉 Python 或 TypeScript 任意一种；熟悉 Quartz 和 NATS。")
    draft = grouped_extraction(doc, "AND").model_dump(mode="json")
    for member, name in zip(draft["requirements"], ("Quartz", "NATS"), strict=True):
        start = doc["raw_text"].index(name)
        member.update(
            quote=name, span_start=start, span_end=start + len(name), depth_basis="inferred"
        )
        if wide_quote:
            member.update(quote=doc["raw_text"], span_start=0, span_end=len(doc["raw_text"]))
    frozen = freeze_requirements(doc, RequirementExtractionOutput.model_validate(draft))
    assert frozen["groups"][0]["operator"] == "AND"


def test_negating_simultaneous_expertise_does_not_negate_the_or_requirement():
    doc = document("熟悉 Quartz 或 NATS 中一种；两项都接触过更好，但不要求同时具备。")
    frozen = freeze_requirements(doc, grouped_extraction(doc))
    assert all(
        req["modality"] == "required" and req["group_operator"] == "OR"
        for req in frozen["requirements"]
    )


def test_required_or_and_preferred_both_can_share_the_same_source_clause():
    # Regression from an actual public-input model failure, generalized to new
    # technologies. It checks contract scope, not the semantic model's accuracy.
    doc = document("熟悉 Quartz 或 NATS 中一种；两项都接触过更好，但不强制同时具备。")
    required = grouped_extraction(doc).model_dump(mode="json")
    preferred = grouped_extraction(doc, "AND", "preferred").model_dump(mode="json")
    preferred["groups"][0]["key"] = "preferred_both"
    preferred["groups"][0]["member_keys"] = ["preferred_quartz", "preferred_nats"]
    for row in preferred["requirements"]:
        row["key"] = "preferred_" + row["key"]
        row["group_key"] = "preferred_both"
    output = RequirementExtractionOutput.model_validate(
        {
            "requirements": required["requirements"] + preferred["requirements"],
            "groups": required["groups"] + preferred["groups"],
        }
    )
    frozen = freeze_requirements(doc, output)
    assert {group["operator"] for group in frozen["groups"]} == {"OR", "AND"}
    assert len([row for row in frozen["requirements"] if row["modality"] == "required"]) == 2
    assert len([row for row in frozen["requirements"] if row["modality"] == "preferred"]) == 2
    with pytest.raises(DomainError):
        freeze_requirements(doc, grouped_extraction(doc, "AND", "required"))


def test_inner_quote_cannot_erase_inherited_bonus_heading():
    doc = document("加分：熟悉队列。")
    proposal = extraction(doc, quote="熟悉队列。", span_start=3)
    with pytest.raises(DomainError, match="加分"):
        freeze_requirements(doc, RequirementExtractionOutput.model_validate(proposal))


def test_multiline_group_keeps_each_members_own_paragraph_offsets():
    doc = document("任选以下一种：\n熟悉 Quartz。\n熟悉 NATS。")
    reqs = []
    for key, paragraph in zip(["quartz", "nats"], doc["paragraphs"][1:], strict=True):
        base = extraction(doc)["requirements"][0]
        reqs.append(
            {
                **base,
                "key": key,
                "technology": key,
                "group_operator": "OR",
                "group_key": "one",
                "quote": paragraph["text"],
                "paragraph_id": paragraph["paragraph_id"],
                "span_start": paragraph["span_start"],
                "span_end": paragraph["span_end"],
            }
        )
    proposal = RequirementExtractionOutput.model_validate(
        {
            "requirements": reqs,
            "groups": [
                {
                    "key": "one",
                    "operator": "OR",
                    "member_keys": ["quartz", "nats"],
                    "quote": doc["raw_text"],
                    "paragraph_id": doc["paragraphs"][0]["paragraph_id"],
                    "span_start": 0,
                    "span_end": len(doc["raw_text"]),
                }
            ],
        }
    )
    frozen = freeze_requirements(doc, proposal)
    assert len({row["paragraph_id"] for row in frozen["requirements"]}) == 2
    assert frozen["groups"][0]["quote"] == doc["raw_text"]


def test_not_required_stays_visible_without_a_false_positive_achievement():
    from test_research_coverage_v163 import coverage, evaluate

    from careerforge.services.research_coverage import freeze_research_task

    doc = document("不要求掌握分布式追踪。")
    item = extraction(
        doc, depth="掌握", modality="not_required", group_operator="NOT", group_key="excluded"
    )["requirements"][0]
    group = {key: item[key] for key in ("quote", "paragraph_id", "span_start", "span_end")}
    frozen = freeze_requirements(
        doc,
        RequirementExtractionOutput.model_validate(
            {
                "requirements": [item],
                "groups": [
                    {**group, "key": "excluded", "operator": "NOT", "member_keys": [item["key"]]}
                ],
            }
        ),
    )
    task = freeze_research_task("Explain tracing", scope="focused", requirements=frozen)
    assessed = coverage(task)
    assessed["requirements"] = []
    result = evaluate(task, analysis=assessed, review=assessed)
    assert result["complete"]
    assert result["requirements"][0]["status"] == "not_required"
    assert result["requirements"][0]["complete"] is False
    assert result["requirements"][0]["demonstrated_depth"] is None


def test_learning_text_references_keep_requirement_logic_and_source_lineage():
    from careerforge.contracts_learning_ext import PlanTextReference
    from careerforge.services.learning_projection import (
        TEXT_CATALOG_VERSION,
        resolve_reference,
        text_catalog,
    )

    doc = document("熟悉 Quartz 或 NATS。")
    frozen = freeze_requirements(doc, grouped_extraction(doc))
    catalog = text_catalog(
        {
            "text_catalog_version": TEXT_CATALOG_VERSION,
            "goal": {
                "goal_version_id": "goal",
                "source_text": doc["raw_text"],
                "requirements": frozen,
            },
        }
    )
    refs = [entry for entry in catalog.values() if entry["kind"] == "goal_requirement"]
    assert refs and all(entry["group_operator"] == "OR" for entry in refs)
    for entry in refs:
        ref = resolve_reference(
            PlanTextReference(text_id=entry["text_id"], quote=entry["text"]), catalog
        )
        assert ref["raw_sha256"] == doc["raw_sha256"]
        assert ref["modality"] == "required" and ref["group_path"]
        ref["group_path"][0]["operator"] = "AND"
        assert entry["group_path"][0]["operator"] == "OR"
    assert all(req["group_path"][0]["operator"] == "OR" for req in frozen["requirements"])


def test_bonus_depth_is_preserved_without_becoming_a_completion_gate():
    from test_research_coverage_v163 import coverage, evaluate

    from careerforge.services.research_coverage import freeze_research_task

    doc = document("加分：掌握分布式追踪。")
    frozen = freeze_requirements(
        doc,
        RequirementExtractionOutput.model_validate(extraction(doc, depth="掌握", modality="bonus")),
    )
    task = freeze_research_task("Explain tracing", scope="focused", requirements=frozen)
    assert task.requirements[0].depth == "掌握"
    assessed = coverage(task)
    assessed["requirements"] = []
    result = evaluate(task, analysis=assessed, review=assessed)
    assert result["complete"] is True
    assert result["requirements"][0]["required_for_completion"] is False
    assert result["requirements"][0]["complete"] is False


def test_unknown_target_depth_never_becomes_demonstrated_mastery():
    from test_research_coverage_v163 import evaluate

    from careerforge.services.research_coverage import freeze_research_task

    doc = document("对队列有理解，具体熟练程度不限定。")
    frozen = freeze_requirements(
        doc,
        RequirementExtractionOutput.model_validate(
            extraction(doc, depth=None, depth_basis="unspecified")
        ),
    )
    task = freeze_research_task("Explain queue", scope="focused", requirements=frozen)
    result = evaluate(task)
    assert not result["complete"]
    assert result["requirements"][0]["target_depth"] is None
    assert result["requirements"][0]["demonstrated_depth"] is None
    assert "unknown_target_depth" in result["requirements"][0]["reason_codes"]


def configure_extractor(env):
    # This test double provides source-bound judgments. It is not a semantic
    # extractor, model-quality measurement, or fallback shipped in production.
    env["model"].overrides["extract"] = lambda request: ModelOutput(
        synthetic=True, structured=extraction(request["context"]["jd_document"])
    )


@pytest.mark.asyncio
async def test_standalone_extraction_is_metered_immutable_and_reusable_by_research(env):
    from test_ingestion_build_v163 import build_input, import_fixture_source

    app, actor = env["app"], env["actor"]
    configure_extractor(env)
    before_profile = app.learning.profile(actor)
    doc = app.jd.import_jd(
        actor, JDImportInput(namespace_id=env["ns"], jd_id="queue-role", raw_text="熟悉队列。")
    )
    body = JDExtractionInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        jd_revision_id=doc["jd_revision_id"],
        idempotency_key="extract-queue",
    )
    request = app.jd.start_extraction(actor, body)
    assert app.jd.start_extraction(actor, body) == request
    await app.drain()
    result = app.runtime.stage(request["run_id"], "result")
    assert result and result["user_mastery_unchanged"]
    assert app.learning.profile(actor) == before_profile
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
        assert run.status == "succeeded" and run.calls_consumed == 1
        assert app.jd.validate_result(db, actor, run, result) == result
    assert app.jd.start_extraction(actor, body) == request
    source = import_fixture_source(env)
    build = app.build.start(
        actor,
        build_input(
            env, source, "research-imported-goal", goal_version_id=result["goal_version_id"]
        ),
    )
    await app.drain()
    outcome = app.runtime.stage(build, "result")
    assert outcome and outcome["publication_ids"]
    requirement = outcome["research_task"]["requirements"][0]
    assert requirement["jd_revision_id"] == doc["jd_revision_id"]
    assert requirement["modality"] == "required"
    assert sum(call["role"] == "extract" for call in env["model"].calls) == 1
    with app.db.transaction() as db:
        db.get(ObjectRow, doc["jd_revision_id"]).state = "revoked"
    with app.db.read() as db, pytest.raises(DomainError):
        app.jd.validate_result(db, actor, db.get(AgentRun, request["run_id"]), result)


@pytest.mark.asyncio
async def test_direct_build_imported_revision_uses_same_extraction_and_reserves_budget(env):
    from test_ingestion_build_v163 import build_input, import_fixture_source

    app, actor = env["app"], env["actor"]
    configure_extractor(env)
    doc = app.jd.import_jd(
        actor,
        JDImportInput(namespace_id=env["ns"], jd_id="queue-build-role", raw_text="熟悉队列。"),
    )
    source = import_fixture_source(env)
    request = build_input(
        env, source, "build-imported-revision", jd_revision_id=doc["jd_revision_id"]
    )
    run_id = app.build.start(actor, request)
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.input_manifest["execution_plan"]["extraction_call_reserve"] == 1
        assert run.input_manifest["jd_text"] is None
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and result["research_task"]["requirements"][0]["raw_sha256"] == doc["raw_sha256"]
    assert app.build.start(actor, request) == run_id
    extract_call = next(call for call in env["model"].calls if call["role"] == "extract")
    assert "private_jd" in extract_call["data_classes"]
    assert doc["jd_revision_id"] in extract_call["access_dependencies"]


@pytest.mark.asyncio
async def test_revoked_jd_before_extraction_dispatch_spends_nothing(env):
    app, actor = env["app"], env["actor"]
    configure_extractor(env)
    doc = app.jd.import_jd(
        actor, JDImportInput(namespace_id=env["ns"], jd_id="revocable-role", raw_text="熟悉队列。")
    )
    request = app.jd.start_extraction(
        actor,
        JDExtractionInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            jd_revision_id=doc["jd_revision_id"],
            idempotency_key="extract-revoked",
        ),
    )
    with app.db.transaction() as db:
        db.get(ObjectRow, doc["jd_revision_id"]).state = "revoked"
    await app.drain()
    assert not env["model"].calls
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
        assert run.status == "failed" and run.calls_consumed == 0


@pytest.mark.parametrize("registered", [True, False])
def test_mechanism_mapping_preserves_framework_and_never_substitutes_language(registered):
    doc = document("必须熟悉 LangGraph 的条件路由。")
    proposal = extraction(
        doc, technology="LangGraph", keywords=["LangGraph"], topic_key="code.python"
    )
    output = RequirementExtractionOutput.model_validate(proposal)
    topics = {"code.python": 1}
    if registered:
        topics.update({"agent.conditional_routing": 2, "framework.langgraph": 1})
    payload = freeze_requirements(doc, output, topic_versions=topics)
    row = payload["requirements"][0]
    mapping = payload["requirement_topic_links"][0]
    assert row["quote"] == doc["raw_text"]
    assert row["modality"] == "required" and row["depth"] == "熟悉"
    assert mapping["requirement_id"] == row["requirement_id"]
    if registered:
        assert row["topic_key"] == "agent.conditional_routing"
        assert mapping["topics"] == [
            {"topic_key": "agent.conditional_routing", "version": 2, "role": "primary"},
            {"topic_key": "framework.langgraph", "version": 1, "role": "framework"},
        ]
    else:
        assert row["topic_key"] is None and mapping["status"] == "provisional"
    assert (
        freeze_requirements(doc, output, registered_topic_keys=set(topics))["requirements"][0][
            "topic_key"
        ]
        == "code.python"
    )
