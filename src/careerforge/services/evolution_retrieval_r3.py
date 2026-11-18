"""Independent fact/evidence relevance scoring for the explicitly frozen r3 executor.

This module never generates labels, changes retrieval ranking, or calls a model to
grade itself. Selection identity is checked separately from relevance: an exact
authorized projection can still score poorly against the independent labels.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import RetrievalTaskSpec, TaskType
from ..contracts_retrieval_replay import RetrievalR3ReplayCase, RetrievalR3ReplayDatasetInput
from ..errors import DomainError
from ..models import (
    AgentRun,
    EvalDataset,
    EvolutionCandidate,
    EvolutionEvaluation,
    Experience,
    Feedback,
    Idempotency,
    ModelReceipt,
    StageArtifact,
)
from ..models_learning_ext import PolicyReplayManifest
from ..util import fingerprint
from .bounded_evidence import (
    EVIDENCE_PROJECTION_CONTRACT,
    FACT_SELECTION_CONTRACT,
    byte_size,
    fact_support,
    project_pack,
    select_bounded_facts,
)
from .dependencies import check_closure, require_object
from .explicit_source_paths import LOCATE_PATH_GUARD, SOURCE_PATH_CONTRACT, navigation_path_request
from .retrieval_context import is_topic_continuation
from .security import accessible_namespaces
from .taxonomy import expand_query, resolve_topics, visible_topics

R3_SCOPE = "frozen_r3_complete_fact_evidence_version_relevance_under_12000_utf8_bytes"
R3_METRICS = (
    "fact_recall",
    "fact_precision",
    "evidence_recall",
    "evidence_precision",
    "version_recall",
    "version_precision",
)


def r3_execution_contract() -> dict[str, Any]:
    return {
        "version": "bounded-fact-typed-execution-1.6.6-r3-v1",
        "retrieval_context_contract": "question-focused-retrieval-1.6.6-r3",
        "scorer_contract": "independent-fact-evidence-version-relevance-1.6.6-r3-v1",
        "label_schema": "complete-fact-evidence-version-relevance-1",
        "projection_contract": EVIDENCE_PROJECTION_CONTRACT,
        "selection_contract": FACT_SELECTION_CONTRACT,
        "terminal_report_contract": "frozen-r3-terminal-report-1.6.6-v1",
        "evidence_budget_bytes": 12000,
        "max_project_samples": 3,
    }


def is_r3(value: Any) -> bool:
    return value == r3_execution_contract()


def r3_label_record_hash(cases: list[dict[str, Any]]) -> str:
    """Exact annotation-record bytes are reproducible with canonical JSON."""
    return fingerprint(
        {
            "protocol": "complete-fact-evidence-version-relevance-1",
            "cases": [
                {key: case[key] for key in RetrievalR3ReplayCase.model_fields} for case in cases
            ],
        }
    )


def r3_spec(
    case: dict[str, Any], policy: dict[str, Any], run_id: str, key: str
) -> RetrievalTaskSpec:
    return RetrievalTaskSpec(
        # Cross-run and cross-arm output/selection reuse must be detectable.
        turn_id=f"{run_id}:{key}retrieval",
        task_type=TaskType(case["task_type"]),
        user_question=case["question"],
        focus_revision=1,
        topic_keys=case["topic_keys"],
        topic_hints=case["focus"]["topic_keys"],
        pinned_publication_ids=case["publication_ids"],
        project_source_ids=list(dict.fromkeys(case["focus"]["project_source_ids"]))
        or sorted(
            {u["source_id"] for u in case["frozen_units"] if u["origin_kind"] == "repository"}
        ),
        per_project_limit=policy["per_project_limit"],
        freshness_policy="explicit_pinned",
        max_evidence_tokens=12000,
        max_project_samples=3,
    )


def freeze_r3_case(service: Any, db: Session, actor: str, case: dict[str, Any]) -> None:
    case["r3_support"] = {
        p["publication_id"]: {
            "bindings": bindings,
            "citations": citations,
        }
        for p in case["frozen_units"]
        for bindings, citations in [fact_support(service.app.knowledge, db, actor, p)]
    }
    validate_r3_labels(case)
    case["r3_labels_hash"] = fingerprint(case["expected"])


def validate_r3_labels(case: dict[str, Any]) -> None:
    """Require explicit positive/negative labels for every exact candidate fact."""
    expected = case["expected"]
    universe = {
        (p["publication_id"], f["fact_atom_id"]): (p, f)
        for p in case["frozen_units"]
        for f in p["facts"]
    }
    labels = {(r["publication_id"], r["fact_atom_id"]): r for r in expected["labels"]}
    if (
        expected.get("complete_candidate_pool") is not True
        or len(labels) != len(expected["labels"])
        or labels.keys() != universe.keys()
        or not any(r["relevant"] is True for r in labels.values())
        or len(case["publication_ids"]) != len(set(case["publication_ids"]))
    ):
        raise DomainError(
            "R3_GOLD_INCOMPLETE", "r3 标签必须完整覆盖候选事实，且有独立相关事实。", 422
        )
    for (pid, fid), label in labels.items():
        pack, _ = universe[(pid, fid)]
        support = case["r3_support"][pid]["bindings"][fid]
        if (
            type(label["relevant"]) is not bool
            or any(label[k] != pack[k] for k in ("unit_version_id", "source_id", "commit"))
            or label["support_refs"] != support["support_refs"]
            or label["counterevidence_refs"] != support["counterevidence_refs"]
        ):
            raise DomainError("R3_GOLD_BINDING", "事实标签的版本或完整支持/反证不匹配。", 422)
        if label["relevant"] and not _matches_hard_scope(case, pack):
            raise DomainError("R3_GOLD_SCOPE", "相关事实必须符合固定的来源和硬主题范围。", 422)


def check_r3_case(service: Any, db: Session, actor: str, case: dict[str, Any]) -> None:
    validate_r3_labels(case)
    if case.get("r3_labels_hash") != fingerprint(case["expected"]):
        raise DomainError("R3_LABELS_CHANGED", "独立标签已经变化。", 422)
    for pack in case["frozen_units"]:
        bindings, citations = fact_support(service.app.knowledge, db, actor, pack)
        if case["r3_support"][pack["publication_id"]] != {
            "bindings": bindings,
            "citations": citations,
        }:
            raise DomainError("REPLAY_EVIDENCE_CHANGED", "完整事实支持或反证已变化。")


def r3_policy_fields(value: RetrievalR3ReplayDatasetInput, actor: str) -> dict[str, Any]:
    if value.execution_contract != r3_execution_contract()["version"]:
        raise DomainError("TYPED_EXECUTION_CONTRACT", "r3 数据集需要显式匹配的版本。", 422)
    kind = "synthetic_fixture" if value.provenance == "fixture" else "independent_human"
    if value.label_provenance.kind != kind:
        raise DomainError("R3_LABEL_PROVENANCE", "标签来源与数据集类型不一致。", 422)
    if value.provenance == "human_gold" and (
        actor in value.annotators or len(set(value.annotators)) < 2
    ):
        raise DomainError("R3_LABEL_INDEPENDENCE", "登记者不能代替两名独立标注者。", 422)
    return {
        "typed_execution_contract": r3_execution_contract(),
        "r3_label_provenance": value.label_provenance.model_dump(mode="json"),
        "r3_registered_by": actor,
    }


def check_r3_qualification(
    service: Any, db: Session, actor: str, dataset: EvalDataset, candidate: EvolutionCandidate
) -> None:
    policy = dataset.threshold_policy
    if policy["r3_label_provenance"]["record_sha256"] != r3_label_record_hash(dataset.cases):
        raise DomainError("R3_LABEL_PROVENANCE", "独立标注记录摘要与精确样例不一致。", 422)
    if dataset.provenance == "fixture":
        if policy["r3_label_provenance"]["kind"] != "synthetic_fixture":
            raise DomainError("R3_LABEL_PROVENANCE", "合成标签来源不能重标。", 422)
        return
    authors = {
        feedback.principal_id
        for experience in db.scalars(
            select(Experience).where(Experience.id.in_(candidate.experience_ids))
        )
        if (feedback := db.get(Feedback, experience.feedback_id)) is not None
    } | {actor, policy["r3_registered_by"]}
    annotators = set(policy["annotators"])
    approved = {
        a["principal_id"]
        for a in service._attestations(db, dataset)
        if a["frozen_hash"] == dataset.frozen_hash and a["decision"] == "approve"
    }
    if (
        policy["r3_label_provenance"]["kind"] != "independent_human"
        or annotators & authors
        or len(annotators & approved) < 2
        or any(p.get("synthetic") is not False for c in dataset.cases for p in c["frozen_units"])
    ):
        raise DomainError("R3_LABEL_INDEPENDENCE", "r3 正式评测缺少真实来源和独立认证标签。", 422)
    if dataset.split == "holdout":
        calibration = db.get(EvolutionEvaluation, policy["baseline_calibration_id"])
        before = db.get(EvalDataset, calibration.dataset_id) if calibration else None
        manifest = (
            db.scalar(
                select(PolicyReplayManifest).where(
                    PolicyReplayManifest.evaluation_id == calibration.id
                )
            )
            if calibration
            else None
        )
        run = db.get(AgentRun, manifest.run_id) if manifest else None
        if (
            not calibration
            or not before
            or before.split != "development"
            or before.provenance != "human_gold"
            or before.namespace_id != dataset.namespace_id
            or not is_r3(before.threshold_policy.get("typed_execution_contract"))
            or not is_r3(calibration.report.get("typed_execution_contract"))
            or calibration.report.get("retrieval_evaluation_scope") != R3_SCOPE
            or calibration.report.get("dataset_hash") != before.frozen_hash
            or calibration.report.get("r3_independent_labels_qualified") is not True
            or calibration.report.get("synthetic_execution") is not False
            or not manifest
            or not run
            or run.status != "succeeded"
            or run.workflow != "evolution_replay"
            or calibration.run_id != run.id
            or run.principal_id != actor
            or run.input_manifest != manifest.frozen_input
            or run.input_hash != manifest.frozen_hash
            or manifest.frozen_hash != fingerprint(manifest.frozen_input)
            or manifest.frozen_hash != calibration.report.get("frozen_input_hash")
            or manifest.trace_hash != fingerprint(manifest.trace)
            or manifest.trace_hash != calibration.report.get("replay_trace_manifest_hash")
            or calibration.report.get("engine_versions") != manifest.frozen_input["engine_versions"]
        ):
            raise DomainError(
                "BASELINE_CALIBRATION_REQUIRED", "r3 门槛需要可复核的独立真实开发集执行。", 422
            )
        # Development does not recurse into calibration. Recheck its source
        # authority, labels, engine and receipts before trusting its baseline.
        service._validate_replay(db, actor, manifest.frozen_input)
        service._checked_replay_trace(db, manifest, calibration)


def r3_input_hash(frozen: dict[str, Any], case: dict[str, Any]) -> str:
    # Labels have a separate binding. None of them become executor/model input.
    return fingerprint(
        {
            "case": {k: v for k, v in case.items() if k not in {"expected", "r3_labels_hash"}},
            "contract": frozen["typed_execution_contract"],
            "selection_manifest_id": frozen["selection_manifest_id"],
            "per_arm_call_cap": frozen["per_arm_call_cap"],
            "per_arm_request_cap": frozen["per_arm_request_cap"],
            "engine_versions": frozen["engine_versions"],
        }
    )


def check_r3_admission(db: Session, actor: str, frozen: dict[str, Any]) -> None:
    """Bind budgets and full run input to the separate original admission receipt."""
    receipt = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "run.create",
            Idempotency.key == frozen.get("r3_runtime_admission_key"),
        )
    )
    run = db.get(AgentRun, receipt.result.get("run_id")) if receipt else None
    if (
        not receipt
        or not run
        or run.principal_id != actor
        or run.workflow != "evolution_replay"
        or run.input_manifest != frozen
        or run.input_hash != fingerprint(frozen)
        or run.call_cap != frozen["total_call_cap"]
        or run.call_cap != 2 * len(frozen["cases"]) * frozen["per_arm_call_cap"]
        or receipt.request_hash
        != fingerprint(
            {
                "ns": run.namespace_id,
                "funding": run.funding_id,
                "workflow": "evolution_replay",
                "input": frozen,
                "cap": run.call_cap,
                "minutes": 120,
            }
        )
    ):
        raise DomainError("R3_ADMISSION_CHANGED", "r3 原始受理记录、固定预算或输入已变化。", 422)


def _matches_hard_scope(case: dict[str, Any], pack: dict[str, Any]) -> bool:
    sources = case.get("focus", {}).get("project_source_ids", [])
    topics = case.get("topic_keys", [])
    return (not sources or pack["source_id"] in sources) and (
        not topics
        or any(
            link["relation"] != "mentions" and link["topic_key"] in topics
            for link in pack["topic_links"]
        )
    )


def reconstruct_r3_selection(
    service: Any,
    db: Session,
    actor: str,
    spec: RetrievalTaskSpec,
    frozen_units: list[dict[str, Any]],
    frozen_support: dict[str, Any],
    selection_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Reproduce selection from admitted inputs, not mutable derived witnesses.

    This r3 selector intentionally ignores paid vector/reranker/rewrite rankings
    when allocating complete facts. Its exact question aliases, authorized pool,
    policy limits and byte budget suffice. No Gold labels or model calls enter
    this reconstruction. Independent labels still determine relevance scores.
    """
    if str(UUID(selection_id)) != selection_id or UUID(selection_id).version != 4:
        raise ValueError("Selection identity must retain the actual fixed-width UUID contract")
    inferred = resolve_topics(service.db, spec.user_question, actor)
    visible = {topic.key for topic in visible_topics(db, actor)}
    hints = list(dict.fromkeys(k for k in [*spec.topic_hints, *inferred] if k in visible))[:30]
    expansion = hints if is_topic_continuation(spec.user_question) else inferred
    topics = list(dict.fromkeys([*spec.topic_keys, *expansion]))
    variants = expand_query(service.db, spec.user_question, topics, actor)
    normalization = {
        "original_query": spec.user_question,
        "alias_variants": variants,
        "variants": variants,
        "selected_topic_keys": topics,
        "hard_topic_keys": spec.topic_keys,
        "topic_hints": hints,
        "facts_approved": False,
        "trace_authority": "untrusted_cache_hint",
        "retrieval_context_contract": r3_execution_contract()["retrieval_context_contract"],
        "ranking_only_topic_hints": [key for key in hints if key not in topics],
    }
    pinned = {unit["publication_id"]: unit for unit in frozen_units}
    path_request = navigation_path_request(
        spec.user_question, contract=SOURCE_PATH_CONTRACT, guard=LOCATE_PATH_GUARD, locate=False
    )
    current = {}
    for publication in service.app.knowledge.selected_publications(
        db, actor, spec.project_source_ids, spec.pinned_publication_ids
    ):
        pack = service.app.knowledge.publication_pack(db, actor, publication.id)
        if pinned.get(publication.id) != pack:
            raise ValueError("Frozen publication pool changed")
        # This evaluator admits non-LOCATE tasks only. The r3 retriever returns
        # a clarification gap for a source-navigation request under those tasks.
        if not path_request and _matches_hard_scope({"topic_keys": spec.topic_keys}, pack):
            current[publication.id] = pack
    units, projection = select_bounded_facts(
        current,
        {
            pid: (frozen_support[pid]["bindings"], frozen_support[pid]["citations"])
            for pid in current
        },
        question=spec.user_question,
        variants=variants,
        limit=spec.max_evidence_tokens,
        per_project_limit=spec.per_project_limit,
        max_project_samples=spec.max_project_samples,
        selection_manifest_id=selection_id,
    )
    return units, projection, normalization


def _selected_facts(case: dict[str, Any], result: dict[str, Any]) -> set[tuple[str, str]]:
    packs = {p["publication_id"]: p for p in case["frozen_units"]}
    found: set[tuple[str, str]] = set()
    seen = set()
    for unit in result["knowledge_units"]:
        pid = unit["publication_id"]
        if pid in seen or pid not in packs or not _matches_hard_scope(case, packs[pid]):
            raise ValueError("Unpinned or duplicate publication")
        seen.add(pid)
        fact_ids = {f["fact_atom_id"] for f in unit["facts"]}
        if len(fact_ids) != len(unit["facts"]):
            raise ValueError("Duplicate fact")
        support = case["r3_support"][pid]
        canonical = project_pack(
            packs[pid],
            fact_ids,
            support["bindings"],
            support["citations"],
            result["selection_manifest_id"],
            force_projection="evidence_projection" in unit,
            allowed_facet_ids={f["id"] for f in unit["facets"]},
        )
        if canonical != unit:
            raise ValueError("Not an exact complete fact/support projection")
        found.update((pid, fid) for fid in fact_ids)
    if sum(byte_size(u) for u in result["knowledge_units"]) > 12000:
        raise ValueError("Evidence byte budget exceeded")
    return found


def score_r3_case(
    case: dict[str, Any], result: dict[str, Any], error: str | None
) -> dict[str, Any]:
    empty = {name: 0.0 for name in R3_METRICS}
    if error:
        return {
            **empty,
            "score": 0.0,
            "hard_gate_violations": int(error not in {"REPLAY_ARM_BUDGET", "BUDGET_EXHAUSTED"}),
            "incomplete_execution": True,
        }
    try:
        returned = _selected_facts(case, result)
    except (KeyError, TypeError, ValueError, DomainError):
        return {**empty, "score": 0.0, "hard_gate_violations": 1, "incomplete_execution": False}
    labels = {(r["publication_id"], r["fact_atom_id"]): r for r in case["expected"]["labels"]}
    relevant = {key for key, row in labels.items() if row["relevant"]}

    def evidence(keys):
        return {
            (pid, fid, kind, ref)
            for pid, fid in keys
            for kind in ("support_refs", "counterevidence_refs")
            for ref in labels[(pid, fid)][kind]
        }

    def versions(keys):
        return {
            tuple(
                labels[key][k] for k in ("publication_id", "unit_version_id", "source_id", "commit")
            )
            for key in keys
        }

    metrics: dict[str, Any] = {}
    for name, actual, wanted in (
        ("fact", returned, relevant),
        ("evidence", evidence(returned), evidence(relevant)),
        ("version", versions(returned), versions(relevant)),
    ):
        # A wrong fact from the right publication/version is still irrelevant.
        # Version credit requires an actually selected positive fact, not just
        # membership in a publication that contains an omitted positive fact.
        hits = versions(returned & relevant) if name == "version" else actual & wanted
        metrics[name + "_recall"] = len(hits) / max(1, len(wanted))
        metrics[name + "_precision"] = len(hits) / max(1, len(actual))
    return {
        **metrics,
        "score": sum(metrics.values()) / len(metrics),
        "hard_gate_violations": 0,
        "incomplete_execution": False,
    }


def r3_trace_binding(
    service: Any,
    db: Session,
    actor: str,
    frozen: dict[str, Any],
    run_id: str,
    index: int,
    case: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild from live authority and receipts, never a cached result alone."""
    try:
        arm = trace["arm"]
        if arm not in {"baseline", "candidate"}:
            raise ValueError("Unknown arm")
        key = f"replay:{index}:{arm}:"
        policy = frozen["before" if arm == "baseline" else "after"]
        policy_hash = frozen["base_hash" if arm == "baseline" else "candidate_hash"]
        result = trace["typed_result"]
        if (
            trace["case_id"] != case["case_id"]
            or trace["policy_hash"] != policy_hash
            or trace["frozen_input_hash"] != r3_input_hash(frozen, case)
            or trace["metrics"] != score_r3_case(case, result, trace["error_code"])
        ):
            raise ValueError("Case, policy, labels, metrics or execution inputs differ")
        receipts = list(
            db.scalars(
                select(ModelReceipt)
                .where(ModelReceipt.run_id == run_id, ModelReceipt.stage_key.startswith(key))
                .order_by(ModelReceipt.id)
            )
        )
        ids = trace["receipt_ids"]
        if (
            len(ids) != len(set(ids))
            or set(ids) != {r.id for r in receipts}
            or len(receipts) != trace["model_calls"]
            or len(receipts) > frozen["per_arm_call_cap"]
            or trace["model_call_cap"] != frozen["per_arm_call_cap"]
            or len(ids) != len(trace["receipt_request_hashes"])
            or len(ids) != len(trace["receipt_response_hashes"])
            or len(ids) != len(trace["usage"])
        ):
            raise ValueError("Unmatched arm receipt set or budget")
        snapshots = []
        for receipt in receipts:
            offset = ids.index(receipt.id)
            if (
                receipt.request_hash != trace["receipt_request_hashes"][offset]
                or not receipt.stage_key.startswith(key + run_id + ":")
                or fingerprint(receipt.response) != trace["receipt_response_hashes"][offset]
                or receipt.profile_version != frozen["model_profile"]
                or receipt.payer_id != actor
                or (receipt.usage or {}) != trace["usage"][offset]
                or receipt.role not in {"embedding", "reranker", "query_rewriter"}
                or (trace["error_code"] is None and receipt.status != "completed")
            ):
                raise ValueError("Receipt changed or reused")
            snapshots.append(
                {
                    "id": receipt.id,
                    "stage_key": receipt.stage_key,
                    "request_hash": receipt.request_hash,
                    "response_hash": fingerprint(receipt.response),
                    "profile": receipt.profile_version,
                    "role": receipt.role,
                    "payer_id": receipt.payer_id,
                    "epoch": receipt.epoch,
                    "status": receipt.status,
                    "usage": receipt.usage,
                }
            )
        selection = None
        if trace["error_code"] is None:
            _selected_facts(case, result)
            identifier = result["selection_manifest_id"]
            check_closure(db, actor, [identifier])
            row = require_object(db, actor, identifier, "selection_manifest")
            selection = row.payload
            task_spec = r3_spec(case, policy, run_id, key)
            spec = task_spec.model_dump(mode="json")
            actual_spec = selection["task_spec"]
            actual_units, actual_projection, normalization = reconstruct_r3_selection(
                service, db, actor, task_spec, case["frozen_units"], case["r3_support"], identifier
            )
            if (
                result["knowledge_units"] != actual_units
                or selection["evidence_projection"] != actual_projection
                or result["query_normalization"] != normalization
                or selection["omitted_publications"]
                != sorted(
                    {row["publication_id"] for row in actual_projection["fact_ranking"]}
                    - {unit["publication_id"] for unit in actual_units}
                )
                or result["retrieval_manifest_id"] != identifier
                or result["provenance_hash"]
                != fingerprint(
                    {k: v for k, v in result.items() if k not in {"provenance_hash", "elapsed_ms"}}
                )
                or selection["policy_version"] != policy_hash
                or any(
                    actual_spec[k] != v
                    for k, v in spec.items()
                    if k not in {"authorized_namespace_refs", "topic_hints"}
                )
                or set(actual_spec["authorized_namespace_refs"])
                != {n.id for n in accessible_namespaces(db, actor)}
                or actual_spec["topic_hints"] != normalization["topic_hints"]
                or selection["retrieval_context_contract"]
                != frozen["typed_execution_contract"]["retrieval_context_contract"]
                or selection["query_normalization"] != result["query_normalization"]
                or selection["retrieval_plan"] != result["plan"]
                or selection["publication_ids"]
                != [u["publication_id"] for u in result["knowledge_units"]]
            ):
                raise ValueError("Selection is not this frozen run/arm/executor output")
            projection = selection["evidence_projection"]
            size = sum(byte_size(u) for u in result["knowledge_units"])
            records = projection["units"]
            if (
                projection["contract"] != EVIDENCE_PROJECTION_CONTRACT
                or projection["ranking_contract"] != FACT_SELECTION_CONTRACT
                or projection["selected_bytes"] != size
                or selection["evidence_budget"]
                != {"unit": "utf8_bytes", "limit": 12000, "selected_bytes": size}
                or len(records) != len(result["knowledge_units"])
            ):
                raise ValueError("Bounded selection contract changed")
            groups: dict[str, set[str]] = defaultdict(set)
            for unit, record in zip(result["knowledge_units"], records, strict=True):
                pid = unit["publication_id"]
                pack = next(p for p in case["frozen_units"] if p["publication_id"] == pid)
                facts = [f["fact_atom_id"] for f in unit["facts"]]
                if (
                    any(
                        record[k] != unit[k]
                        for k in ("publication_id", "unit_version_id", "source_id")
                    )
                    or record["publication_hash"] != fingerprint(pack)
                    or record["projection_hash"] != fingerprint(unit)
                    or record["selected_fact_ids"] != facts
                    or record["selected_citation_ids"]
                    != [c["citation_id"] for c in unit["citations"]]
                    or record["selected_facet_ids"] != [f["id"] for f in unit["facets"]]
                    or record["fact_support"]
                    != {f: case["r3_support"][pid]["bindings"][f] for f in facts}
                    or record["selected_bytes"] != byte_size(unit)
                ):
                    raise ValueError("Projection not bound to exact selected facts and support")
                groups[unit["source_id"]].add(pid)
            if len(groups) > 3 or any(
                len(ids) > policy["per_project_limit"] for ids in groups.values()
            ):
                raise ValueError("Selection exceeded frozen group bounds")
        return {
            "contract": r3_execution_contract(),
            "run_id": run_id,
            "case_id": case["case_id"],
            "arm": arm,
            "policy_hash": policy_hash,
            "execution_input_hash": r3_input_hash(frozen, case),
            "labels_hash": case["r3_labels_hash"],
            "label_provenance_hash": fingerprint(frozen["threshold_policy"]["r3_label_provenance"]),
            "result_hash": fingerprint(result),
            "selection_hash": fingerprint(selection),
            "engine_hash": fingerprint(frozen["engine_versions"]),
            "receipts": snapshots,
            "synthetic_execution": any(
                r.response is not None and r.response.get("synthetic") is not False
                for r in receipts
            ),
        }
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise DomainError(
            "R3_REPLAY_BINDING", "r3 回放输入、输出、选择或调用回执不匹配。", 422
        ) from exc


def check_r3_trace(
    service: Any,
    db: Session,
    actor: str,
    frozen: dict[str, Any],
    run_id: str,
    index: int,
    case: dict[str, Any],
    trace: dict[str, Any],
) -> None:
    stage = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run_id,
            StageArtifact.stage_key == f"replay:{index}:{trace['arm']}:result",
        )
    )
    if (
        stage is None
        or stage.input_hash != fingerprint(frozen)
        or stage.payload != trace
        or trace.get("r3_binding")
        != r3_trace_binding(service, db, actor, frozen, run_id, index, case, trace)
    ):
        raise DomainError("R3_REPLAY_BINDING", "r3 配对执行绑定已经变化。", 422)


def check_r3_terminal_report(
    db: Session, manifest: PolicyReplayManifest, evaluation: EvolutionEvaluation
) -> None:
    """Bind the entire report to its original atomic terminal result.

    The terminal StageArtifact is an authority anchor, not another report the
    evaluator is allowed to refresh. This does not protect against an actor
    who can rewrite that original anchor along with all derived records.
    """
    frozen, report = manifest.frozen_input, evaluation.report
    run = db.get(AgentRun, manifest.run_id)
    terminal = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == manifest.run_id, StageArtifact.stage_key == "result"
        )
    )
    if (
        not is_r3(frozen.get("typed_execution_contract"))
        or report.get("typed_execution_contract") != frozen["typed_execution_contract"]
        or not run
        or run.workflow != "evolution_replay"
        or run.input_manifest != frozen
        or run.input_hash != manifest.frozen_hash
        or manifest.frozen_hash != fingerprint(frozen)
        or report.get("frozen_input_hash") != manifest.frozen_hash
        or evaluation.run_id != run.id
        or evaluation.id != manifest.evaluation_id
        or evaluation.candidate_id != manifest.candidate_id
        or evaluation.dataset_id != manifest.dataset_id
        or report.get("replay_trace_manifest_hash") != manifest.trace_hash
        or manifest.trace_hash != fingerprint(manifest.trace)
        or "evaluation_id" in report
        or not terminal
        or terminal.input_hash != manifest.frozen_hash
        or terminal.payload != {"evaluation_id": evaluation.id, **report}
    ):
        raise DomainError(
            "R3_TERMINAL_BINDING", "r3 完整评测报告与原子终态记录或身份绑定不一致。", 422
        )
