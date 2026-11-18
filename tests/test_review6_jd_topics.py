from types import SimpleNamespace

from careerforge.services.jd_topics import (
    JD_TOPIC_MAPPING,
    mapped_learning_topics,
    plan_topic_coverage,
    topic_links,
)


def item(identifier, technology, quote, *, modality="required", operator="SINGLE"):
    return {
        "requirement_id": identifier,
        "technology": technology,
        "quote": quote,
        "modality": modality,
        "group_operator": operator,
        "paragraph_id": "p1",
        "span_start": 0,
        "span_end": len(quote),
    }


def test_jd_topic_mapping_distinguishes_siblings_and_unconfirmed_terms():
    tool = "agent.tool_calling"
    versions = {tool: 1, "storage.redis": 1, "storage.pgvector": 1, "framework.langgraph": 1}
    aliases = {
        tool: ["Tool Calling"],
        "storage.redis": ["Redis"],
        "storage.pgvector": ["pgvector"],
        "framework.langgraph": ["LangGraph"],
    }
    wide = "使用 Redis、pgvector 或完整 LangGraph 不作硬性要求；相关经验仅加分。"
    rows = topic_links(
        [
            item("r1", "Redis", wide, modality="not_required", operator="NOT"),
            item("r2", "pgvector 相关经验", wide, modality="bonus", operator="OR"),
            item("r3", "参数校验", "熟悉 Tool Calling，了解参数校验。"),
            item("r4", "特殊调度方式", "掌握特殊调度方式。"),
        ],
        versions,
        aliases,
        {tool: ["argument_validation"]},
    )
    assert [(row["status"], [link["topic_key"] for link in row["topics"]]) for row in rows] == [
        ("excluded", ["storage.redis"]),
        ("mapped", ["storage.pgvector"]),
        ("mapped", [tool]),
        ("pending_confirmation", []),
    ]
    assert rows[2]["facet_keys"] == ["argument_validation"]
    assert rows[3]["proposal"]["reason"] == "no_anchored_authorized_alias"
    assert rows[3]["proposal"]["span_end"] == len("掌握特殊调度方式。")
    assert mapped_learning_topics(
        {
            "topic_link_contract": JD_TOPIC_MAPPING,
            "requirement_topic_links": rows,
        }
    ) == ["storage.pgvector", tool]


def test_plan_links_only_a_selected_fact_that_supports_the_requested_facet():
    key = "agent.tool_calling"
    rows = topic_links(
        [item("r1", "参数校验", "了解参数校验。")],
        {key: 1},
        {key: []},
        {key: ["argument_validation"]},
    )
    frozen = {
        "goal": {
            "goal_version_id": "goal",
            "requirements": {
                "topic_link_contract": JD_TOPIC_MAPPING,
                "requirement_topic_links": rows,
                "groups": [],
            },
        },
        "units": [
            {
                "publication_id": "pub",
                "topic_links": [{"topic_key": key, "topic_version": 1, "relation": "implements"}],
                "facets": [
                    {
                        "topic_key": key,
                        "schema_version": 1,
                        "facet_key": "argument_validation",
                        "value_status": "known",
                        "claim_ids": ["specific"],
                    }
                ],
                "facts": [
                    {"fact_atom_id": "selected", "claim_id": "specific"},
                    {"fact_atom_id": "unrelated", "claim_id": "unrelated"},
                ],
            }
        ],
    }

    def projected(facts):
        module = SimpleNamespace(key="learn", publication_id="pub", focus_fact_ids=facts)
        return plan_topic_coverage(frozen, [module])["jd_topic_coverage"]["requirements"][0]

    assert projected(["unrelated"])["plan_status"] == "no_selected_reviewed_material"
    assert projected(["unrelated", "selected"])["anchors"] == [
        {"module_key": "learn", "publication_id": "pub", "fact_atom_ids": ["selected"]},
    ]
