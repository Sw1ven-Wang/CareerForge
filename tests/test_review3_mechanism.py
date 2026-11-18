"""New publication paths must retain the original authorization and both roles."""

from copy import deepcopy

import pytest

from careerforge.contracts import (
    ImportSourceInput,
    KnowledgeBuildInput,
    RetrievalTaskSpec,
    TaskType,
)
from careerforge.services.learning_intent import SCOPED_QUESTION_ROUTING_CONTRACT
from careerforge.services.orchestration import rule_intent


@pytest.mark.asyncio
@pytest.mark.parametrize("discover", [False, True])
@pytest.mark.parametrize("independent_second_endpoint", [True, False])
async def test_mechanism_publishes_only_dually_reviewed_calls(
    env, monkeypatch, independent_second_endpoint, discover
):
    app, actor = env["app"], env["actor"]
    caller, callee = "entry.py", "tools.py"
    target = {
        "key": "invoke",
        "caller": {
            "path": caller,
            "symbol": "dispatch",
            "purpose": "dispatch",
            "start_line": 1,
            "end_line": 2,
        },
        "callee": {
            "path": callee,
            "symbol": "echo",
            "purpose": "result",
            "start_line": 1,
            "end_line": 2,
        },
    }
    code = {
        caller: "def dispatch(value):\n    return echo(value)\n",
        callee: "def echo(value):\n    return value\n",
    }
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixture/mechanism",
            commit_sha="d" * 40,
            license_id="MIT",
            files=code,
            source_metadata={"synthetic": True},
        ),
    )
    complete = env["model"].complete

    def support(reads, paths):
        return [
            {
                "read_id": next(x["read_id"] for x in reads if x["path"] == path),
                "quote": code[path].strip(),
            }
            for path in paths
        ]

    async def model(request):
        context = request["context"]
        if (request.get("schema") or {}).get("title") in {
            "DiscoveryOutput",
            "StagedDiscoveryOutput",
        }:
            from careerforge.contracts import ModelOutput

            (candidate,) = context["call_candidates"]
            return ModelOutput(
                structured={
                    "edges": [
                        {"candidate_id": candidate["candidate_id"], "phase": "entry"},
                        {"candidate_id": "not-in-the-index", "phase": "dispatch"},
                    ],
                    "read_window_ids": [],
                },
                synthetic=True,
            )
        response = await complete(request)
        if not request.get("schema"):
            return response
        data = deepcopy(response.structured)
        context = request["context"]
        if request["role"] == "analysis":
            unit = data["units"][0]
            unit["claims"][0].update(
                statement="dispatch calls echo in the scoped fixture.",
                support=support(context["reading"]["reads"], [caller, callee]),
            )
            data["mechanism_edges"] = [
                {
                    "subject_key": unit["subject_key"],
                    "claim_key": "return",
                    "from_path": caller,
                    "from_symbol": "dispatch",
                    "to_path": callee,
                    "to_symbol": "echo",
                }
            ]
        elif request["role"] == "reviewer":
            paths = [caller, callee] if independent_second_endpoint else [caller]
            data["claim_decisions"][0]["independent_reads"] = support(
                context["independent_bundle"]["reads"], paths
            )
        elif request["role"] == "coverage" and "independent_reads" in context:
            (candidate,) = context["subject"]["mechanism_candidates"]
            data["approved_mechanism_edges"] = [
                {
                    "edge_key": candidate["edge_key"],
                    "claim_id": candidate["claim_id"],
                }
            ]
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [],
                "topic_links": [
                    {
                        "annotation_id": context["review"]["accepted_topic_link_ids"][0],
                        "claim_ids": [candidate["claim_id"]],
                        "independent_read_ids": [
                            x["read_id"]
                            for x in context["review"]["claim_decisions"][0]["independent_reads"]
                        ],
                    }
                ],
                "facets": [],
            }
        return response.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", model)
    build = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=source,
        question="Explain dispatch and echo call flow",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        call_cap=11 if discover else 10,
        idempotency_key="mechanism",
        mechanism_edge_targets=[] if discover else [target],
        discover_mechanisms=discover,
    )
    run = app.build.start(actor, build)
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result and result["publication_ids"], result
    assert app.build.start(actor, build) == run
    (card,) = result["mechanism_cards"]
    expected = "reviewed" if independent_second_endpoint else "unknown"
    assert card["required_edges"][0]["status"] == expected
    assert card["complete_mechanism"] is False
    if discover:
        assert result["mechanism_discovery"]["invalid_choices"] == 1
        assert card["phase_evidence"]["entry"]["status"] == expected
        assert card["phase_evidence"]["result"]["status"] == "unknown"
        assert card["phase_evidence"]["validation"]["status"] == "unknown"
        assert card["reviewed_edge_connections"] == []
    coverage = result["learning_coverage_manifest"]["sources"][0]
    assert coverage["required_edges"][0]["status"] == expected
    assert bool(coverage["next_read_targets"]) is not independent_second_endpoint
    evidence = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id="proof",
            user_question="dispatch echo",
            task_type=TaskType.IMPLEMENTATION,
            project_source_ids=[source],
            topic_keys=["agent.tool_calling"],
            focus_revision=1,
        ),
    )
    (unit,) = evidence["knowledge_units"]
    assert len(unit["mechanism_edges"]) == int(independent_second_endpoint)
    if independent_second_endpoint:
        (edge,) = unit["mechanism_edges"]
        assert edge["edge_key"] == card["required_edges"][0]["key"]
        assert all(edge["independent_read_ids"].values())
        assert all(edge["analysis_evidence_ids"].values())


def test_fixed_scope_routes_descriptive_questions_without_permission_request():
    focus = {"project_source_ids": ["source"], "topic_keys": ["agent.tool_calling"]}
    kwargs = {"routing_contract_version": SCOPED_QUESTION_ROUTING_CONTRACT}
    assert (
        rule_intent("什么时候切到顺序模式？", focus, **kwargs).interpreted_intent
        == TaskType.IMPLEMENTATION
    )
    assert rule_intent("什么时候切到顺序模式？", {}, **kwargs) is None
    assert (
        rule_intent("我没有权限访问另一个用户的私有知识，能给我其内容吗？", focus, **kwargs) is None
    )


def test_discovery_without_selected_edges_uses_the_relation_audit_schema():
    from types import SimpleNamespace

    from careerforge.contracts_scoped_review_v166 import (
        MECHANISM_PUBLICATION_CONTRACT,
        TARGETED_REVIEW_CONTRACT,
        MechanismCoverageOutput,
        RelationScopedCoverageOutput,
    )
    from careerforge.services.knowledge import KnowledgeService

    manifest = {
        "targeted_review_contract": TARGETED_REVIEW_CONTRACT,
        "mechanism_publication_contract": MECHANISM_PUBLICATION_CONTRACT,
        "mechanism_edge_targets": [],
    }
    child = SimpleNamespace(input_manifest=manifest)
    assert KnowledgeService._coverage_type(child) is RelationScopedCoverageOutput
    manifest["mechanism_edge_targets"] = [{"key": "a"}]
    assert KnowledgeService._coverage_type(child) is MechanismCoverageOutput
