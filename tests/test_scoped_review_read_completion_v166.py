"""Real local read receipts and synthetic model calls; no native quality claims."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from careerforge.contracts import ImportSourceInput, ModelOutput, ToolCall
from careerforge.contracts_scoped_review_v166 import REVIEW_READ_COMPLETION, SCOPED_REVIEW_VERSION
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt, SourceRead
from careerforge.services.dependencies import invalidate
from careerforge.services.investigation import Investigation


def setup_reader(env, *, reads=3, tools=8, lines=150, completion=REVIEW_READ_COMPLETION, role="reviewer"):
    app, actor = env["app"], env["actor"]
    content = "".join(f"v{i} = {i}\n" for i in range(1, 301))
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/scoped-read",
            commit_sha="a" * 40,
            license_id="MIT",
            files={"source.py": content},
            source_metadata={"synthetic": True},
        ),
    )
    contract = {
        "version": "bounded-investigation-1.6.3",
        "max_rounds": 1,
        "max_tools": tools,
        "max_reads": reads,
        "max_batch": 3,
        "bootstrap_reads": 0,
    }
    manifest = {
        "source_ids": [source],
        "knowledge_review_contract_version": SCOPED_REVIEW_VERSION,
        "analysis_read_completion" if role == "analysis" else "review_read_completion": completion,
    }
    run = app.runtime.create(actor, env["ns"], env["funding"], "analysis" if role == "analysis" else "review", manifest, 3, "scope-read")
    epoch = app.runtime.claim(run, "reader")
    inv = Investigation(
        app.sources,
        app.runtime,
        app.gateway,
        actor,
        run,
        epoch,
        "reader",
        source,
        role,
        "reviewer:unit",
        contract=contract,
        read_completion_contract=completion,
    )

    def response(request):
        return ModelOutput(
            synthetic=True,
            tool_calls=[
                ToolCall(
                    id="read",
                    name="read_span",
                    arguments={"path": "source.py", "start_line": 1, "end_line": lines},
                )
            ],
            finish_reason="tool_calls",
        )

    env["model"].overrides[role] = response
    return inv, source, content


@pytest.mark.asyncio
@pytest.mark.parametrize("reads,expected_end", [(6, 300), (2, 140)])
async def test_analysis_requested_range_pages_share_budget_and_replay(env, reads, expected_end):
    from careerforge.services.investigation import ANALYSIS_READ_COMPLETION
    inv, source, content = setup_reader(env, reads=reads, tools=12, lines=300,
        completion=ANALYSIS_READ_COMPLETION, role="analysis")
    bundle = await inv.run("v1", {})
    assert bundle["reads"][-1]["end_line"] == expected_end
    assert bundle["read_attempts"] <= reads
    assert await inv.run("v1", {}) == bundle
    with env["app"].db.read() as db:
        assert all(r.role == "analysis" for r in db.scalars(select(SourceRead)))
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 1


@pytest.mark.asyncio
async def test_two_completion_pages_use_same_read_budget_and_no_extra_model_call(env):
    inv, source, content = setup_reader(env)
    bundle = await inv.run("v1", {})
    assert len(bundle["reads"]) == 3
    assert bundle["read_attempts"] == 3
    assert bundle["tool_count"] == 5
    assert bundle["round_count"] == 1
    assert bundle["read_completion"]["completed_pages"] == 2
    assert bundle["read_completion"]["new_source_ranges"] == 0
    assert bundle["reads"][-1]["end_line"] == 150
    assert "".join(r["content"] for r in bundle["reads"]) == "".join(
        content.splitlines(keepends=True)[:150]
    )
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 1
        rows = db.scalars(select(SourceRead)).all()
        assert len(rows) == 3
        assert all(
            r.run_id == inv.run_id and r.source_id == source and r.role == "reviewer" for r in rows
        )
    again = await inv.run("v1", {})
    assert again == bundle
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 3
        assert db.get(AgentRun, inv.run_id).calls_consumed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reads,tools,expected", [(1, 8, 1), (2, 8, 2), (3, 3, 1), (3, 4, 2)])
async def test_completion_cannot_reset_or_overrun_existing_budget(env, reads, tools, expected):
    inv, _, _ = setup_reader(env, reads=reads, tools=tools)
    bundle = await inv.run("v1", {})
    assert len(bundle["reads"]) == expected
    assert bundle["read_attempts"] <= reads
    assert bundle["tool_count"] <= tools
    assert bundle["reads"][-1]["continuation"] is not None
    assert await inv.run("v1", {}) == bundle


@pytest.mark.asyncio
async def test_max_two_extra_pages_leaves_long_request_explicitly_unread(env):
    inv, _, _ = setup_reader(env, reads=9, lines=300)
    bundle = await inv.run("v1", {})
    assert len(bundle["reads"]) == 3
    assert bundle["reads"][-1]["continuation"] is not None
    assert bundle["reads"][-1]["end_line"] < 300
    assert bundle["read_completion"]["completed_pages"] == 2


@pytest.mark.asyncio
async def test_unknown_model_receipt_never_retries_or_creates_more_reads(env):
    inv, _, _ = setup_reader(env)
    await inv.run("v1", {})
    with env["app"].db.transaction() as db:
        db.scalar(select(ModelReceipt)).status = "unknown"
    calls = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await inv.run("v1", {})
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert len(env["model"].calls) == calls
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 3


@pytest.mark.asyncio
async def test_revoked_source_stops_completion_before_additional_read(env):
    inv, source, _ = setup_reader(env)
    inv._freeze_contract()
    value = inv.execute_tool(
        "manual", "read_span", {"path": "source.py", "start_line": 1, "end_line": 150}
    )
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], source, "revoked", "synthetic revocation")
    with pytest.raises(DomainError):
        inv._complete_requested_read("manual", value)
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1


@pytest.mark.parametrize("mutation", ["path", "range", "read_id", "content"])
def test_forged_continuation_cannot_expand_original_request(env, mutation):
    inv, _, _ = setup_reader(env)
    inv._freeze_contract()
    value = inv.execute_tool(
        "manual", "read_span", {"path": "source.py", "start_line": 1, "end_line": 150}
    )
    forged = deepcopy(value)
    if mutation == "path":
        forged["continuation"]["path"] = "other.py"
    elif mutation == "range":
        forged["continuation"]["end_line"] = 300
    elif mutation == "read_id":
        forged["read_id"] = "unread"
    else:
        forged["content"] += "forged"
    with pytest.raises(DomainError):
        inv._complete_requested_read("manual", forged)
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1


@pytest.mark.asyncio
async def test_v2_completes_long_requested_range_without_hidden_truncation(env):
    from careerforge.contracts_scoped_review_v166 import REVIEW_READ_COMPLETION_V2
    inv, _, content = setup_reader(env, reads=24, tools=36, lines=300, completion=REVIEW_READ_COMPLETION_V2)
    bundle = await inv.run("v1", {})
    assert bundle["reads"][-1]["end_line"] == 300
    assert bundle["read_completion"]["completed_pages"] > 2
    assert bundle["pending_requested_ranges"] == []
    assert bundle["read_coverage_complete"] is True
    assert await inv.run("v1", {}) == bundle


@pytest.mark.asyncio
async def test_v2_cannot_claim_sufficiency_when_budget_leaves_unread_pages(env):
    from careerforge.contracts_scoped_review_v166 import REVIEW_READ_COMPLETION_V2
    inv, _, _ = setup_reader(env, reads=1, tools=8, lines=300, completion=REVIEW_READ_COMPLETION_V2)
    def response(request):
        return ModelOutput(synthetic=True, tool_calls=[
            ToolCall(id="read", name="read_span", arguments={"path":"source.py", "start_line":1,"end_line":300}),
            ToolCall(id="finish", name="finish_investigation", arguments={"reason":"evidence_sufficient"})
        ],finish_reason="tool_calls")
    env["model"].overrides["reviewer"] = response
    bundle = await inv.run("v1", {})
    assert bundle["stop_reason"] == "requested_evidence_incomplete"
    assert bundle["pending_requested_ranges"]
    assert bundle["read_attempts"] == 1
    assert await inv.run("v1", {}) == bundle


@pytest.mark.asyncio
async def test_v2_finish_reports_explicit_candidate_file_not_read(env):
    from careerforge.contracts_scoped_review_v166 import REVIEW_READ_COMPLETION_V2
    inv, _, _ = setup_reader(env, reads=24, tools=36, completion=REVIEW_READ_COMPLETION_V2)
    env["model"].overrides["reviewer"] = lambda request: ModelOutput(synthetic=True, tool_calls=[
        ToolCall(id="finish",name="finish_investigation",arguments={"reason":"evidence_sufficient"})],finish_reason="tool_calls")
    bundle = await inv.run("unrelated", {"review_subject":{"claims":[{"statement":"source.py implements this operation"}]}})
    assert bundle["stop_reason"] == "requested_evidence_incomplete"
    assert bundle["unread_claim_paths"] == ["source.py"]
