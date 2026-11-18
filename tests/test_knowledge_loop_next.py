"""Deterministic regression for targeted review and comparison safety."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from support import FILES

from careerforge.contracts import (
    ImportSourceInput,
    KnowledgeBuildInput,
    RetrievalTaskSpec,
    TaskType,
)
from careerforge.contracts_jd_v166 import RequirementDraft
from careerforge.models import Publication, ReviewDecision
from careerforge.services.dependencies import invalidate
from careerforge.services.jd import _primary_topic
from careerforge.services.orchestration import _mentioned_sources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relation,global_issue", [("illustrates", False), ("implements", False), ("illustrates", True)]
)
async def test_independent_read_targets_and_subset_topic(env, monkeypatch, relation, global_issue):
    app, actor = env["app"], env["actor"]
    src = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/targeted",
            commit_sha="b" * 40,
            license_id="MIT",
            files={**FILES, "src/registry.py": "register(echo)\n"},
            source_metadata={"synthetic": True},
        ),
    )
    original = env["model"].complete

    async def complete(request):
        answer = await original(request)
        if not request.get("schema"):
            return answer
        data = deepcopy(answer.structured)
        context = request["context"]
        role = request["role"]
        if role == "analysis":
            data["units"][0]["topics"][0]["relation"] = relation
            data["units"][0]["facets"] = [
                {
                    "topic_key": "agent.tool_calling",
                    "facet_key": "dispatch",
                    "value_status": "known",
                    "value": "echo dispatch",
                    "claim_keys": ["return"],
                    "applicability_reason": "test only",
                }
            ]
        if role == "reviewer":
            data["decision"] = "needs_evidence"
            data["accepted_facet_ids"] = []
            data["blocking_issues"] = ["No evidence for facet"]
            data["scope_review"]["issue_scopes"] = [
                {
                    "issue_index": 0,
                    "scope": "facet",
                    "target_ids": [context["subject"]["facets"][0]["id"]],
                }
            ]
        if role == "coverage" and "independent_reads" in context:
            review = context["review"]
            row = review["claim_decisions"][0]
            data["scope_audit"]["global_blocking_issues"] = (
                ["Real global blocker"] if global_issue else []
            )
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [0],
                "topic_links": [
                    {
                        "annotation_id": review["accepted_topic_link_ids"][0],
                        "claim_ids": [row["claim_id"]],
                        "independent_read_ids": [row["independent_reads"][0]["read_id"]],
                    }
                ],
                "facets": [],
            }
        return answer.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", complete)
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=src,
        question="Tool Calling 示例",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        call_cap=10,
        idempotency_key="targeted",
        mechanism_read_targets=[
            {
                "path": "src/registry.py",
                "symbol": "register",
                "purpose": "registration",
                "start_line": 1,
                "end_line": 1,
            },
            {
                "path": "src/tools.py",
                "symbol": "echo",
                "purpose": "result",
                "start_line": 1,
                "end_line": 3,
            },
        ],
    )
    run = app.build.start(actor, body)
    await app.drain()
    root = app.runtime.stage(run, "result")
    assert root and len(root["evidence_gaps"]) == 1, root
    assert root["evidence_gaps"][0]["subject_type"] == "facet"
    assert app.build.start(actor, body) == run
    review_request = next(
        c
        for c in env["model"].calls
        if c["role"] == "reviewer" and "independent_bundle" in c["context"]
    )
    bundle = review_request["context"]["independent_bundle"]
    assert {r["path"] for r in bundle["reads"]} >= {"src/registry.py", "src/tools.py"}
    assert all(row["status"] == "read" for row in bundle["mechanism_targets"])
    if global_issue:
        assert not root["publication_ids"]
        return

    (pid,) = root["publication_ids"]
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, pid)
        review = db.get(ReviewDecision, db.get(Publication, pid).review_id)
        assert review.decision == "accept_partial_scoped"
        assert pack["facts"] and not pack["facets"]
        assert [link["relation"] for link in pack["topic_links"]] == (
            ["illustrates"] if relation == "illustrates" else []
        )
    if relation == "illustrates":
        tech = await app.retrieval.retrieve(
            actor,
            RetrievalTaskSpec(
                turn_id="technology",
                user_question="Tool Calling",
                task_type=TaskType.CONCEPT,
                topic_keys=["agent.tool_calling"],
                focus_revision=1,
            ),
        )
        assert pid in {u["publication_id"] for u in tech["knowledge_units"]}
        with app.db.transaction() as db:
            invalidate(db, actor, pack["topic_links"][0]["id"], "revoked", "fixture link revoked")
        with app.db.read() as db:
            current = app.knowledge.publication_pack(db, actor, pid)
            assert current["facts"] and not current["topic_links"]


def test_comparison_resolves_unique_short_project_alias_without_expanding_regular_focus():
    names = [{"id": "a", "name": "example/pi"}, {"id": "b", "name": "example/KamaClaude"}]
    assert _mentioned_sources("比较 Pi 和 KamaClaude", names) == ["b"]
    assert _mentioned_sources("比较 Pi 和 KamaClaude", names, comparison_aliases=True) == ["a", "b"]
    duplicate = [*names, {"id": "c", "name": "other/pi"}]
    assert _mentioned_sources("比较 Pi 和 KamaClaude", duplicate, comparison_aliases=True) == ["b"]


@pytest.mark.asyncio
async def test_compare_reports_evidence_gap_if_second_project_is_absent(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    evidence = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id="comparison",
            user_question="compare Tool Calling",
            task_type=TaskType.COMPARE,
            project_source_ids=[source],
            topic_keys=["agent.tool_calling"],
            focus_revision=1,
        ),
    )
    result = await app.learning.compare(
        actor, evidence, ["agent.tool_calling"], expected_sources=[source, "other"]
    )
    assert result["kind"] == "evidence_gap"
    assert result["comparison_status"] == "comparison_insufficient_evidence"
    assert len(result["comparison_sources"]) == 2
    assert result["comparison_sources"][1]["status"] == "no_reviewed_material"

    diagnosed = await app.learning.compare(
        actor,
        evidence,
        ["agent.tool_calling"],
        expected_sources=[source, "other"],
        scoped_conditions=True,
        diagnostics=True,
    )
    assert diagnosed["comparison_sources"][0]["gap_reasons"] == ["missing_facet"]
    assert diagnosed["comparison_sources"][1]["gap_reasons"] == ["scope_excluded"]


def test_registry_alias_needs_source_mention_and_unique_authorized_topic():
    row = RequirementDraft(
        key="runtime",
        technology="Redis",
        keywords=["Redis"],
        depth=None,
        depth_basis="unspecified",
        modality="required",
        group_operator="SINGLE",
        topic_key="other.runtime",
        paragraph_id="paragraph-1",
        span_start=0,
        span_end=8,
        quote="熟悉 Redis",
    )
    aliases = {"runtime.redis": ["Redis"], "other.runtime": ["Redis"]}
    assert _primary_topic(row, {"runtime.redis"}, aliases) == "runtime.redis"
    assert _primary_topic(row, set(aliases), aliases) is None
    assert (
        _primary_topic(
            row.model_copy(update={"quote": "熟悉 RedisMock"}), {"runtime.redis"}, aliases
        )
        is None
    )


@pytest.mark.asyncio
async def test_paired_facets_keep_different_conditions_and_citations(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    original = env["model"].complete

    async def complete(request):
        answer = await original(request)
        if request["role"] != "analysis" or not request.get("schema"):
            return answer
        result = deepcopy(answer.structured)
        unit = result["units"][0]
        repo = request["context"]["repo_map"]["repository"]
        unit["claims"][0]["conditions"] = [
            "first trigger" if repo.endswith("/one") else "second trigger"
        ]
        unit["facets"] = [
            {
                "topic_key": "agent.tool_calling",
                "facet_key": "failure_boundary",
                "value_status": "known",
                "value": "same words",
                "claim_keys": ["return"],
                "applicability_reason": "fixture",
            }
        ]
        return answer.model_copy(update={"structured": result})

    monkeypatch.setattr(env["model"], "complete", complete)
    a, _, _ = await publish_fixture(env, key="compare-one", repository="fixtures/one")
    b, _, _ = await publish_fixture(env, key="compare-two", repository="fixtures/two")
    pack = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id="condition-comparison",
            user_question="Compare failure boundaries",
            task_type=TaskType.COMPARE,
            project_source_ids=[a, b],
            topic_keys=["agent.tool_calling"],
            focus_revision=1,
        ),
    )
    comparison = await app.learning.compare(
        actor, pack, ["agent.tool_calling"], expected_sources=[a, b], scoped_conditions=True
    )
    assert comparison["kind"] == "comparison"
    scope = comparison["comparison"]["condition_scope"]
    dimension = next(d for d in scope["dimensions"] if d["facet_key"] == "failure_boundary")
    assert dimension["trigger_alignment"] == "different_recorded_conditions"
    assert not scope["quality_ranking_supported"]
    assert {c["repository"] for c in comparison["citations"]} == {"fixtures/one", "fixtures/two"}
    assert {
        tuple(f["conditions"]) for side in dimension["sides"] for f in side["reviewed_facts"]
    } == {
        ("first trigger",),
        ("second trigger",),
    }
