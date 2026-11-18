from copy import deepcopy

import pytest
from sqlalchemy import select
from support import FILES

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Publication, ReviewDecision


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "scoped_with_gaps", "global_with_local_gap", "invalid_path", "cancel", "invalid_read"])
async def test_same_run_repairs_only_new_frozen_windows(env, monkeypatch, mode):
    app, actor = env["app"], env["actor"]
    entry = "src/caller.py"
    src = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/retry",
            commit_sha="d" * 40,
            license_id="MIT",
            files={
                **FILES,
                entry: "from .tools import echo\n\ndef call(value):\n    return echo(value)\n",
            },
            source_metadata={"synthetic": True},
        ),
    )
    original = env["model"].complete

    async def complete(request):
        answer = await original(request)
        if not request.get("schema"):
            return answer
        data = deepcopy(answer.structured)
        context, role = request["context"], request["role"]
        if role == "analysis":
            data["mechanism_edges"] = []
            unit = data["units"][0]
            if "prior_review_findings" not in context:
                unit["claims"][0]["statement"] = "The wrong symbol returns value."
            else:
                caller = next(r for r in context["reading"]["reads"] if r["path"] == entry)
                tools = next(r for r in context["reading"]["reads"] if r["path"] == "src/tools.py")
                unit["claims"][0].update(
                    statement="call invokes echo in the fixture.",
                    support=[
                        {
                            "read_id": caller["read_id"],
                            "quote": "def call(value):\n    return echo(value)",
                        },
                        {"read_id": tools["read_id"], "quote": "def echo(value):"},
                    ],
                )
                data["mechanism_edges"] = [
                    {
                        "subject_key": unit["subject_key"],
                        "claim_key": "return",
                        "from_path": path,
                        "from_symbol": "call",
                        "to_path": "src/tools.py",
                        "to_symbol": "echo",
                    }
                    for path in (entry, "not-read.py")
                ]
        if role == "reviewer" and "frozen_source_inventory" not in context:
            reads = context["independent_bundle"]["reads"]
            data["claim_decisions"][0]["independent_reads"] = [
                {
                    "read_id": next(r["read_id"] for r in reads if r["path"] == entry),
                    "quote": "def call(value):\n    return echo(value)",
                },
                {
                    "read_id": next(r["read_id"] for r in reads if r["path"] == "src/tools.py"),
                    "quote": "def echo(value):",
                },
            ]
        if role == "reviewer" and "frozen_source_inventory" in context:
            if mode == "invalid_read":
                data["claim_decisions"][0]["independent_reads"][0]["read_id"] = "not-independent"
            row = data["claim_decisions"][0]
            row["outcome"] = "contradicted"
            row["counterevidence"] = list(row["independent_reads"])
            data["decision"] = "accept_scoped" if mode == "scoped_with_gaps" else "needs_evidence"
            data["blocking_issues"] = ["The first draft used a different symbol."]
            data["scope_review"]["standalone_claim_ids"] = []
            data["scope_review"]["issue_scopes"] = [
                {"issue_index": 0, "scope": "claim", "target_ids": [row["claim_id"]]},
            ]
            if mode == "global_with_local_gap":
                data["scope_review"]["global_blocking_issues"] = [
                    "The overall architecture still requires investigation."
                ]
            data["evidence_read_requests"] = [
                {
                    "issue_index": 0,
                    "target": {
                        "path": "unknown.py" if mode == "invalid_path" else entry,
                        "symbol": "call",
                        "start_line": 1,
                        "end_line": 4,
                        "purpose": "dispatch",
                    },
                }
            ] * 2
        if role == "coverage" and "independent_reads" in context:
            review = context["review"]
            data["relation_audit"] = {
                "confirmed_local_issue_indices": [],
                "topic_links": [],
                "facets": [],
            }
            if review["blocking_issues"]:
                for row in data["scope_audit"]["elements"]:
                    row["fact_bound"] = False
                    row["standalone"] = False
                data["all_facts_bound"] = False
                data["uncovered_elements"] = [
                    row["element_id"] for row in data["scope_audit"]["elements"]
                ]
        return answer.model_copy(update={"structured": data})

    monkeypatch.setattr(env["model"], "complete", complete)
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=src,
        question="Which frozen symbol returns value?",
        topic_keys=["agent.tool_calling"],
        max_units_per_project=1,
        auto_evidence_retry=True,
        call_cap=18,
        planning_call_seconds=30,
        idempotency_key="repair",
        mechanism_read_targets=[
            {
                "path": "src/tools.py",
                "symbol": "echo",
                "purpose": "result",
                "start_line": 1,
                "end_line": 3,
            }
        ],
    )
    with pytest.raises(DomainError) as budget:
        app.build.start(actor, body.model_copy(update={"call_cap": 14}))
    assert budget.value.code == "BUILD_BUDGET"
    run = app.build.start(actor, body)
    await app.execute(run)
    await app.execute(app.runtime.stage(run, "analysis_child")["run_id"])
    await app.execute(run)
    await app.execute(app.runtime.stage(run, "review_child")["run_id"])
    await app.execute(run)
    if mode == "cancel":
        with app.db.read() as db:
            revision = db.get(AgentRun, run).revision
        app.runtime.cancel(actor, run, revision)
    await app.drain()
    if mode == "invalid_read":
        with app.db.read() as db:
            assert db.get(AgentRun, run).status == "partially_succeeded"
        assert app.runtime.stage(run, "result")["publication_ids"] == []
        return
    assert app.build.start(actor, body) == run
    with app.db.read() as db:
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run)))
        published = list(db.scalars(select(Publication)))
        (first,) = [
            r
            for r in db.scalars(select(ReviewDecision))
            if r.reviewer_run_id == app.runtime.stage(run, "review_child")["run_id"]
        ]
        assert first.decision == "needs_evidence"
        assert db.get(AgentRun, run).calls_consumed <= body.call_cap
        assert len(published) == (1 if mode in {"success", "scoped_with_gaps", "global_with_local_gap"} else 0)
        assert len(children) == {"success": 4, "scoped_with_gaps": 4, "global_with_local_gap": 4, "invalid_path": 2, "cancel": 3}[mode]
        if mode == "cancel":
            assert db.get(AgentRun, run).status == "cancelled"
            assert (
                db.get(
                    AgentRun, app.runtime.stage(run, "analysis_retry_child")["run_id"]
                ).calls_consumed
                == 0
            )
    if mode in {"success", "scoped_with_gaps", "global_with_local_gap"}:
        result = app.runtime.stage(run, "result")
        assert result["evidence_retry"]["status"] == "re_reviewed"
        assert len(result["unit_version_ids"]) == 2
        assert len(result["evidence_retry"]["plan"]["retry_targets"]) == 1
        assert result["publication_ids"] == [published[0].id]
        (card,) = result["mechanism_cards"]
        (edge,) = card["edges"]
        assert edge["from_path"] == entry and edge["to_path"] == "src/tools.py"
        assert {r["path"] for r in edge["independent_citations"]} == {entry, "src/tools.py"}
        assert not card["limitations"]["complete_mechanism"]
    elif mode == "invalid_path":
        assert app.runtime.stage(run, "result")["evidence_retry"]["status"] == "no_new_read_target"


def test_tutor_fallback_progress_is_partial_even_with_a_checked_fact():
    from careerforge.services.answer_completion import tutor_answer_progress

    facts = [{"publication_id": "publication", "fact_atom_id": "fact", "checked": "exact_fallback"}]
    result = tutor_answer_progress({"task_satisfied": True}, facts)
    assert result["tutor_answer_status"] == "partially_answered"
    assert result["tutor_answer_progress"]["exact_fallback_only"]
    assert result["tutor_answer_progress"]["used_fact_ids"] == ["fact"]
    assert result["tutor_answer_progress"]["missing_aspects"]
    assert (
        tutor_answer_progress({"task_satisfied": False}, [])["tutor_answer_status"]
        == "insufficient_evidence"
    )
