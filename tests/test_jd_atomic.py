import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from careerforge.contracts_jd_v166 import JDImportInput, RequirementExtractionOutput
from careerforge.errors import DomainError
from careerforge.services.jd import import_document
from careerforge.services.jd_atomic import (
    AtomicRequirementExtractionOutput,
    named_capability_hints,
    validate_atomic_coverage,
)
from careerforge.services.jd_topics import topic_links


def catalog():
    values = json.loads(
        (Path(__file__).parents[1] / "src/careerforge/data/topics.json").read_text(encoding="utf-8")
    )
    return [{**row, "version": 1, "facet_keys": list(row["facets"])} for row in values]


def doc(text):
    return import_document(JDImportInput(namespace_id="private", jd_id="test", raw_text=text))


def requirement(document, technology, quote, key="one", **extra):
    start = document["raw_text"].index(quote)
    paragraph = next(p for p in document["paragraphs"] if p["span_start"] <= start < p["span_end"])
    return dict(
        key=key,
        technology=technology,
        keywords=[technology],
        quote=quote,
        paragraph_id=paragraph["paragraph_id"],
        span_start=start,
        span_end=start + len(quote),
        depth=None,
        depth_basis="unspecified",
        modality="required",
        group_operator="SINGLE",
        group_key=None,
        topic_key=None,
        **extra,
    )


def test_old_coarse_paragraph_cannot_hide_multiple_named_capabilities():
    document = doc("建设文本分块、语义检索、结果重排及向量数据库。")
    draft = AtomicRequirementExtractionOutput(
        requirements=[requirement(document, "RAG建设", document["raw_text"])]
    )
    with pytest.raises(DomainError) as error:
        validate_atomic_coverage(document, draft, catalog())
    assert error.value.code == "REQUIREMENT_GRANULARITY"
    assert "文本分块" in error.value.message and "向量数据库" in error.value.message


def test_atomic_mapping_preserves_vector_database_vs_embedding_and_unknown_terms():
    document = doc("建设文本分块、语义检索、结果重排及向量数据库；研究新型自定义机制。")
    labels = ["文本分块", "语义检索", "结果重排", "向量数据库", "新型自定义机制"]
    rows = [requirement(document, label, label, str(i)) for i, label in enumerate(labels)]
    topics = catalog()
    draft = AtomicRequirementExtractionOutput(requirements=rows)
    validate_atomic_coverage(document, draft, topics)
    links = topic_links(
        [{**row, "requirement_id": row["key"]} for row in rows],
        {t["key"]: 1 for t in topics},
        {t["key"]: t["aliases"] for t in topics},
        {t["key"]: t["facet_keys"] for t in topics},
    )
    assert [r["topics"][0]["topic_key"] for r in links[:4]] == [
        "rag.chunking",
        "rag.semantic_search",
        "rag.rerank",
        "storage.vector_database",
    ]
    assert links[4]["status"] == "pending_confirmation"
    assert not any(x["topic_key"] == "rag.embedding" for x in links[3]["topics"])


def test_atomic_schema_expansion_does_not_change_legacy_schema():
    document = doc("了解文本分块。")
    rows = [requirement(document, "文本分块", "文本分块", str(i)) for i in range(35)]
    assert len(AtomicRequirementExtractionOutput(requirements=rows).requirements) == 35
    with pytest.raises(ValidationError):
        RequirementExtractionOutput(requirements=rows)
    assert (
        RequirementExtractionOutput.model_json_schema()["properties"]["requirements"]["maxItems"]
        == 30
    )


def test_literal_hints_do_not_invent_python_frameworks_or_require_embedding_for_vector_store():
    document = doc("建设向量数据库与知识图谱，采用 MCP。")
    keys = {h["topic_key"] for h in named_capability_hints(document, catalog())}
    assert {"storage.vector_database", "knowledge.graph", "interface.mcp"} <= keys
    assert not keys & {"rag.embedding", "code.python", "framework.langgraph"}


def test_memory_system_uses_explicit_alias_not_two_character_substring_matching():
    document = doc("设计记忆体系。")
    row = requirement(document, "记忆体系", "记忆体系")
    topics = catalog()
    links = topic_links(
        [{**row, "requirement_id": row["key"]}],
        {t["key"]: 1 for t in topics},
        {t["key"]: t["aliases"] for t in topics},
        {t["key"]: t["facet_keys"] for t in topics},
    )
    assert links[0]["topics"] == [{"topic_key": "agent.memory", "version": 1, "role": "primary"}]


def test_negated_and_bonus_capabilities_are_not_forced_to_be_required():
    document = doc("Redis 仅加分，MCP 不要求。")
    rows = [
        requirement(document, "Redis", "Redis 仅加分", "redis"),
        requirement(document, "MCP", "MCP 不要求", "mcp"),
    ]
    rows[0]["modality"] = "bonus"
    rows[1].update(modality="not_required", group_operator="NOT", group_key="not_mcp")
    # This check only covers named capability completeness, not logical validation or approval.
    validate_atomic_coverage(
        document, AtomicRequirementExtractionOutput(requirements=rows), catalog()
    )


@pytest.mark.parametrize(
    "label, expected",
    [
        ("MCP（Model Context Protocol）", "mapped"),
        ("Context 与 MCP（Model Context Protocol）", "pending_confirmation"),
    ],
)
def test_protocol_full_name_does_not_introduce_nested_generic_context_topic(label, expected):
    document = doc(label)
    row = requirement(document, label, label)
    topics = catalog()
    links = topic_links(
        [{**row, "requirement_id": row["key"]}],
        {t["key"]: 1 for t in topics},
        {t["key"]: t["aliases"] for t in topics},
        {t["key"]: t["facet_keys"] for t in topics},
    )
    assert links[0]["status"] == expected
    if expected == "mapped":
        assert links[0]["topics"][0]["topic_key"] == "interface.mcp"


def test_same_topic_metrics_are_independent_requirements_not_one_label():
    document = doc("关注任务完成率与调用成功率。")
    draft = AtomicRequirementExtractionOutput(
        requirements=[requirement(document, "任务完成率与调用成功率", "任务完成率与调用成功率")]
    )
    with pytest.raises(DomainError, match="分别建项"):
        validate_atomic_coverage(document, draft, catalog())


def test_atomic_goal_normalization_preserves_all_requirements_and_legacy_cap():
    from careerforge.services.jd import freeze_requirements, validate_requirement_set
    from careerforge.services.jd_atomic import ATOMIC_JD_CONTRACT
    from careerforge.services.research_coverage import normalize_requirements

    labels = [f"自定义能力{i}" for i in range(41)]
    document = doc("；".join(labels))
    draft = AtomicRequirementExtractionOutput(
        requirements=[requirement(document, label, label, str(i)) for i, label in enumerate(labels)]
    )
    frozen = freeze_requirements(document, draft)
    assert frozen["jd_granularity_contract"] == ATOMIC_JD_CONTRACT
    validate_requirement_set(frozen, document)
    normalized, _ = normalize_requirements(frozen, source_text=document["raw_text"])
    assert len(normalized) == 41
    assert {row.requirement_id for row in normalized} == {
        row["requirement_id"] for row in frozen["requirements"]
    }
    with pytest.raises(DomainError, match="上限"):
        normalize_requirements(frozen["requirements"])
    with pytest.raises(DomainError, match="未知"):
        normalize_requirements({**frozen, "jd_granularity_contract": "future-unknown"})
