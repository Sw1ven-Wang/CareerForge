"""Synthetic control-flow checks only. No real labels, scores or deployment claims."""

from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from conftest import FILES
from pydantic import ValidationError
from sqlalchemy import select
from test_bounded_fact_evidence_v166 import choose, pack
from test_typed_execution_replay_v16 import execute, typed_candidate

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, ModelOutput, ToolCall
from careerforge.contracts_learning_ext import ReplayInput, ReplayThresholds
from careerforge.contracts_retrieval_replay import (
    RetrievalFactLabel,
    RetrievalLabelProvenance,
    RetrievalR3ReplayCase,
    RetrievalR3ReplayDatasetInput,
    RetrievalRelevanceGold,
)
from careerforge.errors import DomainError
from careerforge.models import (
    EvolutionCandidate,
    ObjectRow,
)
from careerforge.services.bounded_evidence import fact_support
from careerforge.services.evolution_retrieval_r3 import (
    R3_METRICS,
    R3_SCOPE,
    r3_execution_contract,
    r3_label_record_hash,
    score_r3_case,
    validate_r3_labels,
)
from careerforge.services.evolution_typed import typed_execution_contract
from careerforge.services.security import create_principal
from careerforge.util import fingerprint


def label_for(full, support, fact, relevant):
    return RetrievalFactLabel(
        **{k: full[k] for k in ("publication_id", "unit_version_id", "source_id", "commit")},
        fact_atom_id=fact["fact_atom_id"],
        relevant=relevant,
        **support[0][fact["fact_atom_id"]],
    )


def pure_case():
    full, support = pack()
    labels = [
        label_for(full, support, f, f["fact_atom_id"] == "p-a").model_dump() for f in full["facts"]
    ]
    case = {
        "publication_ids": ["p"],
        "frozen_units": [full],
        "r3_support": {"p": {"bindings": support[0], "citations": support[1]}},
        "expected": {"labels": labels, "complete_candidate_pool": True},
    }
    units, _ = choose({"p": full}, {"p": support})
    return case, {
        "knowledge_units": units,
        "selection_manifest_id": "selection-0000000000000000000000000000",
    }


def test_explicit_versioned_contract_does_not_change_missing_historical_default():
    assert typed_execution_contract()["retrieval_context_contract"] is None
    assert typed_execution_contract(r3_execution_contract()) == r3_execution_contract()
    for key in r3_execution_contract():
        bad = r3_execution_contract()
        bad.pop(key)
        with pytest.raises(DomainError):
            typed_execution_contract(bad)


def test_r3_scores_exact_fact_support_version_relevance_not_whole_publication_membership():
    case, result = pure_case()
    validate_r3_labels(case)
    metrics = score_r3_case(case, result, None)
    assert metrics["score"] == 1.0 and metrics["hard_gate_violations"] == 0
    assert all(metrics[name] == 1.0 for name in R3_METRICS)
    # Returning the same publication with its other complete, irrelevant fact
    # does not receive a membership reward.
    full = case["frozen_units"][0]
    support = case["r3_support"]["p"]
    units, _ = choose(
        {"p": full}, {"p": (support["bindings"], support["citations"])}, question="Beta"
    )
    wrong = score_r3_case(case, {**result, "knowledge_units": units}, None)
    assert wrong["fact_recall"] == wrong["fact_precision"] == wrong["evidence_recall"] == 0
    assert wrong["score"] == 0
    assert all(wrong[name] == 0 for name in R3_METRICS)
    assert wrong["hard_gate_violations"] == 0


def test_r3_empty_selection_gets_no_precision_or_coverage_reward():
    case, result = pure_case()
    result["knowledge_units"] = []
    metrics = score_r3_case(case, result, None)
    assert metrics["score"] == 0
    assert all(metrics[name] == 0 for name in R3_METRICS)
    assert metrics["hard_gate_violations"] == 0


@pytest.mark.parametrize(
    "damage",
    ["text", "conditions", "counterevidence", "version", "source", "commit", "duplicate", "budget"],
)
def test_r3_projection_corruption_is_a_hard_failure(damage):
    case, result = pure_case()
    unit = result["knowledge_units"][0]
    if damage == "text":
        unit["facts"][0]["text"] = "Invented answer"
    elif damage == "conditions":
        unit["facts"][0]["conditions"] = []
    elif damage == "counterevidence":
        unit["citations"].pop()
    elif damage == "version":
        unit["unit_version_id"] = "another-version"
    elif damage == "source":
        unit["source_id"] = "another-source"
    elif damage == "commit":
        unit["commit"] = "b" * 40
    elif damage == "duplicate":
        result["knowledge_units"].append(deepcopy(unit))
    else:
        unit["limitations"] = ["x" * 13000]
    assert score_r3_case(case, result, None)["hard_gate_violations"] == 1


@pytest.mark.parametrize(
    "damage",
    ["missing", "duplicate", "support", "counterevidence", "version", "unknown", "all_negative"],
)
def test_complete_independent_labels_bind_every_fact_and_evidence_version(damage):
    case, _ = pure_case()
    labels = case["expected"]["labels"]
    if damage == "missing":
        labels.pop()
    elif damage == "duplicate":
        labels.append(deepcopy(labels[0]))
    elif damage == "support":
        labels[0]["support_refs"] = ["other"]
    elif damage == "counterevidence":
        labels[0]["counterevidence_refs"] = []
    elif damage == "version":
        labels[0]["unit_version_id"] = "old"
    elif damage == "unknown":
        labels[0]["fact_atom_id"] = "unknown"
    else:
        for label in labels:
            label["relevant"] = False
    with pytest.raises(DomainError):
        validate_r3_labels(case)


async def setup_r3(env, *, paid=False):
    app, actor = env["app"], env["actor"]
    candidate, _ = await typed_candidate(
        env,
        "retrieval_recipe",
        {"per_project_limit": 1, "vector_enabled": False, "rerank_enabled": False},
    )

    def analysis(request):
        if not request.get("schema"):
            return ModelOutput(
                synthetic=True,
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={"reason": "evidence_sufficient"},
                    )
                ],
                finish_reason="tool_calls",
            )
        read = next(
            r for r in request["context"]["reading"]["reads"] if r["path"].endswith("tools.py")
        )
        return ModelOutput(
            synthetic=True,
            structured={
                "units": [
                    {
                        "subject_key": f"r3-{i}",
                        "unit_type": "function",
                        "title": "Synthetic r3 fact labels",
                        "claims": [
                            {
                                "key": "f",
                                "statement": text,
                                "conditions": ["Synthetic process fixture only."],
                                "support": [{"read_id": read["read_id"], "quote": "return value"}],
                            }
                        ],
                        "topics": [{"topic_key": "agent.tool_calling", "relation": "illustrates"}],
                        "limitations": ["Synthetic only."],
                    }
                    for i, text in enumerate(
                        [
                            "Alpha exact relevant path returns value.",
                            "Alpha background returns value.",
                            "Alpha unrelated returns value.",
                        ]
                    )
                ],
                "unanswered_questions": [],
            },
        )

    env["model"].overrides["analysis"] = analysis
    cases = []
    for group in ("a", "b"):
        source = app.sources.import_source(
            actor,
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key=f"fixtures/r3-{group}",
                commit_sha="a" * 40,
                license_id="MIT",
                files=FILES,
                source_metadata={"synthetic": True},
            ),
        )
        run = app.build.start(
            actor,
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=env["funding"],
                question="Alpha exact relevant path",
                source_id=source,
                topic_keys=["agent.tool_calling"],
                call_cap=16,
                duration_minutes=60,
                max_units_per_project=3,
                idempotency_key=f"r3-build-{group}",
            ),
        )
        await app.drain()
        result = app.runtime.stage(run, "result")
        assert result and len(result["publication_ids"]) == 3, result
        ids = result["publication_ids"]
        with app.db.read() as db:
            labels = []
            for pid in ids:
                full = app.knowledge.publication_pack(db, actor, pid)
                support = fact_support(app.knowledge, db, actor, full)
                labels.extend(
                    label_for(full, support, f, "exact relevant" in f["text"])
                    for f in full["facts"]
                )
        cases.append(
            RetrievalR3ReplayCase(
                case_id=group,
                group_id=group,
                user_question_group=f"r3-heldout-{group}",
                time_partition="2020-01",
                question=f"Alpha exact relevant path {group}",
                publication_ids=ids,
                expected=RetrievalRelevanceGold(labels=labels, complete_candidate_pool=True),
            )
        )
    if paid:
        env["model"].supports_embeddings = True
        env["model"].supports_reranking = True
        env["model"].overrides["embedding"] = lambda request: ModelOutput(
            synthetic=True,
            structured={
                "model": "synthetic-vector",
                "vectors": [[1.0] + [0.0] * 7 for _ in request["context"]["texts"]],
            },
        )
        env["model"].overrides["reranker"] = lambda request: ModelOutput(
            synthetic=True,
            structured={
                "scores": [
                    {"index": i, "score": float(i)}
                    for i in range(len(request["context"]["documents"]))
                ]
            },
        )
        index = app.runtime.create(
            actor, env["ns"], env["funding"], "retrieval_index", {}, 1, "r3-fixture-index"
        )
        epoch = app.runtime.claim(index, "r3-indexer")
        await app.retrieval.index_vectors(
            actor,
            [pid for case in cases for pid in case.publication_ids],
            (index, epoch, "r3-indexer"),
            batch_size=16,
        )
        app.runtime.finish(index, epoch, "r3-indexer", "succeeded")
        for case in cases:
            case.task_type = "explain_project_architecture"
        with app.db.transaction() as db:
            row = db.get(EvolutionCandidate, candidate)
            row.content = {**row.content, "vector_enabled": True, "rerank_enabled": True}
            row.content_hash = fingerprint(row.content)
    value = RetrievalR3ReplayDatasetInput(
        execution_contract=r3_execution_contract()["version"],
        namespace_id=env["ns"],
        name="r3-frozen",
        version="1",
        split="holdout",
        provenance="fixture",
        label_provenance=RetrievalLabelProvenance(
            kind="synthetic_fixture",
            protocol="complete-fact-evidence-version-relevance-1",
            record_reference="synthetic-control-flow-fixture-only",
            record_sha256=r3_label_record_hash([case.model_dump(mode="json") for case in cases]),
        ),
        cases=cases,
        thresholds=ReplayThresholds(min_cases=2, min_groups=2, max_latency_ratio=5),
    )
    dataset = app.evolution.register_r3_retrieval_dataset(actor, value)
    return candidate, dataset, value


@pytest.mark.asyncio
async def test_r3_actual_projection_pair_and_shared_promotion_canary_gate(env):
    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await setup_r3(env)
    started, report = await execute(env, candidate, dataset, cap=1)
    assert report["decision"] == "passed", report
    assert report["retrieval_evaluation_scope"] == R3_SCOPE
    assert report["current_retrieval_quality_validated"] is False
    assert report["fixture_process_only"] is True
    assert report["per_arm_call_cap"] == 1
    assert report["model_calls"] == 0  # deterministic actual selector, not fabricated calls
    detail = app.evolution.replay_detail(actor, started["replay_id"])
    for trace in detail["trace"]:
        assert trace["r3_binding"]["run_id"] == started["run_id"]
        assert trace["metrics"]["fact_recall"] == 1
        assert trace["metrics"]["fact_precision"] == (1 if trace["arm"] == "candidate" else 1 / 3)
        with app.db.read() as db:
            row = db.get(ObjectRow, trace["typed_result"]["selection_manifest_id"])
            assert row.payload["evidence_budget"]["limit"] == 12000
    # No evaluator labels, label provenance, or hashes enter any provider request.
    for call in env["model"].calls:
        assert "r3_labels_hash" not in str(call["context"])
        assert "complete_candidate_pool" not in str(call["context"])
    # The frozen r3 executor still runs and scores identically, but new r4
    # admissions cannot be certified by it. Even a passing synthetic fixture
    # must not bypass this version boundary to start a canary or promotion.
    assert report["admitted_retrieval_context_contract"] == "question-focused-retrieval-1.6.6-r4"
    assert (
        "current_retrieval_requires_matching_execution_and_scoring"
        in report["external_acceptance_blockers"]
    )
    with app.db.read() as db:
        row = db.get(EvolutionCandidate, candidate)
        with pytest.raises(DomainError) as failure:
            app.evolution.checked_evaluation(db, actor, row, report["evaluation_id"])
        assert failure.value.code == "REPLAY_DEPLOYMENT_CONTRACT"
    for release in (app.rollouts.start, app.evolution.promote):
        with pytest.raises(DomainError) as failure:
            release(actor, candidate, report["evaluation_id"], 1)
        assert failure.value.code == "REPLAY_DEPLOYMENT_CONTRACT"


@pytest.mark.asyncio
async def test_r3_zero_gain_degradation_and_insufficient_data_cannot_promote(env):
    app, actor = env["app"], env["actor"]
    candidate, dataset, value = await setup_r3(env)
    with app.db.transaction() as db:
        row = db.get(EvolutionCandidate, candidate)
        row.content = {**row.content, "per_project_limit": 3}
        row.content_hash = fingerprint(row.content)
    _, report = await execute(env, candidate, dataset, cap=1)
    assert (
        report["decision"] == "inconclusive" and report["reason"] == "improvement_not_established"
    )
    with pytest.raises(DomainError):
        app.evolution.promote(actor, candidate, report["evaluation_id"], 1)

    # A narrower result cannot win by omitting independently required positives.
    # This is a separate explicitly synthetic dataset, never a relabeled run.
    with app.db.transaction() as db:
        row = db.get(EvolutionCandidate, candidate)
        row.content = {**row.content, "per_project_limit": 1}
        row.content_hash = fingerprint(row.content)
    all_positive = value.model_copy(deep=True, update={"name": "all-required"})
    for case in all_positive.cases:
        for label in case.expected.labels:
            label.relevant = True
    all_positive.label_provenance.record_sha256 = r3_label_record_hash(
        [case.model_dump(mode="json") for case in all_positive.cases]
    )
    degraded_ds = app.evolution.register_r3_retrieval_dataset(actor, all_positive)
    _, degraded = await execute(env, candidate, degraded_ds, key="drops-required-facts", cap=1)
    assert degraded["decision"] == "failed", degraded
    assert degraded["reason"] == "critical_metric_regression"
    assert "fact_recall" in degraded["critical_metric_regressions"]
    assert "evidence_recall" in degraded["critical_metric_regressions"]
    few = value.model_copy(deep=True, update={"name": "insufficient-independent-samples"})
    few.thresholds.min_cases = 3
    few_ds = app.evolution.register_r3_retrieval_dataset(actor, few)
    _, insufficient = await execute(env, candidate, few_ds, key="insufficient", cap=1)
    assert insufficient["decision"] == "inconclusive", insufficient
    assert insufficient["reason"] == "insufficient_independent_groups"
    for outcome in (degraded, insufficient):
        for release in (app.evolution.promote, app.rollouts.start):
            with pytest.raises(DomainError):
                release(actor, candidate, outcome["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_r3_registration_requires_complete_labels_and_real_independent_provenance(env):
    app, actor = env["app"], env["actor"]
    candidate, _, value = await setup_r3(env)
    incomplete = value.model_copy(deep=True, update={"name": "incomplete"})
    incomplete.cases[0].expected.labels.pop()
    with pytest.raises(DomainError) as failure:
        app.evolution.register_r3_retrieval_dataset(actor, incomplete)
    assert failure.value.code == "R3_GOLD_INCOMPLETE"
    mislabeled_record = value.model_copy(deep=True, update={"name": "unmatched-label-record"})
    mislabeled_record.label_provenance.record_sha256 = "0" * 64
    with pytest.raises(DomainError) as failure:
        app.evolution.register_r3_retrieval_dataset(actor, mislabeled_record)
    assert failure.value.code == "R3_LABEL_PROVENANCE"
    with app.db.transaction() as db:
        annotators = [
            create_principal(db, name)[0].id for name in ("independent-a", "independent-b")
        ]
    human = value.model_copy(
        deep=True,
        update={
            "name": "human",
            "split": "development",
            "provenance": "human_gold",
            "annotators": annotators,
        },
    )
    human.label_provenance.kind = "independent_human"
    with pytest.raises(DomainError) as failure:
        app.evolution.register_r3_retrieval_dataset(actor, human)
    assert failure.value.code == "SYNTHETIC_GOLD"
    human.annotators = [actor, annotators[0]]
    with pytest.raises(DomainError) as failure:
        app.evolution.register_r3_retrieval_dataset(actor, human)
    assert failure.value.code == "R3_LABEL_INDEPENDENCE"
    from careerforge.models import Experience

    with app.db.read() as db:
        row = db.get(EvolutionCandidate, candidate)
        training = db.get(Experience, row.experience_ids[0]).payload
    contaminated = value.model_copy(deep=True, update={"name": "training-overlap"})
    contaminated.cases[0].group_id = training["group_id"]
    contaminated.label_provenance.record_sha256 = r3_label_record_hash(
        [case.model_dump(mode="json") for case in contaminated.cases]
    )
    contaminated_ds = app.evolution.register_r3_retrieval_dataset(actor, contaminated)
    with pytest.raises(DomainError) as failure:
        app.evolution.start_replay(
            actor,
            candidate,
            ReplayInput(
                dataset_id=contaminated_ds["dataset_id"],
                funding_id=env["funding"],
                idempotency_key="leaked-r3",
            ),
        )
    assert failure.value.code == "HOLDOUT_LEAKAGE"
    for mode in ("development", "production"):
        app.db.settings = replace(app.db.settings, mode=mode)
        with pytest.raises(DomainError) as failure:
            app.evolution.register_r3_retrieval_dataset(
                actor, value.model_copy(update={"name": mode})
            )
        assert failure.value.code == "FIXTURE_NOT_GOLD"


@pytest.mark.asyncio
async def test_r3_explicit_api_schema_rejects_missing_marker_and_legacy_labels(env):
    candidate, _, value = await setup_r3(env)
    payload = value.model_dump(mode="json")
    payload.pop("execution_contract")
    with pytest.raises(ValidationError):
        RetrievalR3ReplayDatasetInput.model_validate(payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/evolution/r3-retrieval-datasets",
            json=payload,
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_r3_live_selection_binding_rejects_reuse_tamper_and_revocation(env, monkeypatch):
    # This test preserves the historical r3 canary's revocation/rollback path.
    # The separate r4 test above rejects using this evidence for new admissions.
    import careerforge.services.evolution_replay as replay_module
    from careerforge.models import AgentRun, ModelReceipt, PolicyHead, PolicyRollout
    from careerforge.models_learning_ext import PolicyReplayManifest
    from careerforge.services.evolution_retrieval_r3 import check_r3_trace
    from careerforge.services.policies import ensure_policies

    monkeypatch.setattr(
        replay_module, "RETRIEVAL_CONTEXT_CONTRACT", "question-focused-retrieval-1.6.6-r3"
    )
    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await setup_r3(env, paid=True)
    started, report = await execute(env, candidate, dataset, key="bindings", cap=2)
    assert report["decision"] == "passed", report
    with app.db.read() as db:
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        frozen, traces = deepcopy(manifest.frozen_input), deepcopy(manifest.trace)
        original = traces[0]
        case = frozen["cases"][0]
        check_r3_trace(app.evolution, db, actor, frozen, started["run_id"], 0, case, original)
        for field, value in (
            ("typed_result", traces[1]["typed_result"]),
            ("frozen_input_hash", "0" * 64),
            ("policy_hash", "0" * 64),
            ("case_id", "other-case"),
            ("r3_binding", {}),
            ("receipt_ids", ["invented"]),
            ("usage", [{"tokens": 0}] * len(original["receipt_ids"])),
            ("latency_ms", 0.0),
        ):
            trace = {**deepcopy(original), field: value}
            with pytest.raises(DomainError):
                check_r3_trace(app.evolution, db, actor, frozen, started["run_id"], 0, case, trace)
        with pytest.raises(DomainError):
            check_r3_trace(app.evolution, db, actor, frozen, "another-run", 0, case, original)
    from careerforge.models import StageArtifact

    with app.db.transaction() as db:
        saved_stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == started["run_id"],
                StageArtifact.stage_key == "replay:0:baseline:result",
            )
        )
        old_input_hash = saved_stage.input_hash
        saved_stage.input_hash = "0" * 64
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError) as failure:
            release(actor, candidate, report["evaluation_id"], 1)
        assert failure.value.code == "R3_REPLAY_BINDING"
    with app.db.transaction() as db:
        db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == started["run_id"],
                StageArtifact.stage_key == "replay:0:baseline:result",
            )
        ).input_hash = old_input_hash
    # Even self-consistent manifest hashes cannot substitute labels for the
    # separately frozen dataset. Privileged storage-corruption threat model.
    for field in ("cases", "threshold_policy"):
        changed = deepcopy(frozen)
        if field == "cases":
            changed[field][0]["expected"]["labels"][0]["relevant"] = False
        else:
            changed[field]["minimum_improvement"] = 0.75
        with app.db.read() as db, pytest.raises(DomainError) as failure:
            app.evolution._validate_replay(db, actor, changed)
        assert failure.value.code == "REPLAY_INPUT_CHANGED"
    # A privileged run+manifest rewrite cannot raise the originally admitted
    # budget, even if it recomputes all of those objects' self-hashes.
    with app.db.transaction() as db:
        row = db.get(AgentRun, started["run_id"])
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        changed = deepcopy(frozen)
        changed["per_arm_request_cap"] += 1
        row.input_manifest, row.input_hash = changed, fingerprint(changed)
        manifest.frozen_input, manifest.frozen_hash = changed, fingerprint(changed)
    with app.db.read() as db, pytest.raises(DomainError) as failure:
        app.evolution._validate_replay(db, actor, changed)
    assert failure.value.code == "R3_ADMISSION_CHANGED"
    with app.db.transaction() as db:
        row = db.get(AgentRun, started["run_id"])
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        row.input_manifest, row.input_hash = frozen, fingerprint(frozen)
        manifest.frozen_input, manifest.frozen_hash = frozen, fingerprint(frozen)
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
        head_before = db.scalar(
            select(PolicyHead).where(
                PolicyHead.namespace_id == env["ns"], PolicyHead.target == "retrieval_recipe"
            )
        )
        unchanged_head = (head_before.policy_id, head_before.revision)
    rollout = app.rollouts.start(
        actor, candidate, report["evaluation_id"], 1, minimum_per_arm=1, maximum_turns=2
    )
    assert rollout["champion_unchanged"] is True
    assert report["model_calls"] == 8 and report["call_ratio"] == 1
    assert report["synthetic_execution"] is True
    for call in env["model"].calls:
        assert "complete_candidate_pool" not in str(call["context"])
        assert frozen["threshold_policy"]["r3_label_provenance"]["record_sha256"] not in str(
            call["context"]
        )
    assert report["current_retrieval_quality_validated"] is False
    with app.db.transaction() as db:
        receipt = db.get(ModelReceipt, original["receipt_ids"][0])
        saved_response = deepcopy(receipt.response)
        receipt.response = {**receipt.response, "text": "tampered receipt"}
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError):
            release(actor, candidate, report["evaluation_id"], 1)
    with app.db.transaction() as db:
        db.get(ModelReceipt, original["receipt_ids"][0]).response = saved_response
    repeated = app.evolution.start_replay(
        actor,
        candidate,
        ReplayInput(
            dataset_id=dataset["dataset_id"],
            funding_id=env["funding"],
            per_arm_call_cap=2,
            idempotency_key="bindings",
        ),
    )
    assert repeated["run_id"] == started["run_id"] and repeated["replayed"] is True
    selection_id = original["typed_result"]["selection_manifest_id"]
    with app.db.transaction() as db:
        selection = db.get(ObjectRow, selection_id)
        selection.payload = {
            **selection.payload,
            "evidence_budget": {"unit": "utf8_bytes", "limit": 40000, "selected_bytes": 0},
        }
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError):
            release(actor, candidate, report["evaluation_id"], 1)
    with app.db.transaction() as db:
        assert (
            app.rollouts.assign_in(db, actor, env["ns"], "synthetic-next-turn", policies)
            == policies
        )
        assert db.get(PolicyRollout, rollout["rollout_id"]).status == "reverted"
        head = db.scalar(
            select(PolicyHead).where(
                PolicyHead.namespace_id == env["ns"], PolicyHead.target == "retrieval_recipe"
            )
        )
        assert (head.policy_id, head.revision) == unchanged_head
    # Direct source revocation also hides stored replay bodies; source-derived
    # caches never become authority, even after successful synthetic evaluation.
    with app.db.transaction() as db:
        source = db.get(ObjectRow, case["frozen_units"][0]["source_id"])
        source.state = "revoked"
    assert app.evolution.replay_detail(actor, started["replay_id"])["trace"] == []
    with app.db.read() as db:
        assert (
            len(
                db.scalars(
                    select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
                ).all()
            )
            == 8
        )
        assert db.get(AgentRun, started["run_id"]).status == "succeeded"


@pytest.mark.parametrize(
    "before,after,count,groups,decision,reason",
    [
        (1.0, 0.5, 4, 2, "failed", "quality_regression"),
        (0.5, 0.5, 4, 2, "inconclusive", "improvement_not_established"),
        (0.5, 1.0, 1, 1, "inconclusive", "insufficient_independent_groups"),
    ],
)
def test_r3_independent_decision_keeps_original_strict_gain_and_sample_gates(
    before, after, count, groups, decision, reason
):
    from careerforge.services.evolution import paired_report

    thresholds = ReplayThresholds(min_cases=4, min_groups=2).model_dump()
    report = paired_report(
        [
            {
                "case_id": str(i),
                "group_id": str(i % groups),
                "baseline": before,
                "candidate": after,
                "safety_violations": 0,
            }
            for i in range(count)
        ],
        4,
        2,
        thresholds,
    )
    assert (report["decision"], report["reason"]) == (decision, reason)


@pytest.mark.asyncio
async def test_r3_paid_partial_stage_resume_reuses_receipts_and_budget_exhaustion_is_inconclusive(
    env, monkeypatch
):
    from careerforge.errors import LeaseLost
    from careerforge.models import AgentRun, ModelReceipt

    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await setup_r3(env, paid=True)
    started = app.evolution.start_replay(
        actor,
        candidate,
        ReplayInput(
            dataset_id=dataset["dataset_id"],
            funding_id=env["funding"],
            per_arm_call_cap=2,
            idempotency_key="partial-r3",
        ),
    )
    epoch = app.runtime.claim(started["run_id"], "r3-partial")
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    save = app.runtime.save_stage
    stage = app.runtime.stage

    def pause(run_id, epoch, worker, key, trace):
        save(run_id, epoch, worker, key, trace)
        if key == "replay:0:baseline:result":
            raise LeaseLost()

    monkeypatch.setattr(app.runtime, "save_stage", pause)
    with pytest.raises(LeaseLost):
        await app.evolution.execute_replay(run, epoch, "r3-partial")
    saved = stage(run.id, "replay:0:baseline:result")
    before = len(env["model"].calls)
    monkeypatch.setattr(app.runtime, "save_stage", save)

    def poisoned(run_id, key):
        result = stage(run_id, key)
        if key == "replay:0:baseline:result":
            result = {**deepcopy(result), "frozen_input_hash": "0" * 64}
        return result

    monkeypatch.setattr(app.runtime, "stage", poisoned)
    with pytest.raises(DomainError) as failure:
        await app.evolution.execute_replay(run, epoch, "r3-partial")
    assert failure.value.code == "R3_REPLAY_BINDING"
    assert len(env["model"].calls) == before
    monkeypatch.setattr(app.runtime, "stage", stage)
    await app.evolution.execute_replay(run, epoch, "r3-partial")
    app.runtime.finish(run.id, epoch, "r3-partial", "succeeded")
    report = stage(run.id, "result")
    assert report["decision"] == "passed", report
    assert report["model_calls"] == 8
    assert stage(run.id, "replay:0:baseline:result") == saved
    with app.db.read() as db:
        receipts = db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)).all()
        assert len(receipts) == 8 and len({r.id for r in receipts}) == 8
        assert sum(r.stage_key.startswith("replay:0:baseline:") for r in receipts) == 2
    _, limited = await execute(env, candidate, dataset, key="r3-budget-insufficient", cap=1)
    assert limited["decision"] == "inconclusive", limited
    assert limited["reason"] == "incomplete_or_budget_limited_paired_execution"
    assert limited["model_calls"] == 4
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError):
            release(actor, candidate, limited["evaluation_id"], 1)


@pytest.mark.parametrize("scope", ["source", "topic"])
def test_r3_hard_scope_cannot_be_replaced_by_pinned_membership(scope):
    case, result = pure_case()
    if scope == "source":
        case["focus"] = {"project_source_ids": ["another-source"], "topic_keys": []}
    else:
        case["topic_keys"] = ["another.topic"]
    with pytest.raises(DomainError) as failure:
        validate_r3_labels(case)
    assert failure.value.code == "R3_GOLD_SCOPE"
    assert score_r3_case(case, result, None)["hard_gate_violations"] == 1


@pytest.mark.asyncio
async def test_r3_recomputed_selector_rejects_fully_rehashed_derived_selection(env):
    """Privileged local derived-storage corruption, not an HTTP permission bypass."""
    from careerforge.models import EvolutionEvaluation, StageArtifact
    from careerforge.models_learning_ext import PolicyReplayManifest
    from careerforge.services.bounded_evidence import select_bounded_facts
    from careerforge.services.evolution import paired_report
    from careerforge.services.evolution_retrieval_r3 import check_r3_trace

    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await setup_r3(env)
    with app.db.transaction() as db:
        row = db.get(EvolutionCandidate, candidate)
        row.content = {**row.content, "per_project_limit": 3}
        row.content_hash = fingerprint(row.content)
    started, report = await execute(env, candidate, dataset, key="derived-rebinding", cap=1)
    assert report["decision"] == "inconclusive" and report["improvement"] == 0
    terminal = deepcopy(app.runtime.stage(started["run_id"], "result"))
    with app.db.transaction() as db:
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        frozen, traces = deepcopy(manifest.frozen_input), deepcopy(manifest.trace)
        original_input_hash = fingerprint(frozen)
        for index, case in enumerate(frozen["cases"]):
            trace = next(
                t for t in traces if t["case_id"] == case["case_id"] and t["arm"] == "candidate"
            )
            result = deepcopy(trace["typed_result"])
            sid = result["selection_manifest_id"]
            # Fabricate a favorable narrower subset while the actual admitted
            # candidate recipe and executed selection both used limit=3.
            units, projection = select_bounded_facts(
                {p["publication_id"]: p for p in case["frozen_units"]},
                {pid: (s["bindings"], s["citations"]) for pid, s in case["r3_support"].items()},
                question=case["question"],
                variants=result["query_normalization"]["alias_variants"],
                limit=12000,
                per_project_limit=1,
                max_project_samples=3,
                selection_manifest_id=sid,
            )
            result["knowledge_units"] = units
            result["sampled_source_ids"] = list(dict.fromkeys(u["source_id"] for u in units))
            result["provenance_hash"] = fingerprint(
                {k: v for k, v in result.items() if k not in {"provenance_hash", "elapsed_ms"}}
            )
            selection = db.get(ObjectRow, sid)
            payload = deepcopy(selection.payload)
            payload["publication_ids"] = [u["publication_id"] for u in units]
            payload["selected_source_ids"] = result["sampled_source_ids"]
            payload["evidence_projection"] = projection
            payload["evidence_budget"]["selected_bytes"] = projection["selected_bytes"]
            payload["omitted_publications"] = sorted(
                set(case["publication_ids"]) - set(payload["publication_ids"])
            )
            selection.payload, selection.content_hash = payload, fingerprint(payload)
            trace["typed_result"] = result
            trace["metrics"] = score_r3_case(case, result, None)
            # All mutable hash copies are re-bound, rather than relying on the
            # checker to generate a binding it must now refuse to generate.
            trace["r3_binding"] = {
                **trace["r3_binding"],
                "result_hash": fingerprint(result),
                "selection_hash": fingerprint(payload),
            }
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == started["run_id"],
                    StageArtifact.stage_key == f"replay:{index}:candidate:result",
                )
            )
            stage.payload = deepcopy(trace)
        changed = deepcopy(report)
        for sample in changed["samples"]:
            trace = next(
                t for t in traces if t["case_id"] == sample["case_id"] and t["arm"] == "candidate"
            )
            sample["candidate_metrics"], sample["candidate"] = (
                trace["metrics"],
                trace["metrics"]["score"],
            )
        changed.update(
            paired_report(
                changed["samples"],
                frozen["threshold_policy"]["min_cases"],
                frozen["threshold_policy"]["min_groups"],
                frozen["threshold_policy"],
            )
        )
        assert changed["decision"] == "passed" and changed["improvement"] > 0
        changed["replay_trace_manifest_hash"] = fingerprint(traces)
        manifest.trace, manifest.trace_hash = traces, fingerprint(traces)
        evaluation = db.get(EvolutionEvaluation, report["evaluation_id"])
        evaluation.report, evaluation.decision = changed, changed["decision"]
        assert fingerprint(manifest.frozen_input) == original_input_hash
    calls = len(env["model"].calls)
    with app.db.read() as db:
        for index, case in enumerate(frozen["cases"]):
            trace = next(
                t for t in traces if t["case_id"] == case["case_id"] and t["arm"] == "candidate"
            )
            with pytest.raises(DomainError) as failure:
                check_r3_trace(
                    app.evolution, db, actor, frozen, started["run_id"], index, case, trace
                )
            assert failure.value.code == "R3_REPLAY_BINDING"
        with pytest.raises(DomainError) as failure:
            app.evolution.checked_evaluation(
                db, actor, db.get(EvolutionCandidate, candidate), report["evaluation_id"]
            )
        assert failure.value.code == "R3_REPLAY_BINDING"
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError) as failure:
            release(actor, candidate, report["evaluation_id"], 1)
        assert failure.value.code == "R3_REPLAY_BINDING"
    assert app.runtime.stage(started["run_id"], "result") == terminal
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_r3_terminal_report_binds_all_fields_and_rejects_rehashed_latency_pass(
    env, monkeypatch
):
    from types import SimpleNamespace

    from careerforge.models import AgentRun, EvolutionEvaluation, StageArtifact
    from careerforge.models_learning_ext import PolicyReplayManifest
    from careerforge.services import evolution_replay
    from careerforge.services.evolution import paired_report
    from careerforge.services.evolution_retrieval_r3 import check_r3_terminal_report

    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await setup_r3(env)
    # A deterministic synthetic clock proves gate/control flow, not real latency.
    ticks = iter([0.0, 0.01, 1.0, 1.1, 2.0, 2.1, 3.0, 3.01])
    with monkeypatch.context() as patch:
        patch.setattr(evolution_replay, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
        started, report = await execute(env, candidate, dataset, key="terminal-r3", cap=1)
    assert report["decision"] == "failed"
    assert report["reason"] == "frozen_cost_or_latency_regression"
    assert report["latency_ratio"] > 5
    terminal = deepcopy(app.runtime.stage(started["run_id"], "result"))
    calls = len(env["model"].calls)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    # A valid committed failed result is idempotent too; never turn it into a pass.
    await app.evolution.execute_replay(run, run.epoch, "terminal-check-only")
    assert len(env["model"].calls) == calls
    with app.db.transaction() as db:
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        evaluation = db.get(EvolutionEvaluation, report["evaluation_id"])
        saved = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == started["run_id"], StageArtifact.stage_key == "result"
            )
        )
        check_r3_terminal_report(db, manifest, evaluation)
        # Exact report equality binds all fields, not only the latest exploit.
        for field, value in (
            ("decision", "passed"),
            ("candidate_score", 0.0),
            ("model_calls", 999),
            ("latency_ratio", 1.0),
            ("call_ratio", 999.0),
            ("typed_execution_contract", {}),
            ("frozen_input_hash", "0" * 64),
            ("replay_trace_manifest_hash", "0" * 64),
            ("evaluation_id", "other-evaluation"),
        ):
            saved.payload = {**terminal, field: value}
            with pytest.raises(DomainError) as failure:
                check_r3_terminal_report(db, manifest, evaluation)
            assert failure.value.code == "R3_TERMINAL_BINDING"
        saved.payload = deepcopy(terminal)
        original_hash = saved.input_hash
        saved.input_hash = "0" * 64
        with pytest.raises(DomainError) as failure:
            check_r3_terminal_report(db, manifest, evaluation)
        assert failure.value.code == "R3_TERMINAL_BINDING"
        saved.input_hash = original_hash
        saved_id = saved.id
        db.delete(saved)
        db.flush()
        with pytest.raises(DomainError) as failure:
            check_r3_terminal_report(db, manifest, evaluation)
        assert failure.value.code == "R3_TERMINAL_BINDING"
        db.add(
            StageArtifact(
                id=saved_id,
                run_id=started["run_id"],
                stage_key="result",
                input_hash=original_hash,
                payload=deepcopy(terminal),
            )
        )
        db.flush()
        check_r3_terminal_report(db, manifest, evaluation)
        # Privileged local derived-storage corruption. The admitted input,
        # original terminal content, original receipts and selection stay fixed.
        traces = deepcopy(manifest.trace)
        for trace in traces:
            trace["latency_ms"] = 1.0
            index = next(
                i
                for i, case in enumerate(manifest.frozen_input["cases"])
                if case["case_id"] == trace["case_id"]
            )
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == started["run_id"],
                    StageArtifact.stage_key == f"replay:{index}:{trace['arm']}:result",
                )
            )
            stage.payload = deepcopy(trace)
        changed = deepcopy(evaluation.report)
        policy = manifest.frozen_input["threshold_policy"]
        changed.update(
            paired_report(changed["samples"], policy["min_cases"], policy["min_groups"], policy)
        )
        assert changed["decision"] == "passed"
        changed["latency_ratio"] = 1.0
        changed["replay_trace_manifest_hash"] = fingerprint(traces)
        manifest.trace, manifest.trace_hash = traces, fingerprint(traces)
        evaluation.report, evaluation.decision = changed, changed["decision"]
    with app.db.read() as db, pytest.raises(DomainError) as failure:
        app.evolution.checked_evaluation(
            db, actor, db.get(EvolutionCandidate, candidate), report["evaluation_id"]
        )
    assert failure.value.code == "R3_TERMINAL_BINDING"
    for release in (app.evolution.promote, app.rollouts.start):
        with pytest.raises(DomainError) as failure:
            release(actor, candidate, report["evaluation_id"], 1)
        assert failure.value.code == "R3_TERMINAL_BINDING"
    with pytest.raises(DomainError) as failure:
        await app.evolution.execute_replay(run, run.epoch, "terminal-check-only")
    assert failure.value.code == "R3_TERMINAL_BINDING"
    assert app.runtime.stage(started["run_id"], "result") == terminal
    assert len(env["model"].calls) == calls
