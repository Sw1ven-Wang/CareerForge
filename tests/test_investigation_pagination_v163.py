"""Offline reproductions of single-round, hidden-limit and fail-all read defects.

These exercise the real gateway/receipt and source services with a declared
synthetic model. They establish mechanics and safety, not live model quality.
"""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from careerforge.contracts import ImportSourceInput, ModelOutput, ToolCall
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt, SourceRead, SourceSet, StageArtifact
from careerforge.services.dependencies import invalidate
from careerforge.services.investigation import (
    INVESTIGATION_CONTRACT,
    Investigation,
    investigation_contract,
    tool_specs,
)
from careerforge.services.sources import READ_MAX_CHARACTERS, SOURCE_READ_CONTRACT, SourceService
from careerforge.util import digest

CONTRACT = {
    "version": INVESTIGATION_CONTRACT,
    "max_rounds": 4,
    "max_tools": 16,
    "max_reads": 12,
    "max_batch": 3,
    "bootstrap_reads": 0,
}
FILES = {
    "LICENSE": "MIT\n",
    "README.md": "# Documentation\nreturn value explains an example.\n",
    "docs/guide.md": "return value user guide\n",
    "src/a_bootstrap.py": "def initial(value):\n    return value\n",
    "src/target.py": "def precise_target(value):\n    result = value + 1\n    return result\n",
}


def source_for(env, files=None, name="pagination"):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/" + name,
            commit_sha="a" * 40,
            license_id="MIT",
            files=files or FILES,
            source_metadata={"synthetic": True},
        ),
    )


def investigation_for(env, contract=CONTRACT, source=None, role="analysis", key="bounded"):
    app = env["app"]
    source = source or source_for(env)
    run = app.runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "analysis",
        {"source_ids": [source], "investigation_contract": contract},
        10,
        key,
    )
    epoch = app.runtime.claim(run, "reader")
    assert epoch is not None
    inv = Investigation(
        app.sources,
        app.runtime,
        app.gateway,
        env["actor"],
        run,
        epoch,
        "reader",
        source,
        role,
        role + ":" + source,
        contract=contract,
    )
    return inv


def output(*calls):
    return ModelOutput(
        synthetic=True,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(id=str(i), name=name, arguments=args) for i, (name, args) in enumerate(calls)
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
    )


def finish():
    return output(("finish_investigation", {"reason": "evidence_sufficient"}))


def span(path="src/target.py", start=1, end=3):
    return {"path": path, "start_line": start, "end_line": end}


def page(env, source, path, start, end, **kwargs):
    return env["app"].sources.read_span(
        env["actor"], source, path, start, end, "analysis", "page-fixture", **kwargs
    )


def test_legacy_read_limit_reproduced_and_exact_unicode_pages_cover_original(env):
    body = "".join(f"# {index:03d}: {'中' * 100}\r\n" for index in range(80))
    source = source_for(env, {"wide.py": body})
    with pytest.raises(DomainError, match="2600") as error:
        page(env, source, "wide.py", 1, 70)
    assert error.value.code == "READ_LIMIT"
    args = {"path": "wide.py", "start": 1, "end": 80}
    observed = []
    while args is not None:
        result = page(env, source, **args, paginate=True)
        observed.append(result)
        assert result["read_contract"] == SOURCE_READ_CONTRACT
        assert len(result["content"]) <= READ_MAX_CHARACTERS
        assert result["end_line"] - result["start_line"] + 1 <= 70
        assert result["content"] == "".join(
            body.splitlines(keepends=True)[result["start_line"] - 1 : result["end_line"]]
        )
        assert result["quote_hash"] == digest(result["content"])
        assert "…" not in result["content"] and "truncated" not in result["content"]
        with env["app"].db.transaction() as db:
            read, _ = SourceService.validated_read(db, env["actor"], result["read_id"])
            assert read.text == result["content"]
            SourceService.evidence_from_read(
                db,
                env["actor"],
                env["ns"],
                source,
                result["read_id"],
                result["content"],
                "analysis",
                "page-fixture",
            )
        continuation = result["continuation"]
        args = (
            {
                "path": continuation["path"],
                "start": continuation["start_line"],
                "end": continuation["end_line"],
            }
            if continuation
            else None
        )
    assert len(observed) > 1
    assert "".join(item["content"] for item in observed) == body
    assert observed[-1]["truncated"] is False


def test_legacy_line_limit_reproduced_and_new_read_returns_continuation(env):
    source = source_for(env, {"many.py": "".join(f"# {i}\n" for i in range(90))})
    with pytest.raises(DomainError) as error:
        page(env, source, "many.py", 1, 90)
    assert error.value.code == "READ_RANGE"
    result = page(env, source, "many.py", 1, 90, paginate=True)
    assert (result["start_line"], result["end_line"]) == (1, 70)
    assert result["continuation"] == {"path": "many.py", "start_line": 71, "end_line": 90}
    assert page(env, source, "many.py", 1, 90, paginate=True) == result


def test_irreducible_long_line_does_not_erase_prior_page_or_forge_read(env):
    source = source_for(env, {"wide.py": "# valid prefix\n" + "中" * 2700 + "\n# suffix\n"})
    first = page(env, source, "wide.py", 1, 3, paginate=True)
    assert first["content"] == "# valid prefix\n"
    assert first["continuation"]["start_line"] == 2
    with pytest.raises(DomainError) as error:
        page(env, source, "wide.py", 2, 3, paginate=True)
    assert error.value.code == "READ_LINE_LIMIT"
    assert error.value.details["splittable"] is False
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1
        assert (
            SourceService.validated_read(db, env["actor"], first["read_id"])[0].text
            == first["content"]
        )


@pytest.mark.parametrize("start,end", [(True, 2), (1, False), ("1", 2), (1, [2])])
def test_read_types_are_controlled_errors_not_python_crashes(env, start, end):
    source = source_for(env)
    with pytest.raises(DomainError) as error:
        page(env, source, "src/target.py", start, end, paginate=True)
    assert error.value.code == "TOOL_ARGS"


def test_exact_symbol_does_not_include_neighbor_names_or_execute_code(env):
    source = source_for(
        env,
        {
            "symbols.py": (
                "raise RuntimeError('must never execute imported sources')\n"
                "def echo():\n    pass\n"
                "def echo_extra():\n    pass\n"
                "class Service:\n    def echo(self):\n        pass\n"
            )
        },
    )
    service = env["app"].sources
    legacy = service.search(env["actor"], source, "echo", symbols_only=True)
    exact = service.search(env["actor"], source, "echo", symbols_only=True, exact_symbol=True)
    qualified = service.search(env["actor"], source, "Service.echo", exact_symbol=True)
    assert {item["symbol"] for item in legacy} == {"echo", "echo_extra", "Service.echo"}
    assert {item["symbol"] for item in exact} == {"echo", "Service.echo"}
    assert [item["symbol"] for item in qualified] == ["Service.echo"]
    assert all(item["code_read"] is False for item in exact)


def test_new_schema_exposes_limits_and_preserves_legacy_schema():
    legacy = {item["function"]["name"]: item["function"] for item in tool_specs()}
    current = {item["function"]["name"]: item["function"] for item in tool_specs(CONTRACT)}
    assert legacy["read_span"]["parameters"]["properties"]["start_line"] == {"type": "integer"}
    assert current["read_span"]["parameters"]["properties"]["start_line"]["minimum"] == 1
    assert all(
        token in current["read_span"]["description"]
        for token in ["70", "2600", "continuation", "READ_LINE_LIMIT"]
    )
    assert "3" in current["search_text"]["description"]
    assert current["find_symbol"]["parameters"]["properties"]["exact"]["type"] == "boolean"
    assert "maxLength" not in legacy["search_text"]["parameters"]["properties"]["query"]


async def test_legacy_single_round_stops_after_search_but_new_rounds_read_and_finish(env):
    def scripted(request):
        round_number = request["context"].get("round", 1)
        if round_number == 1:
            return output(("find_symbol", {"query": "precise_target"}))
        if round_number == 2:
            hits = request["context"]["observations"][-1]["value"]["results"]
            assert hits[0]["symbol"] == "precise_target"
            return output(("read_span", span()))
        assert "return result" in request["context"]["observations"][-1]["value"]["content"]
        return finish()

    env["model"].overrides["analysis"] = scripted
    legacy = investigation_for(env, contract=None, key="legacy-search-only")
    old = await legacy.run("a query with no matches", {})
    assert old["trace"][-1]["tool"] == "find_symbol"
    assert len(env["model"].calls) == 1
    current = investigation_for(env, key="multi-round")
    result = await current.run("a query with no matches", {})
    assert result["round_count"] == 3
    assert result["stop_reason"] == "evidence_sufficient"
    assert [read["path"] for read in result["reads"]] == ["src/target.py"]
    assert result["tool_count"] == 5 and result["read_attempts"] == 1
    assert result["runtime_observed"] is False


async def test_recoverable_invalid_ranges_preserve_observations_and_charge_attempts(env):
    def scripted(request):
        round_number = request["context"]["round"]
        if round_number == 1:
            return output(("read_span", span(start=0)))
        if round_number == 2:
            assert request["context"]["observations"][-1]["value"]["error_code"] == "READ_RANGE"
            return output(("read_span", span(end=99)))
        if round_number == 3:
            errors = [
                row["value"]
                for row in request["context"]["observations"]
                if "error_code" in row["value"]
            ]
            assert len(errors) == 2 and all(error["file_lines"] == 3 for error in errors)
            assert request["context"]["remaining_reads"] == 10
            return output(("read_span", span()))
        return finish()

    env["model"].overrides["analysis"] = scripted
    inv = investigation_for(env)
    result = await inv.run("missing query", {})
    assert result["read_attempts"] == 3 and len(result["reads"]) == 1
    assert result["tool_count"] == 6 and result["round_count"] == 4
    with env["app"].db.read() as db:
        assert db.get(AgentRun, inv.run_id).calls_consumed == 4
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 4


async def test_malformed_argument_can_be_corrected_without_type_error(env):
    def scripted(request):
        index = request["context"]["round"]
        if index == 1:
            return output(("read_span", {**span(), "start_line": "1"}))
        if index == 2:
            assert request["context"]["observations"][-1]["value"]["error_code"] == "TOOL_ARGS"
            return output(("read_span", span()))
        return finish()

    env["model"].overrides["analysis"] = scripted
    result = await investigation_for(env).run("missing query", {})
    assert result["read_attempts"] == 2 and len(result["reads"]) == 1


async def test_reviewer_four_tool_batch_is_correctable_without_partial_execution(env):
    def scripted(request):
        index = request["context"].get("round", 1)
        if index == 1:
            return output(*[("list_tree", {})] * 4)
        if index == 2:
            error = request["context"]["observations"][-1]["value"]
            assert error["error_code"] == "TOOL_BATCH"
            assert error["executed_tools"] == 0 and error["charged_tool_attempts"] == 4
            return output(("read_span", span()))
        return finish()

    env["model"].overrides["reviewer"] = scripted
    legacy = investigation_for(env, contract=None, role="reviewer", key="legacy-batch")
    with pytest.raises(DomainError) as error:
        await legacy.run("missing query", {})
    assert error.value.code == "TOOL_BATCH"
    inv = investigation_for(env, role="reviewer", key="fixed-batch")
    result = await inv.run("missing query", {})
    assert result["round_count"] == 3 and result["tool_count"] == 8
    assert len(result["reads"]) == 1 and result["read_attempts"] == 1


async def test_bootstrap_reads_one_implementation_not_readme_or_license(env):
    inv = investigation_for(env, contract={**CONTRACT, "bootstrap_reads": 1})
    result = await inv.run("return value", {})
    assert len(result["reads"]) == 1
    assert result["reads"][0]["path"] == "src/a_bootstrap.py"
    assert result["read_attempts"] == 1
    assert result["tool_count"] == 4  # tree, search, one source page, finish


async def test_read_attempt_budget_counts_failed_and_duplicate_requests(env):
    def scripted(request):
        index = request["context"]["round"]
        if index <= 3:
            return output(("read_span", span(start=0 if index == 1 else 1)))
        assert request["context"]["observations"][-1]["value"]["error_code"] == "READ_BUDGET"
        return finish()

    env["model"].overrides["analysis"] = scripted
    inv = investigation_for(env, contract={**CONTRACT, "max_reads": 2})
    result = await inv.run("missing query", {})
    assert result["read_attempts"] == 2 and len(result["reads"]) == 1
    assert result["tool_count"] == 6
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1


async def test_oversized_batch_cannot_exceed_total_tool_budget(env):
    env["model"].overrides["analysis"] = lambda request: output(*[("read_span", span())] * 8)
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 5})
    result = await inv.run("missing query", {})
    assert result["tool_count"] == 5 and result["round_count"] == 1
    assert result["stop_reason"] == "tool_budget_exhausted"
    assert result["trace"][-1]["value"]["charged_tool_attempts"] == 3
    assert result["reads"] == []


async def test_finish_stops_later_tools_in_same_batch(env):
    env["model"].overrides["analysis"] = lambda request: output(
        ("finish_investigation", {"reason": "unsupported"}), ("read_span", span())
    )
    result = await investigation_for(env).run("missing query", {})
    assert result["reads"] == [] and result["tool_count"] == 3
    assert result["stop_reason"] == "unsupported"


async def test_multiround_replay_is_identical_without_duplicate_paid_calls(env):
    env["model"].overrides["analysis"] = lambda request: (
        output(("read_span", span())) if request["context"]["round"] == 1 else finish()
    )
    inv = investigation_for(env)
    first = deepcopy(await inv.run("missing query", {"topics": []}))
    call_count = len(env["model"].calls)
    second = await inv.run("missing query", {"topics": []})
    assert second == first and len(env["model"].calls) == call_count == 2
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1
        assert db.get(AgentRun, inv.run_id).calls_consumed == 2


async def test_frozen_contract_change_fails_before_model_or_source_body(env):
    inv = investigation_for(env)
    await inv.run("missing query", {})
    inv.contract = {**inv.contract, "max_rounds": 2}
    calls = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await inv.run("missing query", {})
    assert error.value.code == "TOOL_REPLAY" and len(env["model"].calls) == calls


async def test_legacy_tool_receipts_cannot_be_silently_upgraded(env):
    inv = investigation_for(env, contract=None)
    await inv.run("missing query", {})
    inv.contract = investigation_contract(CONTRACT)
    with pytest.raises(DomainError) as error:
        await inv.run("missing query", {})
    assert error.value.code == "TOOL_REPLAY"


def test_same_tool_key_replay_does_not_double_count_or_reset_durable_quota(env):
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 2})
    first = inv.execute_tool("one", "list_tree", {})
    assert inv.execute_tool("one", "list_tree", {}) == first and inv.count == 1
    other = Investigation(
        inv.sources,
        inv.runtime,
        inv.gateway,
        inv.actor,
        inv.run_id,
        inv.epoch,
        inv.worker,
        inv.source_id,
        inv.role,
        inv.session_key,
        contract=inv.contract,
    )
    other.execute_tool("two", "list_tree", {})
    with pytest.raises(DomainError) as error:
        other.execute_tool("three", "list_tree", {})
    assert error.value.code == "TOOL_BUDGET"
    with pytest.raises(DomainError) as changed:
        inv.execute_tool("one", "list_tree", {"prefix": "src/"})
    assert changed.value.code == "TOOL_REPLAY"


def reincarnate(inv):
    return Investigation(
        inv.sources,
        inv.runtime,
        inv.gateway,
        inv.actor,
        inv.run_id,
        inv.epoch,
        inv.worker,
        inv.source_id,
        inv.role,
        inv.session_key,
        contract=inv.contract,
    )


def test_crash_after_read_reserves_quota_and_recovers_same_key_without_reexecuting(
    env, monkeypatch
):
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 1, "max_reads": 1})

    def crash(*args, **kwargs):
        raise RuntimeError("crash before completion stage")

    monkeypatch.setattr(inv, "_commit_tool", crash)
    with pytest.raises(RuntimeError, match="crash"):
        inv.execute_tool("read", "read_span", span())

    def do_not_repeat(*args, **kwargs):
        raise AssertionError("an already committed read must be inspected, not repeated")

    monkeypatch.setattr(env["app"].sources, "read_span", do_not_repeat)
    resumed = reincarnate(inv)
    with pytest.raises(DomainError) as blocked:
        resumed.execute_tool("new-key", "read_span", span(start=2))
    assert blocked.value.code == "TOOL_BUDGET"
    result = resumed.execute_tool("read", "read_span", span())
    assert result["content"] == FILES["src/target.py"]
    assert resumed.count == resumed.read_attempts == 1
    assert resumed.execute_tool("read", "read_span", span()) == result
    assert resumed.count == resumed.read_attempts == 1
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1
        assert resumed._durable_usage(db) == (1, 1)


def test_crash_before_read_checks_absence_before_retrying_reserved_same_key(env, monkeypatch):
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 1, "max_reads": 1})

    def crash(*args, **kwargs):
        raise RuntimeError("crash before source operation")

    monkeypatch.setattr(inv, "_invoke_bounded", crash)
    with pytest.raises(RuntimeError, match="crash"):
        inv.execute_tool("read", "read_span", span())
    observed = []
    recover = inv.sources.recover_read_span
    read = inv.sources.read_span

    def inspect(*args, **kwargs):
        observed.append("inspect")
        assert recover(*args, **kwargs) is None
        return None

    def execute(*args, **kwargs):
        observed.append("read")
        return read(*args, **kwargs)

    monkeypatch.setattr(inv.sources, "recover_read_span", inspect)
    monkeypatch.setattr(inv.sources, "read_span", execute)
    resumed = reincarnate(inv)
    resumed.execute_tool("read", "read_span", span())
    assert observed == ["inspect", "read"]
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 1
        assert resumed._durable_usage(db) == (1, 1)


def test_fatal_attempt_stays_consumed_and_does_not_retry_known_failure(env, monkeypatch):
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 1, "max_reads": 1})
    calls = []

    def failed(*args, **kwargs):
        calls.append(True)
        raise DomainError("SOURCE_HASH", "simulated fatal integrity failure")

    monkeypatch.setattr(inv.sources, "read_span", failed)
    with pytest.raises(DomainError) as original:
        inv.execute_tool("read", "read_span", span())
    assert original.value.code == "SOURCE_HASH"
    resumed = reincarnate(inv)
    with pytest.raises(DomainError) as same:
        resumed.execute_tool("read", "read_span", span())
    assert same.value.code == "SOURCE_HASH" and len(calls) == 1
    with pytest.raises(DomainError) as new:
        resumed.execute_tool("new", "read_span", span(start=2))
    assert new.value.code == "TOOL_BUDGET" and len(calls) == 1
    with env["app"].db.read() as db:
        assert resumed._durable_usage(db) == (1, 1)
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 0


def test_persisted_finish_blocks_new_keys_on_fresh_investigation(env):
    inv = investigation_for(env)
    args = {"reason": "evidence_sufficient"}
    inv.execute_tool("finish", "finish_investigation", args)
    resumed = reincarnate(inv)
    assert resumed.execute_tool("finish", "finish_investigation", args) == {
        "stop_reason": "evidence_sufficient"
    }
    with pytest.raises(DomainError) as blocked:
        reincarnate(inv).execute_tool("later", "read_span", span())
    assert blocked.value.code == "INVESTIGATION_FINISHED"


def test_uncertain_reserved_input_cannot_change_or_gain_new_quota(env, monkeypatch):
    inv = investigation_for(env, contract={**CONTRACT, "max_tools": 1, "max_reads": 1})

    def crash(*args, **kwargs):
        raise RuntimeError("crash after reservation")

    monkeypatch.setattr(inv, "_invoke_bounded", crash)
    with pytest.raises(RuntimeError):
        inv.execute_tool("read", "read_span", span())
    with pytest.raises(DomainError) as changed:
        reincarnate(inv).execute_tool("read", "read_span", span(start=2))
    assert changed.value.code == "TOOL_REPLAY"
    with env["app"].db.read() as db:
        assert inv._durable_usage(db) == (1, 1)
        assert db.scalar(select(func.count()).select_from(SourceRead)) == 0


@pytest.mark.parametrize(
    "code",
    [
        "NOT_FOUND",
        "SOURCE_HASH",
        "EVIDENCE_HASH",
        "LEASE_LOST",
        "FUNDING_UNAVAILABLE",
        "EVIDENCE_UNAVAILABLE",
        "SOURCE_REPLAY_CHANGED",
        "UNSAFE_PATH",
    ],
)
async def test_authority_integrity_and_lease_errors_never_enter_retry_loop(env, monkeypatch, code):
    inv = investigation_for(env)
    env["model"].overrides["analysis"] = lambda request: output(("read_span", span()))

    def denied(*args, **kwargs):
        raise DomainError(code, "deliberate security boundary")

    monkeypatch.setattr(env["app"].sources, "read_span", denied)
    with pytest.raises(DomainError) as error:
        await inv.run("missing query", {})
    assert error.value.code == code and len(env["model"].calls) == 1
    assert not any("error_code" in row["value"] for row in inv.trace)
    assert env["app"].runtime.stage(inv.run_id, f"investigation:{inv.session_key}:tool.0.0") is None


def test_read_then_revoke_cannot_persist_body_in_new_tool_stage(env, monkeypatch):
    inv = investigation_for(env)
    original = env["app"].sources.read_span

    def read_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        with env["app"].db.transaction() as db:
            invalidate(db, env["actor"], inv.source_id, "revoked", "source-test")
        return result

    monkeypatch.setattr(env["app"].sources, "read_span", read_then_revoke)
    with pytest.raises(DomainError):
        inv.execute_tool("span", "read_span", span())
    assert env["app"].runtime.stage(inv.run_id, f"investigation:{inv.session_key}:span") is None


def test_read_then_cancel_cannot_persist_body_in_new_tool_stage(env, monkeypatch):
    inv = investigation_for(env)
    original = env["app"].sources.read_span

    def read_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        with env["app"].db.read() as db:
            revision = db.get(AgentRun, inv.run_id).revision
        env["app"].runtime.cancel(env["actor"], inv.run_id, revision)
        return result

    monkeypatch.setattr(env["app"].sources, "read_span", read_then_cancel)
    with pytest.raises(DomainError) as error:
        inv.execute_tool("span", "read_span", span())
    assert error.value.code == "LEASE_LOST"
    assert env["app"].runtime.stage(inv.run_id, f"investigation:{inv.session_key}:span") is None


@pytest.mark.parametrize("target", ["source", "read", "stage"])
def test_cached_page_revalidates_source_receipt_and_observation(env, target):
    inv = investigation_for(env)
    result = inv.execute_tool("span", "read_span", span())
    with env["app"].db.transaction() as db:
        if target == "source":
            source = db.get(SourceSet, inv.source_id)
            source.files = {**source.files, "src/target.py": "# tampered\n"}
        elif target == "read":
            db.get(SourceRead, result["read_id"]).text = "# tampered\n"
        else:
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == inv.run_id,
                    StageArtifact.stage_key == f"investigation:{inv.session_key}:span",
                )
            )
            stage.payload = {
                **stage.payload,
                "value": {**stage.payload["value"], "content": "# tampered\n"},
            }
    with pytest.raises(DomainError) as error:
        inv.execute_tool("span", "read_span", span())
    assert error.value.code in {"SOURCE_HASH", "EVIDENCE_HASH"}


@pytest.mark.parametrize(
    "change",
    [
        {"version": "unknown"},
        {"max_rounds": 7},
        {"max_tools": 37},
        {"max_reads": 25},
        {"max_batch": 4},
        {"bootstrap_reads": 2},
        {"max_rounds": True},
        {"unexpected": 1},
    ],
)
def test_contract_configuration_is_explicit_and_bounded(change):
    with pytest.raises(DomainError) as error:
        investigation_contract({**CONTRACT, **change})
    assert error.value.code == "INVESTIGATION_CONTRACT"
