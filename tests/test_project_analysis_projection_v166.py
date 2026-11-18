"""Mechanical C05 contract tests; synthetic fixtures are not semantic evaluation."""

from copy import deepcopy

import httpx
import pytest
import typer
from conftest import publish_fixture
from sqlalchemy import func, select
from typer.testing import CliRunner

from careerforge.api import create_app
from careerforge.cli.integration_v166 import register_cli
from careerforge.contracts_project_analysis_v166 import (
    PROJECT_ANALYSIS_FIELDS,
    PROJECT_ANALYSIS_VERSION,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    CodeSymbol,
    ModelReceipt,
    ObjectRow,
    SourceRead,
    SourceSet,
    StageArtifact,
)
from careerforge.services.project_analysis import project_section
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint


def fact(**changes):
    return {
        "source_id": "source-1",
        "unit_version_id": "version-1",
        "claim_id": "claim-1",
        "subject_key": "subject",
        "claim_key": "claim",
        "publication_id": "pub-1",
        "statement": "Only when x is absent, wrapper returns await f(x); failures propagate.",
        "conditions": ["Fixed snapshot only", "No runtime execution observed"],
        **changes,
    }


def records(status="known", *, anchor="claim", unit="version-1", source="source-1"):
    assessment = {
        "status": status,
        "explanation": "Synthetic contract fixture, not an evaluated fact.",
        "anchors": [{"subject_key": "subject", "claim_keys": [anchor]}],
        "unresolved_questions": [],
    }
    return [
        {
            "source_id": source,
            "unit_version_ids": [unit],
            "assessment_receipt": {"receipt_id": "independent"},
            "coverage": {
                "dimensions": [{**assessment, "dimension": "call_flow"}],
                "requirements": [],
            },
        }
    ]


@pytest.mark.parametrize(
    "status,expected",
    [
        ("known", "known"),
        ("partial", "partial"),
        ("unknown", "unknown"),
        ("not_applicable", "unknown"),
        ("conflicting", "unknown"),
    ],
)
def test_known_partial_unknown_preserve_exact_scope_without_inventing_edges(status, expected):
    original = fact()
    value = project_section("call_flow", records(status), [original], source_ids=["source-1"])
    assert value["status"] == expected
    assert not value["typed_edges_available"] and not value["full_repository_coverage"]
    if expected != "unknown":
        assert value["items"] == [original]
        assert "from" not in value["items"][0] and "to" not in value["items"][0]
    else:
        assert not value["items"]


@pytest.mark.parametrize(
    "review,facts",
    [
        (
            [],
            [fact()],
        ),  # An analysis self-assessment or a published fact is not a dimension review.
        (records(anchor="unpublished"), [fact()]),
        (records(), []),  # Rejected or accepted-but-unpublished claims cannot supply structure.
        (records(unit="old-version"), [fact()]),
        (records(source="other-source"), [fact()]),
        (records(), [fact(), fact(claim_id="conflict", publication_id="pub-2")]),
    ],
)
def test_absent_unpublished_mixed_version_and_ambiguous_anchors_are_unknown(review, facts):
    value = project_section("call_flow", review, facts, source_ids=["source-1"])
    assert value["status"] == "unknown" and not value["items"]
    assert value["reason_codes"]


def test_a_known_source_does_not_fill_another_source_or_dimension():
    value = project_section("call_flow", records(), [fact()], source_ids=["source-1", "source-2"])
    assert value["status"] == "partial" and value["items"] == [fact()]
    absent = project_section("boundaries", records(), [fact()], source_ids=["source-1"])
    assert absent["status"] == "unknown"


def _project(env, run_id, payload=None):
    app = env["app"]
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        result = app.runtime.stage(run_id, "result") if payload is None else payload
        return app.build.project_analysis.project_in(db, env["actor"], run, result)


def _counts(env):
    with env["app"].db.read() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (ModelReceipt, SourceRead, StageArtifact, CodeSymbol)
        }


def install_dimension_fixture(env, monkeypatch, *, status="partial", anchored=True):
    original = env["model"].complete

    async def complete(request):
        result = await original(request)
        context = request["context"]
        if context.get("assessment_kind") != "independent_task_sufficiency":
            return result
        anchors = context["coverage_anchors"]
        source_id = anchors[0]["source_id"]
        refs = [
            {
                "subject_key": anchors[0]["subject_key"],
                "claim_keys": [anchors[0]["claim_key"] if anchored else "unpublished"],
            }
        ]
        assessment = {
            "status": status,
            "explanation": "Synthetic contract fixture only.",
            "anchors": refs,
            "unresolved_questions": [],
        }
        coverage = {
            "question": assessment,
            "dimensions": [
                {**assessment, "dimension": name}
                for name in (
                    "project_structure",
                    "module_responsibilities",
                    "call_flow",
                    "boundaries",
                )
            ],
            "requirements": [],
            "unresolved_questions": ["Fixture runtime was not observed."],
        }
        return result.model_copy(
            update={
                "structured": {"source_coverages": [{"source_id": source_id, "coverage": coverage}]}
            }
        )

    monkeypatch.setattr(env["model"], "complete", complete)


@pytest.mark.parametrize("status", ["known", "partial", "unknown"])
async def test_new_admission_c05_fields_no_new_reads_calls_or_text_changes(
    env, monkeypatch, status
):
    install_dimension_fixture(env, monkeypatch, status=status)
    source, pub, run_id = await publish_fixture(env)
    result = env["app"].runtime.stage(run_id, "result")
    assert all(key in result for key in PROJECT_ANALYSIS_FIELDS)
    with env["app"].db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.input_manifest["project_analysis_contract_version"] == PROJECT_ANALYSIS_VERSION
        original = env["app"].knowledge.publication_pack(db, env["actor"], pub)
        counts = _counts(env)
        model_calls = len(env["model"].calls)
    first, second = _project(env, run_id), _project(env, run_id)
    assert first == second == {key: result[key] for key in PROJECT_ANALYSIS_FIELDS}
    assert _counts(env) == counts and len(env["model"].calls) == model_calls
    assert result["modules"]["status"] == status
    assert result["knowledge_units"][0]["facts"] == original["facts"]
    assert result["repo_map"]["sources"][0]["source_id"] == source
    assert result["repo_map"]["sources"][0]["full_repository_read"] is False
    assert result["key_symbols"]["items"]
    assert (
        result["projection"]["other_goals_status"] == "not_evaluated_requires_separate_goal_build"
    )
    if status != "unknown":
        item = result["modules"]["items"][0]
        assert item["statement"] == original["facts"][0]["text"]
        assert item["conditions"] == original["facts"][0]["conditions"]
        assert item["citations"][0]["read_id"] != item["independent_citations"][0]["read_id"]
        assert item["commit"] == item["independent_citations"][0]["commit"]


async def test_analysis_or_fact_acceptance_alone_cannot_claim_structure(env):
    _, _, run = await publish_fixture(env)
    value = _project(env, run)
    assert value["knowledge_units"]
    assert all(
        value[key]["status"] == "unknown"
        for key in ("modules", "call_chain_edges", "exception_boundaries")
    )


async def test_independent_unpublished_anchor_stays_unknown_despite_published_fact(
    env, monkeypatch
):
    install_dimension_fixture(env, monkeypatch, status="known", anchored=False)
    _, _, run = await publish_fixture(env)
    value = _project(env, run)
    assert value["knowledge_units"] and value["modules"]["status"] == "unknown"
    assert "claim_not_published_or_ambiguous" in value["modules"]["reason_codes"]


async def test_cached_static_metadata_is_not_semantic_or_read_authority(env, monkeypatch):
    source, _, run = await publish_fixture(env)
    original = _project(env, run)
    counts = _counts(env)
    with env["app"].db.transaction() as db:
        row = db.get(SourceSet, source)
        row.metadata_json = {**row.metadata_json, "repo_map": {"files": [{"path": "invented.py"}]}}
        for symbol in db.scalars(select(CodeSymbol).where(CodeSymbol.source_id == source)):
            symbol.name = "InventedModuleRole"
    assert _project(env, run) == original
    assert _counts(env) == counts
    pack_reader = env["app"].knowledge.publication_pack

    def different_scan_order(*args, **kwargs):
        pack = pack_reader(*args, **kwargs)
        return {
            **pack,
            **{
                key: list(reversed(pack[key]))
                for key in ("facts", "citations", "topic_links", "facets")
            },
        }

    monkeypatch.setattr(env["app"].knowledge, "publication_pack", different_scan_order)
    assert _project(env, run) == original


@pytest.mark.parametrize(
    "tamper", ["source", "revoked", "read_role", "coverage_stage", "coverage_receipt"]
)
async def test_stale_revoked_or_unbound_evidence_fails_closed(env, monkeypatch, tamper):
    install_dimension_fixture(env, monkeypatch)
    source, pub, run = await publish_fixture(env)
    with env["app"].db.transaction() as db:
        if tamper == "source":
            row = db.get(SourceSet, source)
            row.files = {**row.files, "src/tools.py": "different snapshot\n"}
        elif tamper == "revoked":
            db.get(ObjectRow, pub).state = "revoked"
        elif tamper == "read_role":
            row = db.scalar(
                select(SourceRead).where(
                    SourceRead.source_id == source, SourceRead.role == "reviewer"
                )
            )
            row.role = "analysis"
        elif tamper == "coverage_stage":
            row = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run, StageArtifact.stage_key == "task_sufficiency"
                )
            )
            changed = deepcopy(row.payload)
            changed["task_coverages"][0]["coverage"]["dimensions"][0]["status"] = "known"
            row.payload = changed
        else:
            row = db.scalar(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run, ModelReceipt.stage_key == "task.sufficiency"
                )
            )
            row.role = "analysis"
    with pytest.raises(DomainError):
        _project(env, run)


async def test_other_commit_publication_cannot_be_grafted_into_build(env):
    _, _, first = await publish_fixture(env)
    _, other, _ = await publish_fixture(env, key="other", commit="b" * 40)
    result = deepcopy(env["app"].runtime.stage(first, "result"))
    result["publication_ids"] = [other]
    with pytest.raises(DomainError, match="固定任务"):
        _project(env, first, result)


async def test_historical_queued_contract_stays_old_and_explicit_projection_is_read_only(
    env, monkeypatch
):
    prepare = env["app"].build.prepare_in

    def old_admission(db, actor, body):
        manifest = prepare(db, actor, body)
        manifest.pop("project_analysis_contract_version")
        return manifest

    monkeypatch.setattr(env["app"].build, "prepare_in", old_admission)
    _, _, old_run = await publish_fixture(env, key="old")
    before = deepcopy(env["app"].runtime.stage(old_run, "result"))
    assert "repo_map" not in before and "modules" not in before
    old_calls = len(env["model"].calls)
    counts = _counts(env)
    projection = _project(env, old_run)
    assert projection["projection"]["admission_contract"] is None
    assert env["app"].runtime.stage(old_run, "result") == before
    assert _counts(env) == counts and len(env["model"].calls) == old_calls
    monkeypatch.setattr(env["app"].build, "prepare_in", prepare)
    _, _, new_run = await publish_fixture(env, key="new")
    assert "repo_map" in env["app"].runtime.stage(new_run, "result")
    assert len(env["model"].calls) - old_calls == old_calls


async def test_pre_research_queued_build_is_not_upgraded_and_missing_review_dimension_is_unknown(
    env, monkeypatch
):
    prepare = env["app"].build.prepare_in

    def old_admission(db, actor, body):
        manifest = prepare(db, actor, body)
        for key in (
            "project_analysis_contract_version",
            "research_contract_version",
            "investigation_contracts",
            "sufficiency_call_reserve",
            "execution_contract_version",
            "execution_plan",
            "quote_correction_contract",
            "duration_minutes",
            "planning_call_seconds",
        ):
            manifest.pop(key)
        return manifest

    monkeypatch.setattr(env["app"].build, "prepare_in", old_admission)
    _, _, run = await publish_fixture(env, key="pre-research-contract")
    before = deepcopy(env["app"].runtime.stage(run, "result"))
    assert "task_coverage" not in before and "modules" not in before
    assert env["app"].runtime.stage(run, "task_sufficiency") is None
    counts = _counts(env)
    value = _project(env, run)
    assert value["modules"]["status"] == "unknown"
    assert value["projection"]["reason_codes"] == ["independent_coverage_not_committed"]
    assert value["knowledge_units"]
    assert env["app"].runtime.stage(run, "result") == before
    assert _counts(env) == counts


async def test_authenticated_route_owner_only_and_saved_projection_checked(env):
    _, _, run = await publish_fixture(env)
    app = env["app"]
    with app.db.transaction() as db:
        other, _, token = create_principal(db, "not-the-build-owner")
        set_grant(db, env["actor"], env["ns"], other.id, "reader", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test"
    ) as client:
        path = f"/api/v1/knowledge/builds/{run}/project-analysis"
        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(path, headers={"Authorization": "Bearer " + token})
        ).status_code == 404
        headers = {"Authorization": "Bearer " + env["owner"]["token"]}
        response = await client.get(path, headers=headers)
        assert response.status_code == 200 and response.json() == _project(env, run)
        with app.db.transaction() as db:
            row = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run, StageArtifact.stage_key == "result"
                )
            )
            value = deepcopy(row.payload)
            value["modules"]["status"] = "known"
            row.payload = value
        assert (await client.get(path, headers=headers)).status_code == 409
        assert (await client.get(f"/api/v1/runs/{run}", headers=headers)).status_code == 409


def test_cli_only_reads_projection_and_rejects_url_injection():
    calls = []
    app = typer.Typer()
    register_cli(
        app,
        lambda *args: calls.append(args) or {},
        lambda value: None,
        lambda: None,
        lambda value: value,
    )
    result = CliRunner().invoke(app, ["project-analysis", "run-123"])
    assert result.exit_code == 0
    assert calls == [("GET", "/api/v1/knowledge/builds/run-123/project-analysis")]
    result = CliRunner().invoke(app, ["project-analysis", "../other?leak=true"])
    assert result.exit_code != 0 and len(calls) == 1


def test_projection_hash_covers_exact_envelope():
    # The real integration tests compare repeated generated envelopes. This
    # small invariant documents omission of only the hash's own field.
    value = {"projection": {}, "modules": {"status": "unknown"}}
    expected = fingerprint(value)
    value["projection"]["content_hash"] = expected
    copy = deepcopy(value)
    del copy["projection"]["content_hash"]
    assert fingerprint(copy) == expected
