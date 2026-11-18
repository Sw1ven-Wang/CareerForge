"""Real Coordinator/Retrieval paired replay on pinned, hash-checked inputs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import RetrievalTaskSpec, TaskType
from ..contracts_learning_ext import TypedReplayDatasetInput
from ..contracts_retrieval_replay import RetrievalR3ReplayDatasetInput
from ..errors import DomainError
from ..models import (
    EvalDataset,
    EvolutionEvaluation,
    KnowledgeDocument,
    KnowledgeEmbedding,
    ObjectRow,
    Principal,
    SourceSet,
)
from ..util import fingerprint
from .evolution_retrieval_r3 import (
    R3_SCOPE,
    check_r3_case,
    freeze_r3_case,
    is_r3,
    r3_execution_contract,
    r3_label_record_hash,
    r3_policy_fields,
    r3_spec,
    score_r3_case,
)
from .orchestration import Coordinator
from .retrieval import RetrievalService
from .security import accessible_namespaces, namespace_admin
from .sources import SourceService
from .taxonomy import require_topic, taxonomy_revision

TYPED_RELEASE_SCOPE = (
    "actual_coordinator_and_retrieval_on_frozen_catalog_and_indexes_not_universal_quality"
)


def typed_execution_contract(value: Any = None) -> dict[str, Any]:
    """Bind this evaluator to its historical whole-publication executor.

    Missing markers mean the original contract, never the latest conversation
    default. A bounded-fact executor needs its own compatible scorer and release
    evidence; a new service default cannot silently change this frozen replay.
    """
    contract = {
        "version": "whole-publication-typed-execution-1.6.6",
        "retrieval_context_contract": None,
        "scorer_contract": "exact-frozen-publication-1.6.6",
    }
    if is_r3(value):
        return r3_execution_contract()
    if value is not None and value != contract:
        raise DomainError(
            "TYPED_EXECUTION_CONTRACT", "该执行/评分合同不受当前固定整包回放支持。", 422
        )
    return contract


def check_typed_retrieval_release(
    contract: dict[str, Any],
    report: dict[str, Any],
    *,
    mode: str,
    provenance: str,
    admitted_retrieval_context_contract: str,
) -> None:
    """Historical process evidence cannot certify a different deployed selector."""
    contract = typed_execution_contract(contract)
    if is_r3(contract):
        real = provenance == "human_gold"
        if (
            report.get("retrieval_evaluation_scope") != R3_SCOPE
            or report.get("admitted_retrieval_context_contract")
            != admitted_retrieval_context_contract
            or contract["retrieval_context_contract"] != admitted_retrieval_context_contract
            or report.get("fixture_process_only") != (not real)
            or report.get("current_retrieval_quality_validated") is not real
            or (real and report.get("r3_independent_labels_qualified") is not True)
            or (real and report.get("synthetic_execution") is not False)
            or (not real and mode != "test")
        ):
            raise DomainError("REPLAY_DEPLOYMENT_CONTRACT", "r3 评测合同或独立真实证据不足。", 422)
        return
    if (
        report.get("retrieval_evaluation_scope") != "historical_whole_publication_selection_only"
        or report.get("admitted_retrieval_context_contract") != admitted_retrieval_context_contract
        or report.get("current_retrieval_quality_validated") is not False
        or report.get("fixture_process_only") != (provenance == "fixture")
    ):
        raise DomainError("SEMANTIC_REPLAY_REQUIRED", "检索收益的适用范围不匹配。", 422)
    # Existing test-only fixture promotion exercises the release process; it has
    # never authorized production deployment. Human Gold and every non-test mode
    # must match the executor used by new admissions.
    if contract["retrieval_context_contract"] != admitted_retrieval_context_contract and not (
        mode == "test" and provenance == "fixture"
    ):
        raise DomainError(
            "REPLAY_DEPLOYMENT_CONTRACT",
            "旧整包检索评测不能批准当前完整事实检索；需要匹配执行/评分合同的独立评测。",
            422,
        )


def check_typed_execution_trace(
    trace: dict[str, Any], target: str, execution_contract: Any = None
) -> None:
    """Resume old unmarked stages only when their actual output is historical."""
    contract = typed_execution_contract(execution_contract)
    if typed_execution_contract(trace.get("typed_execution_contract")) != contract:
        raise DomainError("TYPED_EXECUTION_CONTRACT", "回放阶段的执行/评分合同不匹配。", 422)
    if target == "retrieval_recipe" and trace.get("error_code") is None:
        result = trace.get("typed_result")
        normalization = result.get("query_normalization") if isinstance(result, dict) else None
        if (
            not isinstance(normalization, dict)
            or normalization.get("retrieval_context_contract")
            != contract["retrieval_context_contract"]
        ):
            raise DomainError(
                "TYPED_EXECUTION_CONTRACT", "检索阶段输出不属于固定的整包执行合同。", 422
            )


def typed_snapshot(
    service: Any, db: Session, actor: str, case: dict[str, Any], target: str
) -> dict[str, Any]:
    for key in set(case["topic_keys"] + case["focus"]["topic_keys"]):
        require_topic(db, actor, key)
    for identifier in case["focus"]["project_source_ids"]:
        SourceService.get(db, actor, identifier)
    snapshot: dict[str, Any] = {
        "taxonomy": taxonomy_revision(db, actor),
        "model_profile": service.app.provider.profile_version,
        "embedding_dimensions": service.app.settings.embedding_dimensions,
        "embedding_model": service.app.settings.embedding_model,
        "rerank_model": service.app.settings.rerank_model,
    }
    if target == "routing_policy":
        scopes = [n.id for n in accessible_namespaces(db, actor)]
        sources = list(
            db.scalars(
                select(SourceSet)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(
                    ObjectRow.namespace_id.in_(scopes),
                    ObjectRow.state == "active",
                )
                .order_by(SourceSet.id)
                .limit(101)
            )
        )
        focused = db.scalars(
            select(SourceSet).where(SourceSet.id.in_(case["focus"]["project_source_ids"]))
        ).all()
        # Matches Coordinator's bounded source catalog, including a truncation flag.
        catalog = {s.id: s for s in [*sources[:100], *focused]}
        snapshot["catalog_truncated"] = len(sources) > 100
        snapshot["catalog"] = [
            {
                "source_id": source.id,
                "repository": source.repository_key,
                "commit": source.commit_sha,
                "metadata_hash": fingerprint(source.metadata_json),
            }
            for source in sorted(catalog.values(), key=lambda s: s.id)
        ]
    else:
        documents, embeddings = {}, {}
        for identifier in case["publication_ids"]:
            pack = service.app.knowledge.publication_pack(db, actor, identifier)
            document = db.get(KnowledgeDocument, identifier)
            if document is None or document.content_hash != fingerprint(pack):
                raise DomainError(
                    "REPLAY_INDEX_REQUIRED",
                    "检索回放需先建立与固定知识版本一致的真实词法索引。",
                    422,
                )
            documents[identifier] = {
                "content_hash": document.content_hash,
                "search_text_hash": fingerprint(document.search_text),
            }
            vector = db.get(KnowledgeEmbedding, (identifier, service.app.provider.profile_version))
            if vector is not None:
                if vector.content_hash != fingerprint(
                    {"text": vector.search_text, "model": vector.model_version}
                ):
                    raise DomainError("REPLAY_INDEX_HASH", "向量索引内容摘要不一致。")
                embeddings[identifier] = {
                    "model": vector.model_version,
                    "dimensions": vector.dimensions,
                    "content_hash": vector.content_hash,
                    "vector_hash": fingerprint(list(vector.vector)),
                }
        snapshot["documents"], snapshot["embeddings"] = documents, embeddings
        snapshot["channels_available"] = list(service.app.retrieval._available())
    return snapshot


def register_typed_dataset(
    service: Any, actor: str, value: TypedReplayDatasetInput | RetrievalR3ReplayDatasetInput
) -> dict[str, Any]:
    from .evolution_replay import REPLAY_VERSION, independence_groups

    r3 = isinstance(value, RetrievalR3ReplayDatasetInput)
    extra_policy = (
        r3_policy_fields(value, actor) if isinstance(value, RetrievalR3ReplayDatasetInput) else {}
    )
    with service.db.transaction() as db:
        ns = namespace_admin(db, actor, value.namespace_id)
        if ns.kind != "private":
            raise DomainError("REPLAY_PRIVATE", "含私人学习与路由上下文的回放不能共享。", 403)
        if service.db.settings.mode != "test" and value.provenance == "fixture":
            raise DomainError("FIXTURE_NOT_GOLD", "合成语料不能登记为生产 Gold。", 422)
        if len({c.case_id for c in value.cases}) != len(value.cases):
            raise DomainError("DATASET_DUPLICATE", "样例身份重复。", 422)
        if value.provenance == "human_gold" and (
            len(set(value.annotators)) < 2
            or any(not db.get(Principal, a) for a in value.annotators)
        ):
            raise DomainError("ANNOTATORS", "人工 Gold 需要独立可认证标注者。", 422)
        if value.provenance == "human_gold" and value.split == "holdout":
            calibration = (
                db.get(EvolutionEvaluation, value.thresholds.baseline_calibration_id)
                if value.thresholds.baseline_calibration_id
                else None
            )
            before = db.get(EvalDataset, calibration.dataset_id) if calibration else None
            if (
                not calibration
                or not before
                or before.namespace_id != ns.id
                or before.split != "development"
                or before.threshold_policy.get("target") != value.target
                or calibration.report.get("replay_version") != REPLAY_VERSION
                or (r3 and not is_r3(calibration.report.get("typed_execution_contract")))
                or (r3 and before.provenance != "human_gold")
                or value.thresholds.min_cases < 20
                or value.thresholds.min_groups < 10
            ):
                raise DomainError(
                    "BASELINE_CALIBRATION_REQUIRED",
                    "正式验收门槛需同目标的开发集真实基线和足够独立组。",
                    422,
                )
        if db.scalar(
            select(EvalDataset.id).where(
                EvalDataset.namespace_id == ns.id,
                EvalDataset.name == value.name,
                EvalDataset.version == value.version,
            )
        ):
            raise DomainError("DATASET_IMMUTABLE", "冻结数据集版本不可覆盖。")
        cases = []
        for item in value.cases:
            case = item.model_dump(mode="json")
            units = [
                service.app.knowledge.publication_pack(db, actor, p) for p in item.publication_ids
            ]
            if r3:
                if value.provenance == "human_gold" and any(
                    u.get("synthetic") is not False for u in units
                ):
                    raise DomainError("SYNTHETIC_GOLD", "合成知识不能用作真实 r3 Gold。", 422)
            elif value.target == "retrieval_recipe":
                if (
                    not units
                    or not case["expected"]["relevant_publication_ids"]
                    or not set(case["expected"]["relevant_publication_ids"])
                    <= set(item.publication_ids)
                ):
                    raise DomainError(
                        "GOLD_SCOPE", "检索 Gold 的相关发布必须位于明确冻结候选池中。", 422
                    )
                if value.provenance == "human_gold" and not case["expected"]["relevance_complete"]:
                    raise DomainError(
                        "GOLD_INCOMPLETE",
                        "正式检索 Gold 需明确完整标注候选池，不能把未标注项暗当负例。",
                        422,
                    )
            elif case["expected"]["task_type"] is None or case["expected"]["action"] is None:
                raise DomainError("GOLD_SCHEMA", "路由 Gold 需标注意图和服务动作。", 422)
            sources = set(item.focus.project_source_ids) | {
                u["source_id"] for u in units if u["origin_kind"] == "repository"
            }
            families = set()
            for identifier in sources:
                source = SourceService.get(db, actor, identifier)
                families.add(source.metadata_json.get("repository_family", source.repository_key))
            for identifier in getattr(item.expected, "source_ids", []):
                SourceService.get(db, actor, identifier)
            for key in getattr(item.expected, "topic_keys", []):
                require_topic(db, actor, key)
            case["repository_family"] = sorted(families)[0] if families else ""
            case["repository_families"] = sorted(families)
            case["frozen_units"] = units
            case["typed_snapshot"] = typed_snapshot(service, db, actor, case, value.target)
            if r3:
                freeze_r3_case(service, db, actor, case)
            if value.target == "routing_policy" and not set(case["expected"]["source_ids"]) <= {
                s["source_id"] for s in case["typed_snapshot"]["catalog"]
            }:
                raise DomainError("GOLD_SCOPE", "路由 Gold 的来源不在该冻结实际目录中。", 422)
            cases.append(case)
        if r3 and extra_policy["r3_label_provenance"]["record_sha256"] != r3_label_record_hash(
            cases
        ):
            raise DomainError("R3_LABEL_PROVENANCE", "独立标注记录摘要与完整样例不一致。", 422)
        policy = {
            **value.thresholds.model_dump(mode="json"),
            "kind": "typed_execution_replay",
            "target": value.target,
            "annotators": sorted(set(value.annotators)),
            "max_safety_violations": 0,
            "confidence": 0.95,
            "replay_version": REPLAY_VERSION,
            "typed_execution_contract": typed_execution_contract(),
            "declared_split": value.split,
            **extra_policy,
        }
        row = EvalDataset(
            namespace_id=ns.id,
            name=value.name,
            version=value.version,
            provenance=value.provenance,
            cases=cases,
            threshold_policy=policy,
            split=value.split,
            frozen_hash=fingerprint({"cases": cases, "policy": policy}),
        )
        db.add(row)
        db.flush()
        return {
            "dataset_id": row.id,
            "frozen_hash": row.frozen_hash,
            "split": row.split,
            "independent_groups": len(set(independence_groups(cases).values())),
            "requires_individual_attestations": value.provenance == "human_gold",
            "external_acceptance_blocker": "real_independent_relevance_and_routing_labels_not_yet_validated",
        }


def check_typed_case(
    service: Any,
    db: Session,
    actor: str,
    case: dict[str, Any],
    target: str,
    execution_contract: Any = None,
) -> None:
    if ("r3_support" in case or "r3_labels_hash" in case) != is_r3(execution_contract):
        raise DomainError("TYPED_EXECUTION_CONTRACT", "事实标签不能混用历史整包合同。", 422)
    if typed_snapshot(service, db, actor, case, target) != case["typed_snapshot"]:
        raise DomainError("REPLAY_INDEX_CHANGED", "回放的目录、主题、词法或向量索引已经变化。")
    if is_r3(execution_contract):
        check_r3_case(service, db, actor, case)


async def execute_typed_case(
    service: Any,
    gateway: Any,
    run: tuple[str, int, str],
    key: str,
    policy: dict[str, Any],
    policy_id: str,
    case: dict[str, Any],
    target: str,
    actor: str,
    *,
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = typed_execution_contract(execution_contract)
    if is_r3(contract) and target != "retrieval_recipe":
        raise DomainError("TYPED_EXECUTION_CONTRACT", "r3 合同只支持检索配对评测。", 422)
    if target == "routing_policy":
        decision, orchestration = await Coordinator(service.db, gateway).decide(
            actor,
            case["question"],
            case["focus"],
            1,
            {"routing_policy": {"content": policy, "policy_id": policy_id}},
            run,
            retrieval_context_contract=contract["retrieval_context_contract"],
        )
        return {
            "intent": decision.model_dump(mode="json"),
            "orchestration": orchestration.model_dump(mode="json"),
            "executed_action": False,
        }
    spec = RetrievalTaskSpec(
        turn_id=key + "retrieval",
        task_type=TaskType(case["task_type"]),
        user_question=case["question"],
        focus_revision=1,
        topic_keys=case["topic_keys"],
        pinned_publication_ids=case["publication_ids"],
        project_source_ids=sorted(
            {u["source_id"] for u in case["frozen_units"] if u["origin_kind"] == "repository"}
        ),
        per_project_limit=policy["per_project_limit"],
        freshness_policy="explicit_pinned",
    )
    if is_r3(contract):
        spec = r3_spec(case, policy, run[0], key)
    return await RetrievalService(
        service.db, service.app.knowledge, service.app.cache, gateway
    ).retrieve(
        actor,
        spec,
        run,
        recipe=policy,
        recipe_version=policy_id,
        step_prefix=key + run[0] + ":" if is_r3(contract) else key,
        retrieval_context_contract=contract["retrieval_context_contract"],
    )


def score_typed_execution(
    case: dict[str, Any],
    result: dict[str, Any],
    error: str | None,
    target: str,
    execution_contract: Any = None,
) -> dict[str, Any]:
    contract = typed_execution_contract(execution_contract)
    if is_r3(contract):
        if target != "retrieval_recipe":
            raise DomainError("TYPED_EXECUTION_CONTRACT", "r3 不能复用路由评分。", 422)
        return score_r3_case(case, result, error)
    return score_typed_case(case, result, error, target)


def score_typed_case(
    case: dict[str, Any], result: dict[str, Any], error: str | None, target: str
) -> dict[str, Any]:
    expected = case["expected"]
    hard = int(error is not None and error not in {"REPLAY_ARM_BUDGET", "BUDGET_EXHAUSTED"})
    if target == "routing_policy":
        decision, orchestration = result.get("intent", {}), result.get("orchestration", {})
        known = {s["source_id"] for s in case["typed_snapshot"]["catalog"]}
        hard += int(not set(decision.get("project_source_ids", [])) <= known)
        intent = float(decision.get("interpreted_intent") == expected["task_type"])
        action = float(orchestration.get("action") == expected["action"])
        references = float(
            set(decision.get("project_source_ids", [])) == set(expected["source_ids"])
            and set(decision.get("topic_keys", [])) == set(expected["topic_keys"])
            and decision.get("reference_resolution") == expected["reference_resolution"]
        )
        return {
            "score": (intent + action + references) / 3,
            "intent_accuracy": intent,
            "action_accuracy": action,
            "reference_accuracy": references,
            "hard_gate_violations": hard,
        }
    units = result.get("knowledge_units", [])
    returned = {u["publication_id"] for u in units}
    relevant = set(expected["relevant_publication_ids"])
    pinned = {u["publication_id"]: u for u in case["frozen_units"]}
    hard += int(not returned <= pinned.keys())
    hard += sum(pinned.get(u["publication_id"]) != u for u in units)
    recall = len(returned & relevant) / len(relevant)
    precision = (
        len(returned & relevant) / max(1, len(returned)) if expected["relevance_complete"] else None
    )
    projects = {pinned[p]["source_id"] for p in relevant}
    project_recall = len({pinned[p]["source_id"] for p in returned & relevant}) / len(projects)
    values = [recall, project_recall] + ([precision] if precision is not None else [])
    return {
        "score": sum(values) / len(values),
        "publication_recall": recall,
        "project_recall": project_recall,
        "precision": precision,
        "hard_gate_violations": hard,
    }
