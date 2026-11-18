"""Actual, budgeted Champion/Candidate execution on frozen independent inputs.

This is an evaluation framework, not a claim of human teaching effectiveness. Gold
labels never enter either Tutor prompt and no model grades or promotes itself.
"""

from __future__ import annotations

import inspect
import time
from math import isfinite
from statistics import mean
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.model import ModelGateway
from ..contracts import ModelOutput
from ..contracts_learning_ext import (
    DatasetAttestationInput,
    ReplayDatasetInput,
    ReplayInput,
    ResearchReplayDatasetInput,
    TypedReplayDatasetInput,
)
from ..contracts_retrieval_replay import RetrievalR3ReplayDatasetInput
from ..db import locked
from ..errors import DomainError, LeaseLost
from ..models import (
    AgentRun,
    EvalDataset,
    EvolutionCandidate,
    EvolutionEvaluation,
    Experience,
    ExternalRequest,
    Feedback,
    Idempotency,
    ModelReceipt,
    PolicyVersion,
    Principal,
    ResearchNode,
    SearchObservation,
    SourceSet,
)
from ..models_learning_ext import EvaluationDatasetAttestation, PolicyReplayManifest
from ..util import fingerprint, uid
from .answers import AnswerService
from .dependencies import check_closure, depend, register
from .evolution_feedback import policy_feedback_inputs
from .evolution_retrieval_r3 import (
    R3_METRICS,
    R3_SCOPE,
    check_r3_admission,
    check_r3_qualification,
    check_r3_terminal_report,
    check_r3_trace,
    is_r3,
    r3_execution_contract,
    r3_input_hash,
    r3_trace_binding,
)
from .evolution_typed import (
    TYPED_RELEASE_SCOPE,
    check_typed_case,
    check_typed_execution_trace,
    check_typed_retrieval_release,
    execute_typed_case,
    register_typed_dataset,
    score_typed_execution,
    typed_execution_contract,
)
from .learning_ext import _no_independent_assistance
from .policies import render_teaching_skill
from .retrieval_context import RETRIEVAL_CONTEXT_CONTRACT
from .runtime import leased
from .security import namespace_access, namespace_admin, private_namespace

REPLAY_VERSION = "frozen-execution-replay-1.6.6"
RELEASE_SCOPE = "frozen_tutor_outputs_and_guard_regressions_not_human_learning_effectiveness"
RESEARCH_RELEASE_SCOPE = "actual_query_generation_on_frozen_observations_not_live_search_quality"


DEVELOPMENT_PROBE_CONTRACT = "development-replay-probe-v1"


class BudgetedReplayGateway(ModelGateway):
    """Same production gateway with a durable, equal per-arm call ceiling.

    Counting receipt rows (not a process-local counter) also binds restart/replay.
    Stage keys must belong to the frozen arm. Existing receipts may be replayed
    after the ceiling is reached, but a new paid attempt cannot borrow the other
    arm's budget.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        run_id: str,
        prefix: str,
        cap: int,
        target: str = "teaching_skill",
    ):
        super().__init__(gateway.runtime, gateway.provider, gateway.settings)
        self.replay_run_id, self.prefix, self.arm_cap = run_id, prefix, cap
        self.target = target
        self.allowed_roles = {
            "research_heuristic": {"planner", "reflector"},
            "retrieval_recipe": {"embedding", "reranker", "query_rewriter"},
            "routing_policy": {"coordinator"},
            "teaching_skill": {"tutor", "answer_checker"},
        }[target]

    async def call(
        self,
        run_id: str,
        epoch: int,
        worker: str,
        key: str,
        role: str,
        system: str,
        context: dict[str, Any],
        output: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        data_classes: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        access_dependencies: list[str] | None = None,
        replay_only: bool = False,
    ) -> ModelOutput:
        if self.target == "routing_policy" and key == "intent":
            key = self.prefix + key
        if (
            run_id != self.replay_run_id
            or not key.startswith(self.prefix)
            or role not in self.allowed_roles
            or tools
        ):
            raise DomainError(
                "REPLAY_CAPABILITY",
                "回放只允许指定臂中的 Tutor 和独立 Answer Guard，不开放工具。",
                403,
            )
        with self.runtime.db.read() as db:
            frozen_run = db.get(AgentRun, run_id)
            if frozen_run is None:
                raise LeaseLost()
            frozen = frozen_run.input_manifest
            if "policy_dependencies" in frozen:
                access_dependencies = sorted(
                    set(access_dependencies or []) | set(frozen["policy_dependencies"])
                )
                data_classes = sorted(
                    set(data_classes or ["public_source"])
                    | {"learning_state"}
                    | set(frozen["policy_data_classes"])
                )
        result = await super().call(
            run_id,
            epoch,
            worker,
            key,
            role,
            system,
            context,
            output,
            tools,
            data_classes,
            messages,
            output_schema,
            access_dependencies,
            replay_only,
        )
        if frozen.get("evolution_graph"):
            from .evolution_stages import record_model_authority

            record_model_authority(
                self.runtime, run_id, epoch, worker, key, role, data_classes, access_dependencies
            )
        return result

    def remaining_calls_in(self, db, run: AgentRun) -> int:
        used = sum(
            receipt.stage_key.startswith(self.prefix)
            for receipt in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
        )
        return min(super().remaining_calls_in(db, run), max(0, self.arm_cap - used))

    def _check_attempt_budget(self, db, run: AgentRun, key: str) -> None:
        # ModelGateway calls this under its funding/run reservation lock. Separate
        # preflight checks let concurrent distinct stages both pass the arm cap.
        arm = [
            receipt
            for receipt in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
            if receipt.stage_key.startswith(self.prefix)
        ]
        if len(arm) >= self.arm_cap:
            raise DomainError("REPLAY_ARM_BUDGET", "该对照臂已达到冻结的调用上限。")


def engine_versions() -> dict[str, str]:
    from importlib import import_module

    from .. import (
        contracts,
        contracts_answer_completion_v166,
        contracts_learning_ext,
        contracts_retrieval_replay,
        contracts_session_plan_v166,
        contracts_session_progress_v166,
        contracts_session_tutor_v166,
        contracts_streaming_ext,
    )
    from . import (
        answer_completion,
        answer_continuity,
        bounded_evidence,
        checker_evidence,
        consumer_context,
        conversations,
        dependencies,
        evolution,
        evolution_feedback,
        evolution_retrieval_r3,
        evolution_typed,
        explicit_source_paths,
        knowledge,
        learning,
        learning_events,
        learning_ext,
        orchestration,
        ordinary_checker,
        policies,
        policy_inputs,
        privacy,
        public_read_authority,
        retrieval,
        retrieval_context,
        retrieval_relevance,
        rollouts,
        runtime,
        security,
        session_plans,
        session_progress,
        session_tutor,
        sources,
        taxonomy,
    )
    from .research_policy import execute_query_replay

    try:
        relevance = import_module("careerforge.services.answer_relevance")
        relevance_source = inspect.getsource(relevance)
    except ModuleNotFoundError as exc:
        if exc.name != "careerforge.services.answer_relevance":
            raise
        relevance_source = "not_available"
    return {
        "evaluator": REPLAY_VERSION,
        "session_tutor_implementation": fingerprint(
            inspect.getsource(session_tutor)
            + inspect.getsource(learning)
            + inspect.getsource(learning_ext)
            + inspect.getsource(session_plans)
            + inspect.getsource(learning_events)
            + inspect.getsource(contracts_session_tutor_v166)
            + inspect.getsource(contracts_session_plan_v166)
            + inspect.getsource(contracts_session_progress_v166)
            + inspect.getsource(session_progress)
        ),
        "answer_relevance_implementation": fingerprint(relevance_source),
        "ordinary_checker_implementation": fingerprint(
            ordinary_checker.ordinary_checker_identity()
        ),
        "checker_evidence_implementation": fingerprint(
            checker_evidence.checker_evidence_identity()
        ),
        "typed_contracts_implementation": fingerprint(
            inspect.getsource(contracts)
            + inspect.getsource(contracts_streaming_ext)
            + inspect.getsource(contracts_learning_ext)
            + inspect.getsource(contracts_retrieval_replay)
            + inspect.getsource(contracts_answer_completion_v166)
        ),
        "policy_evaluator_implementation": fingerprint(inspect.getsource(evolution)),
        "replay_evaluator_implementation": fingerprint(inspect.getsource(import_module(__name__))),
        "gateway_implementation": fingerprint(
            inspect.getsource(ModelGateway) + inspect.getsource(BudgetedReplayGateway)
        ),
        "feedback_lineage_implementation": fingerprint(inspect.getsource(evolution_feedback)),
        "policy_input_implementation": fingerprint(
            inspect.getsource(policy_inputs) + inspect.getsource(policies)
        ),
        "answer_guard_implementation": fingerprint(inspect.getsource(AnswerService)),
        "answer_completion_implementation": fingerprint(inspect.getsource(answer_completion)),
        "answer_continuity_implementation": fingerprint(
            inspect.getsource(answer_continuity)
            + inspect.getsource(conversations)
            + inspect.getsource(rollouts)
        ),
        "template_renderer_implementation": fingerprint(inspect.getsource(render_teaching_skill)),
        "research_replay_implementation": fingerprint(inspect.getsource(execute_query_replay)),
        "coordinator_implementation": fingerprint(
            inspect.getsource(orchestration) + inspect.getsource(taxonomy)
        ),
        "retrieval_implementation": fingerprint(inspect.getsource(retrieval)),
        "retrieval_context_implementation": fingerprint(inspect.getsource(retrieval_context)),
        "retrieval_relevance_implementation": fingerprint(inspect.getsource(retrieval_relevance)),
        "admitted_retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
        "typed_replay_implementation": fingerprint(inspect.getsource(evolution_typed)),
        "typed_execution_contract": fingerprint(typed_execution_contract()),
        "r3_execution_contract": fingerprint(r3_execution_contract()),
        "r3_replay_implementation": fingerprint(inspect.getsource(evolution_retrieval_r3)),
        "bounded_evidence_implementation": fingerprint(inspect.getsource(bounded_evidence)),
        "retrieval_authority_implementation": fingerprint(
            "".join(
                inspect.getsource(module)
                for module in (
                    consumer_context,
                    dependencies,
                    explicit_source_paths,
                    knowledge,
                    privacy,
                    public_read_authority,
                    runtime,
                    security,
                    sources,
                )
            )
        ),
        "knowledge_contract": "publication_pack_with_current_authority_and_frozen_versions",
        "index": "typed_snapshot_pins_real_lexical_and_vector_hashes_other_targets_use_frozen_inputs",
        "embedding": "explicit_profile_dimensions_and_budget_when_retrieval_uses_vectors",
        "tool_capabilities": "no_tools_no_execution_no_publication",
    }


def independence_groups(cases: list[dict[str, Any]]) -> dict[str, str]:
    """Conservatively cluster duplicates across repo families AND question groups."""
    groups = [{c["case_id"]} for c in cases]
    keys = {
        c["case_id"]: {
            "group:" + c["group_id"],
            *(["repo:" + c["repository_family"]] if c["repository_family"] else []),
            "user-question:" + c["user_question_group"],
            "question:" + fingerprint(c["question"].strip().casefold()),
            *("repo:" + str(r) for r in c.get("repository_families", [])),
        }
        for c in cases
    }
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            left = set().union(*(keys[k] for k in groups[i]))
            match = next(
                (
                    j
                    for j in range(i + 1, len(groups))
                    if left & set().union(*(keys[k] for k in groups[j]))
                ),
                None,
            )
            if match is not None:
                groups[i] |= groups.pop(match)
                merged = True
                break
    return {
        case: "cluster:" + fingerprint(sorted(group))[:24] for group in groups for case in group
    }


class EvolutionReplay:
    app: Any
    db: Any

    def _episodes(
        self, db: Session, actor: str, namespace_id: str, ids: list[str]
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented by EvolutionService")

    def _environment_hash(
        self, db: Session, actor: str, base: PolicyVersion, candidate: EvolutionCandidate
    ) -> str:
        raise NotImplementedError("Implemented by EvolutionService")

    def register_typed_replay_dataset(
        self, actor: str, value: TypedReplayDatasetInput
    ) -> dict[str, Any]:
        return register_typed_dataset(self, actor, value)

    def register_r3_retrieval_dataset(
        self, actor: str, value: RetrievalR3ReplayDatasetInput
    ) -> dict[str, Any]:
        return register_typed_dataset(self, actor, value)

    def register_replay_dataset(self, actor: str, value: ReplayDatasetInput) -> dict[str, Any]:
        with self.db.transaction() as db:
            ns = namespace_admin(db, actor, value.namespace_id)
            if ns.kind != "private":
                raise DomainError(
                    "REPLAY_PRIVATE", "含学习问题的回放只允许在已授权私人域保存。", 403
                )
            if value.provenance == "development_probe" and (
                self.db.settings.mode not in {"test", "development"} or value.split != "development"
            ):
                raise DomainError(
                    "DEVELOPMENT_PROBE_SCOPE",
                    "开发联调回放仅允许开发环境与 development 划分。",
                    422,
                )
            if self.db.settings.mode != "test" and value.provenance == "fixture":
                raise DomainError("FIXTURE_NOT_GOLD", "生产回放不能登记合成验收数据。", 422)
            if len({c.case_id for c in value.cases}) != len(value.cases):
                raise DomainError("DATASET_DUPLICATE", "评测样例身份重复。", 422)
            if value.provenance == "human_gold":
                if len(set(value.annotators)) < 2 or any(
                    not db.get(Principal, a) for a in value.annotators
                ):
                    raise DomainError(
                        "ANNOTATORS", "人工 Gold 需要至少两名可独立认证的标注者。", 422
                    )
            if value.split == "holdout" and value.provenance == "human_gold":
                calibration = (
                    db.get(EvolutionEvaluation, value.thresholds.baseline_calibration_id)
                    if value.thresholds.baseline_calibration_id
                    else None
                )
                calibration_dataset = (
                    db.get(EvalDataset, calibration.dataset_id) if calibration else None
                )
                if (
                    not calibration
                    or not calibration_dataset
                    or calibration_dataset.namespace_id != ns.id
                    or calibration_dataset.split != "development"
                    or calibration_dataset.threshold_policy.get("target") != "teaching_skill"
                    or calibration.report.get("replay_version") != REPLAY_VERSION
                ):
                    raise DomainError(
                        "BASELINE_CALIBRATION_REQUIRED",
                        "正式门槛须引用先前开发集实测基线，不能凭空设定。",
                        422,
                    )
                if value.thresholds.min_cases < 20 or value.thresholds.min_groups < 10:
                    raise DomainError(
                        "GOLD_SAMPLE_POLICY", "正式门禁至少20例/10独立组；样本量仍非效果保证。", 422
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
                    self.app.knowledge.publication_pack(db, actor, p) for p in item.publication_ids
                ]
                facts = {f["fact_atom_id"] for u in units for f in u["facts"]}
                if not set(item.expected.required_fact_ids) <= facts:
                    raise DomainError(
                        "GOLD_FACT_SCOPE", "Gold 所需事实必须位于所选冻结知识包。", 422
                    )
                real_families = {
                    db.get(SourceSet, u["source_id"]).metadata_json.get(
                        "canonical_repository_family", u["repository"]
                    )
                    for u in units
                }
                if real_families != {item.repository_family}:
                    raise DomainError(
                        "GOLD_REPOSITORY_GROUP",
                        "仓库家族必须与实际来源身份一致，不能改名制造独立样本。",
                        422,
                    )
                # Preserve exact evidence for both arms; optional facets cannot silently drift.
                case["frozen_units"] = units
                cases.append(case)
            policy = {
                **value.thresholds.model_dump(mode="json"),
                "kind": "teaching_execution_replay",
                "target": "teaching_skill",
                "annotators": sorted(set(value.annotators)),
                "max_safety_violations": 0,
                "confidence": 0.95,
                "replay_version": REPLAY_VERSION,
                "declared_split": value.split,
                **(
                    {"development_probe_contract": DEVELOPMENT_PROBE_CONTRACT}
                    if value.provenance == "development_probe"
                    else {}
                ),
            }
            dataset = EvalDataset(
                namespace_id=ns.id,
                name=value.name,
                version=value.version,
                provenance=value.provenance,
                frozen_hash=fingerprint({"cases": cases, "policy": policy}),
                cases=cases,
                split=value.split,
                threshold_policy=policy,
            )
            db.add(dataset)
            db.flush()
            return {
                "dataset_id": dataset.id,
                "frozen_hash": dataset.frozen_hash,
                "split": dataset.split,
                "independent_groups": len(set(independence_groups(cases).values())),
                "requires_individual_attestations": value.provenance == "human_gold",
                "external_acceptance_blocker": "human_gold_attestations_and_real_learner_effectiveness_not_established"
                if value.provenance == "human_gold"
                else "fixture_process_test_not_real_gold",
            }

    def attest_dataset(
        self, actor: str, dataset_id: str, value: DatasetAttestationInput
    ) -> dict[str, Any]:
        with self.db.transaction() as db:
            dataset = locked(db, EvalDataset, dataset_id)
            if not dataset:
                raise DomainError("NOT_FOUND", "数据集不存在。", 404)
            namespace_access(db, actor, dataset.namespace_id, True)
            if (
                actor not in dataset.threshold_policy.get("annotators", [])
                or dataset.provenance != "human_gold"
            ):
                raise DomainError("ANNOTATOR_SCOPE", "当前身份不是本数据集的独立标注者。", 403)
            if value.frozen_hash != dataset.frozen_hash or dataset.frozen_hash != fingerprint(
                {"cases": dataset.cases, "policy": dataset.threshold_policy}
            ):
                raise DomainError("DATASET_HASH", "标注确认不适用于当前冻结数据集。")
            if db.scalar(
                select(EvaluationDatasetAttestation.id).where(
                    EvaluationDatasetAttestation.dataset_id == dataset.id,
                    EvaluationDatasetAttestation.principal_id == actor,
                )
            ):
                raise DomainError("ATTESTATION_IMMUTABLE", "标注确认不可覆盖；修改 Gold 需新版本。")
            row = EvaluationDatasetAttestation(
                dataset_id=dataset.id,
                principal_id=actor,
                frozen_hash=dataset.frozen_hash,
                decision=value.decision,
                notes=value.notes,
            )
            db.add(row)
            db.flush()
            return {"attestation_id": row.id, "dataset_id": dataset.id, "decision": row.decision}

    def _attestations(self, db: Session, dataset: EvalDataset) -> list[dict[str, str]]:
        rows = db.scalars(
            select(EvaluationDatasetAttestation).where(
                EvaluationDatasetAttestation.dataset_id == dataset.id
            )
        ).all()
        return [
            {"principal_id": r.principal_id, "frozen_hash": r.frozen_hash, "decision": r.decision}
            for r in sorted(rows, key=lambda r: r.principal_id)
        ]

    def _dataset(self, db: Session, actor: str, identifier: str) -> EvalDataset:
        row = db.get(EvalDataset, identifier)
        if not row:
            raise DomainError("NOT_FOUND", "回放数据集不存在。", 404)
        namespace_admin(db, actor, row.namespace_id)
        if row.threshold_policy.get("kind") not in {
            "teaching_execution_replay",
            "research_execution_replay",
            "typed_execution_replay",
        } or row.frozen_hash != fingerprint({"cases": row.cases, "policy": row.threshold_policy}):
            raise DomainError("REPLAY_DATASET", "数据集不是完整且冻结的教学执行回放集。", 422)
        if row.split != row.threshold_policy.get("declared_split"):
            raise DomainError("REPLAY_SPLIT_CHANGED", "数据集划分已经变化。")
        probe = row.threshold_policy.get("development_probe_contract")
        if probe is not None or row.provenance == "development_probe":
            if (
                probe != DEVELOPMENT_PROBE_CONTRACT
                or row.provenance != "development_probe"
                or row.split != "development"
            ):
                raise DomainError(
                    "DEVELOPMENT_PROBE_SCOPE", "开发联调数据不得改写来源或升级为独立验收。", 422
                )
        return row

    def _assert_independence(
        self, db: Session, actor: str, candidate: EvolutionCandidate, dataset: EvalDataset
    ) -> None:
        self._episodes(db, actor, candidate.namespace_id, candidate.experience_ids)
        episodes, _, _ = policy_feedback_inputs(
            db, actor, candidate.namespace_id, candidate.experience_ids, candidate.base_policy_id
        )
        if dataset.provenance == "development_probe":
            if (
                self.db.settings.mode not in {"test", "development"}
                or dataset.split != "development"
            ):
                raise DomainError("DEVELOPMENT_PROBE_SCOPE", "开发回放不可作为独立验收。", 422)
            return
        holdout = dataset.cases
        checks = [
            ({e["group_id"] for e in episodes}, {c["group_id"] for c in holdout}),
            (
                {e.get("user_question_group", e["group_id"]) for e in episodes},
                {c["user_question_group"] for c in holdout},
            ),
            (
                {f for e in episodes for f in e.get("repository_families", [])},
                {
                    f
                    for c in holdout
                    for f in c.get("repository_families", [c["repository_family"]])
                    if f
                },
            ),
            (
                {e.get("time_partition") for e in episodes} - {None},
                {c["time_partition"] for c in holdout},
            ),
            (
                {e.get("question_hash") for e in episodes} - {None},
                {fingerprint(c["question"].strip().casefold()) for c in holdout},
            ),
        ]
        if any(left & right for left, right in checks):
            raise DomainError(
                "HOLDOUT_LEAKAGE", "优化经验与回放集在仓库、用户/问题或时间分区发生泄露。", 422
            )
        if dataset.split == "holdout":
            # A candidate's previously observed development cases cannot become its holdout
            # under a new dataset name or a different hand-written group ID.
            evaluations = db.scalars(
                select(EvolutionEvaluation).where(EvolutionEvaluation.candidate_id == candidate.id)
            ).all()
            for evaluation in evaluations:
                previous = db.get(EvalDataset, evaluation.dataset_id)
                if not previous or previous.split != "development":
                    continue
                for field in ["repository_family", "user_question_group", "time_partition"]:
                    if {c.get(field) for c in previous.cases if c.get(field)} & {
                        c[field] for c in holdout if c[field]
                    }:
                        raise DomainError(
                            "HOLDOUT_LEAKAGE", "开发集与独立验收集存在组或时间污染。", 422
                        )
                if {
                    f
                    for c in previous.cases
                    for f in c.get("repository_families", [c.get("repository_family")])
                    if f
                } & {
                    f
                    for c in holdout
                    for f in c.get("repository_families", [c["repository_family"]])
                    if f
                }:
                    raise DomainError("HOLDOUT_LEAKAGE", "开发集与验收集存在重叠仓库家族。", 422)
                if {fingerprint(c["question"].strip().casefold()) for c in previous.cases} & {
                    fingerprint(c["question"].strip().casefold()) for c in holdout
                }:
                    raise DomainError("HOLDOUT_LEAKAGE", "开发问题不能改名后用于独立验收。", 422)

    def start_replay(self, actor: str, candidate_id: str, value: ReplayInput) -> dict[str, Any]:
        with self.db.transaction() as db:
            return self.start_replay_in(db, actor, candidate_id, value)

    def start_replay_in(self, db, actor, candidate_id, value, *, graph_binding=None):
        request = {"candidate_id": candidate_id, **value.model_dump(mode="json")}
        if graph_binding is not None:
            request["evolution_graph"] = graph_binding
        request_hash = fingerprint(request)
        locked(db, Principal, actor)
        private_namespace(db, actor)
        previous = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == "evolution.replay",
                Idempotency.key == value.idempotency_key,
            )
        )
        if previous:
            if previous.request_hash != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT", "回放请求键对应不同内容。")
            return {**previous.result, "replayed": True}
        _no_independent_assistance(db, actor)
        candidate = db.get(EvolutionCandidate, candidate_id)
        if not candidate:
            raise DomainError("NOT_FOUND", "候选不存在。", 404)
        namespace_admin(db, actor, candidate.namespace_id)
        dataset = self._dataset(db, actor, value.dataset_id)
        if (
            candidate.target != dataset.threshold_policy.get("target")
            or candidate.target
            not in {
                "teaching_skill",
                "research_heuristic",
                "routing_policy",
                "retrieval_recipe",
            }
            or candidate.status not in {"proposed", "evaluated"}
            or dataset.namespace_id != candidate.namespace_id
        ):
            raise DomainError("REPLAY_SCOPE", "回放只针对同域、同类未发布候选。", 422)
        self._assert_independence(db, actor, candidate, dataset)
        baseline = db.get(PolicyVersion, candidate.base_policy_id)
        if (
            not baseline
            or baseline.namespace_id != candidate.namespace_id
            or baseline.target != candidate.target
            or baseline.content_hash != fingerprint(baseline.content)
            or candidate.content_hash != fingerprint(candidate.content)
        ):
            raise DomainError("POLICY_HASH", "候选或 Champion 哈希不一致。")
        attestations = self._attestations(db, dataset)
        if is_r3(dataset.threshold_policy.get("typed_execution_contract")):
            check_r3_qualification(self, db, actor, dataset, candidate)
        if dataset.provenance == "human_gold":
            valid = {
                a["principal_id"]
                for a in attestations
                if a["frozen_hash"] == dataset.frozen_hash and a["decision"] == "approve"
            }
            training_authors = {
                f.principal_id
                for e in db.scalars(
                    select(Experience).where(Experience.id.in_(candidate.experience_ids))
                )
                if (f := db.get(Feedback, e.feedback_id)) is not None
            }
            if len(valid - training_authors) < 2 or any(
                a["decision"] == "reject" for a in attestations
            ):
                raise DomainError(
                    "GOLD_ATTESTATIONS_REQUIRED",
                    "人工 Gold 缺少两名独立认证或存在未解决异议。",
                    422,
                )
            calibration_id = dataset.threshold_policy.get("baseline_calibration_id")
            if dataset.split == "holdout":
                calibration = (
                    db.get(EvolutionEvaluation, calibration_id) if calibration_id else None
                )
                if (
                    not calibration
                    or calibration.base_hash != baseline.content_hash
                    or calibration.report.get("target") != candidate.target
                ):
                    raise DomainError(
                        "BASELINE_CALIBRATION_REQUIRED",
                        "冻结门槛不是针对本次 Champion 的实测基线。",
                        422,
                    )
        total = len(dataset.cases) * 2 * value.per_arm_call_cap
        if total > 100:
            raise DomainError(
                "REPLAY_BUDGET",
                "冻结完整配对评测所需额度超过单任务100次，请调整样例或每臂上限。",
                422,
            )
        ids = list(dict.fromkeys(p for c in dataset.cases for p in c.get("publication_ids", [])))
        selection_id = uid()
        selection_payload = {
            "purpose": "frozen_champion_candidate_replay",
            "publication_ids": ids,
            "dataset_hash": dataset.frozen_hash,
        }
        for case in dataset.cases:
            for pack in case.get("frozen_units", []):
                if fingerprint(
                    self.app.knowledge.publication_pack(db, actor, pack["publication_id"])
                ) != fingerprint(pack):
                    raise DomainError(
                        "REPLAY_EVIDENCE_CHANGED", "Gold 固定证据已变化，不能悄悄换版。"
                    )
            if candidate.target == "research_heuristic":
                self._check_research_corpus(db, actor, candidate.namespace_id, case)
            if candidate.target in {"routing_policy", "retrieval_recipe"}:
                check_typed_case(
                    self,
                    db,
                    actor,
                    case,
                    candidate.target,
                    dataset.threshold_policy.get("typed_execution_contract"),
                )
        frozen = {
            "target": candidate.target,
            "candidate_id": candidate.id,
            "candidate_hash": candidate.content_hash,
            "base_id": baseline.id,
            "base_hash": baseline.content_hash,
            "before": baseline.content,
            "after": candidate.content,
            "dataset_id": dataset.id,
            "dataset_hash": dataset.frozen_hash,
            "dataset_provenance": dataset.provenance,
            "dataset_split": dataset.split,
            "cases": dataset.cases,
            "threshold_policy": dataset.threshold_policy,
            "attestations": attestations,
            "selection_manifest_id": selection_id,
            "model_profile": self.app.provider.profile_version,
            "environment_hash": self._environment_hash(db, actor, baseline, candidate),
            "engine_versions": engine_versions(),
            "per_arm_call_cap": value.per_arm_call_cap,
            "per_arm_request_cap": value.per_arm_request_cap,
            "max_research_nodes": value.max_research_nodes,
            "max_research_depth": value.max_research_depth,
            "total_call_cap": total,
            "same_input_and_authorization": True,
            "prompt_version": "answer-service-per-block-frozen-renderer",
        }
        _, frozen["feedback_witnesses"], policy_dependencies = policy_feedback_inputs(
            db,
            actor,
            candidate.namespace_id,
            candidate.experience_ids,
            candidate.base_policy_id,
        )
        policy_closure = check_closure(db, actor, policy_dependencies)
        frozen["policy_dependencies"] = policy_dependencies
        frozen["policy_data_classes"] = policy_closure.source_data_classes
        if candidate.target == "teaching_skill":
            frozen["answer_contract"] = "atomic-answer-1.6.1"
        if candidate.target in {"routing_policy", "retrieval_recipe"}:
            frozen["typed_execution_contract"] = typed_execution_contract(
                dataset.threshold_policy.get("typed_execution_contract")
            )
        if is_r3(frozen.get("typed_execution_contract")):
            frozen["r3_runtime_admission_key"] = "evolution-replay:" + fingerprint(
                value.idempotency_key
            )
        if graph_binding is not None:
            from .evolution_privacy import freeze_generations_in

            frozen["evolution_graph"] = {
                **graph_binding,
                "privacy_generations": freeze_generations_in(
                    self.app, db, actor, candidate.namespace_id, frozen
                ),
            }
            from ..models_dialogue import WorkflowEvolutionRequest

            request_row = db.get(WorkflowEvolutionRequest, graph_binding["request_id"])
            if request_row is None or request_row.admission_manifest_hash is not None:
                raise DomainError("EVOLUTION_GRAPH_BINDING", "回放请求已冻结任务。", 409)
            request_row.admission_manifest_hash = fingerprint(frozen)
        run = self.app.runtime.create_in(
            db,
            actor,
            candidate.namespace_id,
            value.funding_id,
            "evolution_replay",
            frozen,
            total,
            "evolution-replay:" + fingerprint(value.idempotency_key),
            minutes=120,
        )
        selection = register(
            db,
            actor,
            candidate.namespace_id,
            "selection_manifest",
            selection_payload,
            selection_id,
        )
        for identifier in ids:
            depend(db, actor, selection.id, identifier, "selects_source")
        row = PolicyReplayManifest(
            run_id=run.id,
            candidate_id=candidate.id,
            dataset_id=dataset.id,
            frozen_hash=fingerprint(frozen),
            frozen_input=frozen,
        )
        db.add(row)
        db.flush()
        result = {"run_id": run.id, "replay_id": row.id, "call_cap": total, "published": False}
        db.add(
            Idempotency(
                principal_id=actor,
                operation="evolution.replay",
                key=value.idempotency_key,
                request_hash=request_hash,
                result=result,
            )
        )
        return result

    def _validate_replay(
        self, db: Session, actor: str, frozen: dict[str, Any]
    ) -> tuple[EvolutionCandidate, EvalDataset, PolicyVersion]:
        _no_independent_assistance(db, actor)
        if frozen.get("evolution_graph"):
            from .evolution_stages import validate_graph_identity

            validate_graph_identity(frozen)
        candidate = db.get(EvolutionCandidate, frozen["candidate_id"])
        dataset = self._dataset(db, actor, frozen["dataset_id"])
        baseline = db.get(PolicyVersion, frozen["base_id"])
        if not candidate or not baseline:
            raise DomainError("REPLAY_INPUT_CHANGED", "回放基线或候选缺失。")
        self._assert_independence(db, actor, candidate, dataset)
        if is_r3(frozen.get("typed_execution_contract")):
            check_r3_qualification(self, db, actor, dataset, candidate)
        if frozen["target"] in {"routing_policy", "retrieval_recipe"} and (
            typed_execution_contract(frozen.get("typed_execution_contract"))
            != typed_execution_contract(dataset.threshold_policy.get("typed_execution_contract"))
        ):
            raise DomainError("REPLAY_INPUT_CHANGED", "固定回放的执行/评分合同已改变。")
        if (
            candidate.status not in {"proposed", "evaluated"}
            or candidate.content_hash != frozen["candidate_hash"]
            or fingerprint(candidate.content) != frozen["candidate_hash"]
            or baseline.content_hash != frozen["base_hash"]
            or fingerprint(baseline.content) != frozen["base_hash"]
            or candidate.base_policy_id != baseline.id
            or baseline.namespace_id != candidate.namespace_id
            or baseline.target != candidate.target
            or candidate.target != frozen["target"]
            or fingerprint(frozen["before"]) != frozen["base_hash"]
            or fingerprint(frozen["after"]) != frozen["candidate_hash"]
            or dataset.frozen_hash != frozen["dataset_hash"]
            or dataset.cases != frozen["cases"]
            or dataset.threshold_policy != frozen["threshold_policy"]
            or dataset.provenance != frozen["dataset_provenance"]
            or dataset.split != frozen["dataset_split"]
            or candidate.namespace_id != dataset.namespace_id
            or self.app.provider.profile_version != frozen["model_profile"]
            or self._environment_hash(db, actor, baseline, candidate) != frozen["environment_hash"]
            or engine_versions() != frozen["engine_versions"]
            or self._attestations(db, dataset) != frozen["attestations"]
        ):
            raise DomainError(
                "REPLAY_INPUT_CHANGED", "评测期间模型、权限、来源、候选或 Gold 发生变化。"
            )
        check_closure(db, actor, [frozen["selection_manifest_id"]])
        if is_r3(frozen.get("typed_execution_contract")):
            check_r3_admission(db, actor, frozen)
        for case in frozen["cases"]:
            for pack in case.get("frozen_units", []):
                if fingerprint(
                    self.app.knowledge.publication_pack(db, actor, pack["publication_id"])
                ) != fingerprint(pack):
                    raise DomainError("REPLAY_EVIDENCE_CHANGED", "回放证据已失效或变化。")
            if frozen["target"] == "research_heuristic":
                self._check_research_corpus(db, actor, candidate.namespace_id, case)
            if frozen["target"] in {"routing_policy", "retrieval_recipe"}:
                check_typed_case(
                    self, db, actor, case, frozen["target"], frozen.get("typed_execution_contract")
                )
        return candidate, dataset, baseline

    async def execute_replay(self, run: Any, epoch: int, worker: str) -> None:

        frozen = run.input_manifest
        if self.app.runtime.stage(run.id, "result"):
            if is_r3(frozen.get("typed_execution_contract")):
                with self.db.read() as db:
                    self._validate_replay(db, run.principal_id, frozen)
                    manifest = db.scalar(
                        select(PolicyReplayManifest).where(PolicyReplayManifest.run_id == run.id)
                    )
                    evaluation = (
                        db.get(EvolutionEvaluation, manifest.evaluation_id) if manifest else None
                    )
                    if manifest is None or evaluation is None:
                        raise DomainError(
                            "R3_TERMINAL_BINDING", "r3 终态缺少原子评测与回放身份。", 422
                        )
                    check_r3_terminal_report(db, manifest, evaluation)
            return
        with self.db.transaction() as db:
            leased(db, run.id, epoch, worker)
            self._validate_replay(db, run.principal_id, frozen)
            saved = db.scalar(
                select(PolicyReplayManifest).where(PolicyReplayManifest.run_id == run.id)
            )
            if (
                not saved
                or saved.frozen_hash != fingerprint(frozen)
                or saved.frozen_input != frozen
            ):
                raise DomainError("REPLAY_MANIFEST_HASH", "回放 manifest 完整性校验失败。")
        traces = []
        samples = []
        clusters = independence_groups(frozen["cases"])
        for index, case in enumerate(frozen["cases"]):
            pair = {}
            # Counterbalance order rather than always letting one arm warm the stack.
            arms = ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"]
            for arm in arms:
                key = f"replay:{index}:{arm}:"
                prior = self.app.runtime.stage(run.id, key + "result")
                if prior is not None:
                    trace = prior
                    if frozen["target"] in {"routing_policy", "retrieval_recipe"}:
                        check_typed_execution_trace(
                            trace, frozen["target"], frozen.get("typed_execution_contract")
                        )
                    if is_r3(frozen.get("typed_execution_contract")):
                        with self.db.read() as db:
                            check_r3_trace(
                                self, db, run.principal_id, frozen, run.id, index, case, trace
                            )
                else:
                    execution = await self.execute_replay_arm(run, epoch, worker, index, arm)
                    trace = self.score_replay_arm(run, index, arm, execution)
                    self.app.runtime.save_stage(run.id, epoch, worker, key + "result", trace)
                pair[arm] = trace
                traces.append(trace)
            baseline, candidate = pair["baseline"], pair["candidate"]
            if baseline["frozen_input_hash"] != candidate["frozen_input_hash"]:
                raise DomainError("REPLAY_NOT_PAIRED", "两臂输入不一致，禁止形成可发布结论。")
            samples.append(
                {
                    "case_id": case["case_id"],
                    "group_id": clusters[case["case_id"]],
                    "baseline": baseline["metrics"]["score"],
                    "candidate": candidate["metrics"]["score"],
                    "safety_violations": baseline["metrics"]["hard_gate_violations"]
                    + candidate["metrics"]["hard_gate_violations"],
                    "baseline_metrics": baseline["metrics"],
                    "candidate_metrics": candidate["metrics"],
                }
            )
        report = self.build_replay_report(run, traces, samples)
        self.commit_replay_evaluation(run, epoch, worker, traces, report)

    async def execute_replay_arm(self, run, epoch, worker, index, arm):
        frozen = run.input_manifest
        case = frozen["cases"][index]
        key = f"replay:{index}:{arm}:"
        with self.db.transaction() as db:
            leased(db, run.id, epoch, worker)
            self._validate_replay(db, run.principal_id, frozen)
            old_receipts = [
                r
                for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
                if r.stage_key.startswith(key)
            ]
        gateway = BudgetedReplayGateway(
            self.app.gateway, run.id, key, frozen["per_arm_call_cap"], frozen["target"]
        )
        answers = AnswerService(self.db, self.app.runtime, gateway, self.app.knowledge)
        content = frozen["before"] if arm == "baseline" else frozen["after"]
        policy_hash = frozen["base_hash"] if arm == "baseline" else frozen["candidate_hash"]
        policies = {"teaching_skill": {"content": content, "hash": policy_hash}}
        evidence = {
            "knowledge_units": case.get("frozen_units", []),
            "selection_manifest_id": frozen["selection_manifest_id"],
            "retrieval_manifest_id": frozen["selection_manifest_id"],
            "uncovered_questions": [],
            "source_observations": [],
            "task_type": "explain_implementation",
            "total_project_groups": len({u["source_id"] for u in case.get("frozen_units", [])}),
        }
        started = time.perf_counter()
        error = None
        blocks = []
        research_result: dict[str, Any] = {}
        typed_result: dict[str, Any] = {}
        try:
            if frozen["target"] == "research_heuristic":
                from .research_policy import execute_query_replay

                research_result = await execute_query_replay(
                    gateway,
                    (run.id, epoch, worker),
                    key,
                    content,
                    case,
                    model_cap=frozen["per_arm_call_cap"],
                    request_cap=frozen["per_arm_request_cap"],
                    max_nodes=frozen["max_research_nodes"],
                    max_depth=frozen["max_research_depth"],
                )
            elif frozen["target"] in {"routing_policy", "retrieval_recipe"}:
                typed_result = await execute_typed_case(
                    self,
                    gateway,
                    (run.id, epoch, worker),
                    key,
                    content,
                    policy_hash,
                    case,
                    frozen["target"],
                    run.principal_id,
                    execution_contract=frozen.get("typed_execution_contract"),
                )
            else:
                blocks = await answers.tutor(
                    run.principal_id,
                    case["question"],
                    evidence,
                    policies,
                    {"preferences": {}},
                    (run.id, epoch, worker),
                    step_prefix=key,
                )
        except LeaseLost:
            raise
        except DomainError as exc:
            if (
                frozen.get("evolution_graph")
                and exc.code in {"MODEL_RESULT_UNCERTAIN", "MODEL_REPLAY_ONLY"}
            ) or exc.code in {
                "EGRESS_OR_ROLE_DENIED",
                "FUNDING_UNAVAILABLE",
                "REPLAY_INPUT_CHANGED",
                "REPLAY_EVIDENCE_CHANGED",
                "REPLAY_INDEX_CHANGED",
            }:
                raise
            error = exc.code
        elapsed = (time.perf_counter() - started) * 1000
        with self.db.transaction() as db:
            leased(db, run.id, epoch, worker)
            self._validate_replay(db, run.principal_id, frozen)
            receipts = [
                r
                for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
                if r.stage_key.startswith(key)
            ]
        return {
            "blocks": blocks,
            "research_result": research_result,
            "typed_result": typed_result,
            "elapsed": elapsed,
            "latency_complete": not bool(old_receipts),
            "error": error,
            "receipt_ids": [r.id for r in receipts],
            "receipt_request_hashes": [r.request_hash for r in receipts],
            "receipt_response_hashes": [fingerprint(r.response) for r in receipts],
            "usage": [r.usage or {} for r in receipts],
            "model_calls": len(receipts),
            "evidence": evidence,
            "policy_hash": policy_hash,
        }

    def score_replay_arm(self, run, index, arm, execution):
        frozen = run.input_manifest
        case = frozen["cases"][index]
        blocks, research_result, typed_result, elapsed, error, evidence, policy_hash = (
            execution[k]
            for k in (
                "blocks",
                "research_result",
                "typed_result",
                "elapsed",
                "error",
                "evidence",
                "policy_hash",
            )
        )
        metrics = (
            self._score_research_replay(case, research_result, error)
            if frozen["target"] == "research_heuristic"
            else score_typed_execution(
                case,
                typed_result,
                error,
                frozen["target"],
                frozen.get("typed_execution_contract"),
            )
            if frozen["target"] in {"routing_policy", "retrieval_recipe"}
            else self._score_replay_output(case, blocks, error)
        )
        trace = {
            "case_id": case["case_id"],
            "arm": arm,
            "policy_hash": policy_hash,
            "frozen_input_hash": fingerprint(
                {
                    "question": case["question"],
                    "evidence": evidence,
                    "corpus": case.get("corpus", []),
                    "typed_snapshot": case.get("typed_snapshot"),
                    **(
                        {
                            "typed_execution_contract": typed_execution_contract(
                                frozen.get("typed_execution_contract")
                            )
                        }
                        if "typed_execution_contract" in frozen
                        else {}
                    ),
                }
            ),
            "blocks": blocks,
            "output_hash": fingerprint(blocks),
            "metrics": metrics,
            "research_result": research_result,
            "typed_result": typed_result,
            "latency_ms": elapsed,
            "latency_complete": execution["latency_complete"],
            "model_calls": execution["model_calls"],
            "model_call_cap": frozen["per_arm_call_cap"],
            "receipt_ids": execution["receipt_ids"],
            "receipt_request_hashes": execution["receipt_request_hashes"],
            "receipt_response_hashes": execution["receipt_response_hashes"],
            "usage": execution["usage"],
            "error_code": error,
            **(
                {
                    "typed_execution_contract": typed_execution_contract(
                        frozen.get("typed_execution_contract")
                    )
                }
                if frozen["target"] in {"routing_policy", "retrieval_recipe"}
                else {}
            ),
        }
        if is_r3(frozen.get("typed_execution_contract")):
            trace["frozen_input_hash"] = r3_input_hash(frozen, case)
            with self.db.read() as db:
                trace["r3_binding"] = r3_trace_binding(
                    self, db, run.principal_id, frozen, run.id, index, case, trace
                )
        if frozen["target"] in {"routing_policy", "retrieval_recipe"}:
            check_typed_execution_trace(
                trace, frozen["target"], frozen.get("typed_execution_contract")
            )
        return trace

    def build_replay_report(self, run, traces, samples):
        from .evolution import EVALUATOR_VERSION, paired_report

        frozen = run.input_manifest
        thresholds = frozen["threshold_policy"]
        report = paired_report(
            samples, thresholds["min_cases"], thresholds["min_groups"], thresholds
        )
        by_arm = {a: [t for t in traces if t["arm"] == a] for a in ["baseline", "candidate"]}
        latency_ratio = mean(t["latency_ms"] for t in by_arm["candidate"]) / max(
            1.0, mean(t["latency_ms"] for t in by_arm["baseline"])
        )
        call_ratio = sum(t["model_calls"] for t in by_arm["candidate"]) / max(
            1, sum(t["model_calls"] for t in by_arm["baseline"])
        )
        if any(
            t["error_code"] or t["metrics"].get("incomplete_execution") for t in traces
        ) and not any(s["safety_violations"] for s in samples):
            report.update(
                decision="inconclusive", reason="incomplete_or_budget_limited_paired_execution"
            )
        if any(not t["latency_complete"] for t in traces) and report["decision"] != "failed":
            report.update(
                decision="inconclusive", reason="interrupted_execution_latency_not_comparable"
            )
        if any(t["metrics"].get("incomplete_counterfactual") for t in traces) and not any(
            s["safety_violations"] for s in samples
        ):
            report.update(
                decision="inconclusive",
                reason="unobserved_query_or_unverified_cost_requires_new_authorized_capture",
            )
        if (
            latency_ratio > thresholds["max_latency_ratio"]
            or call_ratio > thresholds["max_call_ratio"]
        ):
            report.update(decision="failed", reason="frozen_cost_or_latency_regression")
        component_regressions = []
        metric_names = {
            "research_heuristic": ["repository_recall"],
            "routing_policy": ["intent_accuracy", "action_accuracy", "reference_accuracy"],
            "retrieval_recipe": ["publication_recall", "project_recall", "precision"],
            "teaching_skill": ["fact_recall", "action_coverage", "layout_match"],
        }[frozen["target"]]
        if is_r3(frozen.get("typed_execution_contract")):
            metric_names = list(R3_METRICS)
        for metric in metric_names:
            before = [
                s["baseline_metrics"][metric]
                for s in samples
                if s["baseline_metrics"][metric] is not None
            ]
            after = [
                s["candidate_metrics"][metric]
                for s in samples
                if s["candidate_metrics"][metric] is not None
            ]
            if (
                before
                and after
                and mean(after) < mean(before) - thresholds["max_quality_regression"]
            ):
                component_regressions.append(metric)
        if component_regressions and not any(
            t["metrics"].get("incomplete_counterfactual")
            or t["metrics"].get("incomplete_execution")
            or t["error_code"]
            for t in traces
        ):
            report.update(decision="failed", reason="critical_metric_regression")
        if frozen["dataset_split"] != "holdout" and report["decision"] != "failed":
            report.update(
                decision="inconclusive",
                reason="development_calibration_is_not_independent_acceptance",
            )
        if self.db.settings.mode != "test" and frozen["dataset_provenance"] != "human_gold":
            report.update(
                decision="inconclusive",
                reason=(
                    "development_probe_is_not_independent_gold"
                    if frozen["dataset_provenance"] == "development_probe"
                    else "fixture_is_not_production_gold"
                ),
            )
        report.update(
            {
                "dataset_hash": frozen["dataset_hash"],
                "dataset_provenance": frozen["dataset_provenance"],
                "candidate_hash": frozen["candidate_hash"],
                "baseline_hash": frozen["base_hash"],
                "target": frozen["target"],
                "environment_hash": frozen["environment_hash"],
                "environment_stable": True,
                "evaluator_version": EVALUATOR_VERSION,
                "replay_version": REPLAY_VERSION,
                "evaluation_scope": RESEARCH_RELEASE_SCOPE
                if frozen["target"] == "research_heuristic"
                else TYPED_RELEASE_SCOPE
                if frozen["target"] in {"routing_policy", "retrieval_recipe"}
                else RELEASE_SCOPE,
                "engine_versions": frozen["engine_versions"],
                "replay_trace_manifest_hash": fingerprint(traces),
                "frozen_input_hash": fingerprint(frozen),
                "hard_gate_passed": not any(s["safety_violations"] for s in samples),
                "critical_metric_regressions": component_regressions,
                "model_calls": sum(t["model_calls"] for t in traces),
                "incomplete_execution": any(
                    t["metrics"].get("incomplete_execution") for t in traces
                ),
                "call_cap": frozen["total_call_cap"],
                "per_arm_call_cap": frozen["per_arm_call_cap"],
                "call_ratio": call_ratio,
                "latency_ratio": latency_ratio,
                "human_effectiveness_validated": False,
                "live_search_effectiveness_validated": False,
                "new_external_requests": 0,
                "per_arm_request_cap": frozen["per_arm_request_cap"],
                "external_acceptance_blockers": [
                    "real_learner_pre_post_and_transfer_effectiveness_not_measured"
                ]
                + (
                    ["synthetic_fixture_is_not_human_gold"]
                    if frozen["dataset_provenance"] == "fixture"
                    else []
                ),
                "same_inputs": True,
                "no_model_self_grading": True,
                "published": False,
            }
        )
        if frozen["target"] in {"routing_policy", "retrieval_recipe"}:
            report["typed_execution_contract"] = typed_execution_contract(
                frozen.get("typed_execution_contract")
            )
        if frozen["target"] == "retrieval_recipe":
            if is_r3(frozen.get("typed_execution_contract")):
                real = frozen["dataset_provenance"] == "human_gold"
                synthetic = any(t["r3_binding"]["synthetic_execution"] for t in traces)
                if real and synthetic and report["decision"] != "failed":
                    report.update(
                        decision="inconclusive",
                        reason="synthetic_execution_is_not_real_retrieval_evidence",
                    )
                report.update(
                    retrieval_evaluation_scope=R3_SCOPE,
                    admitted_retrieval_context_contract=RETRIEVAL_CONTEXT_CONTRACT,
                    current_retrieval_quality_validated=real
                    and not synthetic
                    and report["decision"] == "passed"
                    and frozen["typed_execution_contract"]["retrieval_context_contract"]
                    == RETRIEVAL_CONTEXT_CONTRACT,
                    r3_independent_labels_qualified=real,
                    synthetic_execution=synthetic,
                    fixture_process_only=not real,
                )
                report["external_acceptance_blockers"] = (
                    ([] if real else ["synthetic_fixture_is_not_human_gold"])
                    + ["frozen_candidate_pool_only_not_universal_retrieval_quality"]
                    + (
                        ["current_retrieval_requires_matching_execution_and_scoring"]
                        if frozen["typed_execution_contract"]["retrieval_context_contract"]
                        != RETRIEVAL_CONTEXT_CONTRACT
                        else []
                    )
                )
            else:
                report.update(
                    retrieval_evaluation_scope="historical_whole_publication_selection_only",
                    admitted_retrieval_context_contract=RETRIEVAL_CONTEXT_CONTRACT,
                    current_retrieval_quality_validated=False,
                    fixture_process_only=frozen["dataset_provenance"] == "fixture",
                )
                report["external_acceptance_blockers"].append(
                    "current_bounded_fact_retrieval_requires_compatible_execution_and_scoring"
                )
        return report

    def commit_replay_evaluation(self, run, epoch, worker, traces, report):
        frozen = run.input_manifest
        with self.db.transaction() as db:
            active = leased(db, run.id, epoch, worker)
            locked(db, EvolutionCandidate, frozen["candidate_id"])
            current, dataset, base = self._validate_replay(db, run.principal_id, frozen)
            if frozen.get("evolution_graph"):
                from .evolution_stages import fail, paired_inputs, stage_in, validate_run_in

                validate_run_in(self.app, db, active)
                if stage_in(db, active, "result") is not None:
                    return
                checked_traces, samples = paired_inputs(self.app, db, active)
                checked_report = self.build_replay_report(active, checked_traces, samples)
                if traces != checked_traces or report != checked_report:
                    fail()
                if stage_in(db, active, "replay.aggregate.v1", required=True) != {
                    "trace_hash": fingerprint(traces),
                    "report_hash": fingerprint(report),
                }:
                    fail()
            evaluation = EvolutionEvaluation(
                candidate_id=current.id,
                dataset_id=dataset.id,
                candidate_hash=current.content_hash,
                base_hash=base.content_hash,
                decision=report["decision"],
                report=report,
                run_id=run.id,
            )
            db.add(evaluation)
            db.flush()
            saved = db.scalar(
                select(PolicyReplayManifest)
                .where(PolicyReplayManifest.run_id == run.id)
                .with_for_update()
            )
            saved.evaluation_id, saved.trace_hash, saved.trace = (
                evaluation.id,
                fingerprint(traces),
                traces,
            )
            current.status = "evaluated"
            self.app.runtime.save_stage_in(
                db, active, "result", {"evaluation_id": evaluation.id, **report}
            )
            if frozen.get("evolution_graph"):
                from .evolution_stages import save_effect_in

                save_effect_in(
                    db,
                    active,
                    {
                        "evaluation_id": evaluation.id,
                        "candidate_id": current.id,
                        "candidate_hash": current.content_hash,
                        "base_hash": base.content_hash,
                        "trace_hash": fingerprint(traces),
                        "published": False,
                    },
                )

    @staticmethod
    def _score_replay_output(
        case: dict[str, Any], blocks: list[dict[str, Any]], error: str | None
    ) -> dict[str, Any]:
        expected = case["expected"]
        facts = {f["fact_atom_id"]: (u, f) for u in case["frozen_units"] for f in u["facts"]}
        seen = set()
        hard = 0
        incomplete = False
        actions = set()
        for block in blocks:
            if block.get("gap_category") in {"budget", "authorization"}:
                incomplete = True
            ids = set(block.get("fact_atom_ids", []))
            if block.get("blocked_reason"):
                hard += 1  # Conservatively reject an unsafe proposal even when Guard contained it.
            if ids:
                if not ids <= facts.keys() or block.get("checked") not in {
                    "exact",
                    "semantic_checked",
                    "exact_fallback",
                    "source_anchored_gap",
                }:
                    hard += 1
                    continue
                conditions = {c for f in ids for c in facts[f][1]["conditions"]}
                publications = {facts[f][0]["publication_id"] for f in ids}
                if (
                    set(block.get("conditions", [])) != conditions
                    or set(block.get("publication_ids", [])) != publications
                ):
                    hard += 1
                    continue
                if block.get("checked") == "source_anchored_gap":
                    if (
                        block.get("kind") != "evidence_gap"
                        or len(ids) != 1
                        or block.get("gap_contract") != "source-anchored-gap-1.6.1"
                    ):
                        hard += 1
                    # A bounded statement of uncertainty is not factual coverage.
                elif block.get("kind") == "evidence_gap":
                    hard += 1
                else:
                    seen.update(ids)
            elif block.get("kind") == "learning_action":
                if block.get("checked") != "server_template" or block.get("action_key") not in {
                    "question",
                    "practice",
                    "clarify",
                    "build",
                }:
                    hard += 1
                else:
                    actions.add(block["action_key"])
            elif block.get("kind") != "evidence_gap":
                hard += 1
            if any(
                fragment.casefold() in block.get("text", "").casefold()
                for fragment in expected["forbidden_text"]
            ):
                hard += 1
        fact_recall = len(set(expected["required_fact_ids"]) & seen) / len(
            set(expected["required_fact_ids"])
        )
        requested_actions = set(expected["required_action_keys"])
        action_coverage = (
            len(requested_actions & actions) / len(requested_actions) if requested_actions else None
        )
        order = expected["preferred_order"]
        styles = {
            block.get("style")
            for block in blocks
            if block.get("fact_atom_ids") and block.get("kind") != "evidence_gap"
        }
        layout_match = (
            (
                float("code_first" in styles)
                if order == "code_first"
                else float("table_cell" in styles)
                if order == "comparison_first"
                else float("paragraph" in styles)
            )
            if order
            else None
        )
        values = [v for v in [fact_recall, action_coverage, layout_match] if v is not None]
        return {
            "score": mean(values) if not error else 0.0,
            "fact_recall": fact_recall,
            "action_coverage": action_coverage,
            "layout_match": layout_match,
            "hard_gate_violations": hard,
            "error": error,
            "incomplete_execution": incomplete,
            "claims_about_learning_mastery": False,
        }

    def _checked_replay_release(
        self,
        db: Session,
        actor: str,
        candidate: EvolutionCandidate,
        evaluation: EvolutionEvaluation,
        dataset: EvalDataset,
    ) -> None:
        row = db.scalar(
            select(PolicyReplayManifest).where(PolicyReplayManifest.evaluation_id == evaluation.id)
        )
        run = db.get(AgentRun, row.run_id) if row else None
        if (
            not row
            or not run
            or run.status != "succeeded"
            or run.workflow != "evolution_replay"
            or run.principal_id != actor
            or run.namespace_id != candidate.namespace_id
            or evaluation.run_id != run.id
            or run.input_hash != row.frozen_hash
            or run.input_manifest != row.frozen_input
            or row.candidate_id != candidate.id
            or row.dataset_id != dataset.id
            or row.frozen_hash != fingerprint(row.frozen_input)
            or row.frozen_hash != evaluation.report.get("frozen_input_hash")
            or row.trace_hash != fingerprint(row.trace)
            or row.trace_hash != evaluation.report.get("replay_trace_manifest_hash")
            or evaluation.report.get("replay_version") != REPLAY_VERSION
            or evaluation.report.get("engine_versions") != engine_versions()
            or evaluation.report.get("hard_gate_passed") is not True
            or dataset.split != "holdout"
            or evaluation.report.get("evaluation_scope")
            != (
                RESEARCH_RELEASE_SCOPE
                if candidate.target == "research_heuristic"
                else TYPED_RELEASE_SCOPE
                if candidate.target in {"routing_policy", "retrieval_recipe"}
                else RELEASE_SCOPE
            )
        ):
            raise DomainError(
                "SEMANTIC_REPLAY_REQUIRED",
                "教学内容变化必须通过同冻结输入实际执行与独立 Guard 的回放。",
                422,
            )
        self._validate_replay(db, actor, row.frozen_input)
        if row.frozen_input.get("evolution_graph"):
            from .evolution_stages import reconcile_in, validate_run_in

            validate_run_in(self.app, db, run, terminal=True)
            # New release also binds every private arm and its original egress
            # scope. A once-valid aggregate cannot bypass narrowed authority or
            # damaged execution artifacts merely because old trace hashes match.
            reconcile_in(self.app, db, run, terminal=True)
        self._checked_replay_trace(db, row, evaluation)
        if candidate.target in {"routing_policy", "retrieval_recipe"} and (
            evaluation.report.get("typed_execution_contract")
            != typed_execution_contract(row.frozen_input.get("typed_execution_contract"))
        ):
            raise DomainError("SEMANTIC_REPLAY_REQUIRED", "评测报告的执行/评分合同不匹配。", 422)
        if candidate.target == "retrieval_recipe":
            check_typed_retrieval_release(
                typed_execution_contract(row.frozen_input.get("typed_execution_contract")),
                evaluation.report,
                mode=self.db.settings.mode,
                provenance=dataset.provenance,
                admitted_retrieval_context_contract=RETRIEVAL_CONTEXT_CONTRACT,
            )
        if (
            dataset.provenance == "human_gold"
            and len(
                [
                    a
                    for a in self._attestations(db, dataset)
                    if a["decision"] == "approve" and a["frozen_hash"] == dataset.frozen_hash
                ]
            )
            < 2
        ):
            raise DomainError("GOLD_ATTESTATIONS_REQUIRED", "人工 Gold 的独立确认已失效。", 422)

    def _checked_replay_trace(
        self, db: Session, row: PolicyReplayManifest, evaluation: EvolutionEvaluation
    ) -> None:
        """Rebuild release measurements from frozen outputs and live receipt records.

        A trace digest protects storage integrity, not the continued validity of a
        model receipt. A redacted, unfinished, cross-arm or unmatched receipt may
        not be hidden behind a previously computed passing report.
        """
        frozen, report = row.frozen_input, evaluation.report
        traces = row.trace
        try:
            if len(traces) != 2 * len(frozen["cases"]):
                raise ValueError("Missing paired executions")
            by_pair = {(t["case_id"], t["arm"]): t for t in traces}
            if len(by_pair) != len(traces):
                raise ValueError("Duplicate paired executions")
            receipts = {
                r.id: r
                for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == row.run_id))
            }
            consumed: set[str] = set()
            samples = {sample["case_id"]: sample for sample in report["samples"]}
            for index, case in enumerate(frozen["cases"]):
                inputs = set()
                for arm in ("baseline", "candidate"):
                    trace = by_pair[(case["case_id"], arm)]
                    expected_hash = frozen["base_hash" if arm == "baseline" else "candidate_hash"]
                    prefix = f"replay:{index}:{arm}:"
                    ids = trace["receipt_ids"]
                    hashes = trace["receipt_request_hashes"]
                    responses = trace["receipt_response_hashes"]
                    if frozen["target"] in {"routing_policy", "retrieval_recipe"}:
                        try:
                            check_typed_execution_trace(
                                trace, frozen["target"], frozen.get("typed_execution_contract")
                            )
                        except DomainError as exc:
                            raise ValueError("Typed output contract changed") from exc
                    if is_r3(frozen.get("typed_execution_contract")):
                        run = db.get(AgentRun, row.run_id)
                        if run is None:
                            raise ValueError("Replay run missing")
                        check_r3_trace(
                            self, db, run.principal_id, frozen, row.run_id, index, case, trace
                        )
                    if (
                        trace["policy_hash"] != expected_hash
                        or trace["output_hash"] != fingerprint(trace["blocks"])
                        or trace["error_code"] is not None
                        or trace["latency_complete"] is not True
                        or type(trace["latency_ms"]) not in {float, int}
                        or not isfinite(trace["latency_ms"])
                        or trace["latency_ms"] < 0
                        or trace["model_call_cap"] != frozen["per_arm_call_cap"]
                        or len(ids) != len(hashes)
                        or len(ids) != len(responses)
                        or len(set(ids)) != len(ids)
                        or consumed.intersection(ids)
                        or trace["model_calls"] != len(ids)
                        or len(ids) > frozen["per_arm_call_cap"]
                    ):
                        raise ValueError("Unmatched or incomplete arm")
                    for receipt_id, request_hash, response_hash in zip(
                        ids, hashes, responses, strict=True
                    ):
                        receipt = receipts.get(receipt_id)
                        if (
                            not receipt
                            or not receipt.stage_key.startswith(prefix)
                            or receipt.status != "completed"
                            or receipt.response is None
                            or receipt.request_hash != request_hash
                            or fingerprint(receipt.response) != response_hash
                            or receipt.profile_version != frozen["model_profile"]
                        ):
                            raise ValueError("Receipt no longer supports replay")
                        consumed.add(receipt_id)
                    metrics = (
                        self._score_research_replay(case, trace["research_result"], None)
                        if frozen["target"] == "research_heuristic"
                        else score_typed_execution(
                            case,
                            trace["typed_result"],
                            None,
                            frozen["target"],
                            frozen.get("typed_execution_contract"),
                        )
                        if frozen["target"] in {"routing_policy", "retrieval_recipe"}
                        else self._score_replay_output(case, trace["blocks"], None)
                    )
                    if (
                        metrics != trace["metrics"]
                        or metrics != samples[case["case_id"]][arm + "_metrics"]
                        or metrics["score"] != samples[case["case_id"]][arm]
                        or metrics["hard_gate_violations"] != 0
                        or metrics.get("incomplete_execution")
                        or metrics.get("incomplete_counterfactual")
                    ):
                        raise ValueError("Metrics not supported by execution")
                    inputs.add(trace["frozen_input_hash"])
                if len(inputs) != 1:
                    raise ValueError("Unmatched arm inputs")
            if consumed != receipts.keys():
                raise ValueError("Unaccounted model receipt")
            if is_r3(frozen.get("typed_execution_contract")):
                synthetic = any(t["r3_binding"]["synthetic_execution"] for t in traces)
                real = frozen["dataset_provenance"] == "human_gold"
                if (
                    report.get("synthetic_execution") is not synthetic
                    or report.get("r3_independent_labels_qualified") is not real
                    or report.get("current_retrieval_quality_validated")
                    is not (real and not synthetic and report["decision"] == "passed")
                ):
                    raise ValueError("Retrieval evidence qualification changed")
            arms = {
                arm: [t for t in traces if t["arm"] == arm] for arm in ("baseline", "candidate")
            }
            latency_ratio = mean(t["latency_ms"] for t in arms["candidate"]) / max(
                1.0, mean(t["latency_ms"] for t in arms["baseline"])
            )
            call_ratio = sum(t["model_calls"] for t in arms["candidate"]) / max(
                1, sum(t["model_calls"] for t in arms["baseline"])
            )
            thresholds = frozen["threshold_policy"]
            if (
                report.get("same_inputs") is not True
                or report.get("no_model_self_grading") is not True
                or report.get("model_calls") != len(consumed)
                or report.get("latency_ratio") != latency_ratio
                or report.get("call_ratio") != call_ratio
                or latency_ratio > thresholds["max_latency_ratio"]
                or call_ratio > thresholds["max_call_ratio"]
            ):
                raise ValueError("Execution setup or cost gate changed")
            metric_names = {
                "research_heuristic": ("repository_recall",),
                "routing_policy": ("intent_accuracy", "action_accuracy", "reference_accuracy"),
                "retrieval_recipe": ("publication_recall", "project_recall", "precision"),
                "teaching_skill": ("fact_recall", "action_coverage", "layout_match"),
            }[frozen["target"]]
            if is_r3(frozen.get("typed_execution_contract")):
                metric_names = R3_METRICS
            for metric in metric_names:
                before = [
                    t["metrics"][metric]
                    for t in arms["baseline"]
                    if t["metrics"][metric] is not None
                ]
                after = [
                    t["metrics"][metric]
                    for t in arms["candidate"]
                    if t["metrics"][metric] is not None
                ]
                if (
                    before
                    and after
                    and mean(after) < mean(before) - thresholds["max_quality_regression"]
                ):
                    raise ValueError("Critical metric regressed")
            if is_r3(frozen.get("typed_execution_contract")):
                check_r3_terminal_report(db, row, evaluation)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise DomainError(
                "SEMANTIC_REPLAY_REQUIRED", "配对回放的原始输出、调用回执或门禁测量不再一致。", 422
            ) from exc

    def replay_detail(self, actor: str, identifier: str) -> dict[str, Any]:
        with self.db.read() as db:
            row = db.get(PolicyReplayManifest, identifier)
            candidate = db.get(EvolutionCandidate, row.candidate_id) if row else None
            if not row or not candidate:
                raise DomainError("NOT_FOUND", "回放不存在。", 404)
            namespace_admin(db, actor, candidate.namespace_id)
            run = db.get(AgentRun, row.run_id)
            evaluation = (
                db.get(EvolutionEvaluation, row.evaluation_id) if row.evaluation_id else None
            )
            # Never return cached source-derived answers after evidence/authority changes.
            try:
                self._validate_replay(db, actor, row.frozen_input)
                available = True
            except DomainError:
                available = False
            return {
                "replay_id": row.id,
                "run_id": row.run_id,
                "run_status": run.status if run else "missing",
                "evaluation_id": row.evaluation_id,
                "frozen_hash": row.frozen_hash,
                "trace_hash": row.trace_hash,
                "report": evaluation.report if evaluation else None,
                "trace": row.trace if available else [],
                "evidence_available": available,
                "historical_trace_not_rewritten": True,
            }

    def register_research_replay_dataset(
        self, actor: str, value: ResearchReplayDatasetInput
    ) -> dict[str, Any]:
        """Capture only server-owned search observations, never client-invented results."""
        from .taxonomy import require_topic

        with self.db.transaction() as db:
            ns = namespace_admin(db, actor, value.namespace_id)
            if ns.kind != "private":
                raise DomainError("REPLAY_PRIVATE", "学习问题与研究轨迹只能在授权私人域回放。", 403)
            if self.db.settings.mode != "test" and value.provenance == "fixture":
                raise DomainError("FIXTURE_NOT_GOLD", "合成语料不能登记生产 Gold。", 422)
            if len({c.case_id for c in value.cases}) != len(value.cases):
                raise DomainError("DATASET_DUPLICATE", "研究样例身份重复。", 422)
            if value.provenance == "human_gold" and (
                len(set(value.annotators)) < 2
                or any(not db.get(Principal, a) for a in value.annotators)
            ):
                raise DomainError("ANNOTATORS", "研究 Gold 需要两名可独立认证的标注者。", 422)
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
                    or before.threshold_policy.get("target") != "research_heuristic"
                    or calibration.report.get("replay_version") != REPLAY_VERSION
                    or value.thresholds.min_cases < 20
                    or value.thresholds.min_groups < 10
                ):
                    raise DomainError(
                        "BASELINE_CALIBRATION_REQUIRED",
                        "正式研究门禁须先有同域开发集真实执行基线及足够独立样本。",
                        422,
                    )
            if db.scalar(
                select(EvalDataset.id).where(
                    EvalDataset.namespace_id == ns.id,
                    EvalDataset.name == value.name,
                    EvalDataset.version == value.version,
                )
            ):
                raise DomainError("DATASET_IMMUTABLE", "冻结数据集不可覆盖。")
            cases = []
            for item in value.cases:
                for topic in item.topic_keys:
                    require_topic(db, actor, topic)
                corpus: dict[str, dict[str, Any]] = {}
                repository_names: dict[str, str] = {}
                for run_id in dict.fromkeys(item.observation_run_ids):
                    source_run = db.get(AgentRun, run_id)
                    if (
                        not source_run
                        or source_run.principal_id != actor
                        or source_run.namespace_id != ns.id
                        or source_run.status not in {"succeeded", "partially_succeeded"}
                        or source_run.workflow not in {"knowledge_build", "research"}
                    ):
                        raise DomainError(
                            "RESEARCH_CORPUS_AUTHORITY",
                            "语料只能引用当前身份同域已完成研究任务。",
                            403,
                        )
                    observations = db.scalars(
                        select(SearchObservation)
                        .join(ResearchNode)
                        .where(ResearchNode.run_id == run_id)
                    ).all()
                    if not observations:
                        raise DomainError(
                            "RESEARCH_CORPUS_EMPTY", "指定任务没有持久化搜索观测。", 422
                        )
                    for observation in observations:
                        node = db.get(ResearchNode, observation.node_id)
                        if not node or not node.payload.get("query"):
                            raise DomainError(
                                "RESEARCH_CORPUS_NODE", "观测缺少原始查询与通道。", 422
                            )
                        payload = observation.payload
                        if value.provenance == "human_gold" and payload.get("synthetic"):
                            raise DomainError(
                                "SYNTHETIC_GOLD", "合成搜索观测不能伪装成人工真实 Gold。", 422
                            )
                        query = node.payload["query"]
                        channel = node.payload.get("channel", "repository")
                        key = fingerprint({"query": query, "channel": channel})
                        if observation.query_hash != fingerprint(query):
                            raise DomainError("RESEARCH_CORPUS_HASH", "原始查询哈希不一致。")
                        receipts = payload.get("external_request_ids")
                        cost = payload.get("external_request_count")
                        receipt_snapshots = []
                        if receipts is not None:
                            if (
                                not isinstance(receipts, list)
                                or type(cost) is not int
                                or cost != len(set(receipts))
                            ):
                                raise DomainError(
                                    "RESEARCH_COST_RECEIPT", "搜索观测的费用凭据不一致。"
                                )
                            for receipt_id in receipts:
                                receipt = db.get(ExternalRequest, receipt_id)
                                if not receipt or receipt.run_id != source_run.id:
                                    raise DomainError(
                                        "RESEARCH_COST_RECEIPT", "外部请求凭据不属于冻结研究任务。"
                                    )
                                receipt_snapshots.append(
                                    {
                                        "id": receipt.id,
                                        "request_hash": receipt.request_hash,
                                        "status": receipt.status,
                                        "response_metadata": receipt.response_metadata,
                                    }
                                )
                        elif value.provenance == "fixture" and self.db.settings.mode == "test":
                            # Explicitly synthetic fixture cost is only a workflow test.
                            cost = 1
                        else:
                            cost = None  # old unaudited observations cannot establish same-cost benefit
                        entry = {
                            "query_key": key,
                            "query": query,
                            "channel": channel,
                            "observation_id": observation.id,
                            "node_id": node.id,
                            "source_run_id": run_id,
                            "query_hash": observation.query_hash,
                            "observation_hash": fingerprint(payload),
                            "observation": payload,
                            "request_cost": cost,
                            "synthetic_cost": receipts is None and value.provenance == "fixture",
                            "node_payload_hash": fingerprint(node.payload),
                            "observed_at": observation.created_at.isoformat(),
                            "request_receipts": receipt_snapshots,
                        }
                        if (
                            key in corpus
                            and corpus[key]["observation_hash"] != entry["observation_hash"]
                        ):
                            raise DomainError(
                                "RESEARCH_CORPUS_CONFLICT",
                                "同一精确查询存在不同时间的观测，不能任意挑选有利结果。",
                                422,
                            )
                        corpus[key] = entry
                        for repo in payload.get("candidates", []):
                            identifier = str(repo.get("repository_id", repo.get("name")))
                            repository_names[identifier] = repo.get("name", identifier)
                if not set(item.relevant_repository_ids) <= repository_names.keys():
                    raise DomainError(
                        "RESEARCH_GOLD_SCOPE", "相关仓库 Gold 必须出现在已捕获的实际观测中。", 422
                    )
                families = sorted({repository_names[i] for i in item.relevant_repository_ids})
                cases.append(
                    {
                        "case_id": item.case_id,
                        "group_id": item.group_id,
                        "user_question_group": item.user_question_group,
                        "time_partition": item.time_partition,
                        "repository_family": "research-family:" + fingerprint(families),
                        "repository_families": families,
                        "question": item.question,
                        "topic_keys": item.topic_keys,
                        "corpus": [corpus[k] for k in sorted(corpus)],
                        "expected": {
                            "relevant_repository_ids": list(
                                dict.fromkeys(item.relevant_repository_ids)
                            )
                        },
                    }
                )
            policy = {
                **value.thresholds.model_dump(mode="json"),
                "kind": "research_execution_replay",
                "target": "research_heuristic",
                "annotators": sorted(set(value.annotators)),
                "max_safety_violations": 0,
                "confidence": 0.95,
                "replay_version": REPLAY_VERSION,
                "declared_split": value.split,
            }
            row = EvalDataset(
                namespace_id=ns.id,
                name=value.name,
                version=value.version,
                provenance=value.provenance,
                frozen_hash=fingerprint({"cases": cases, "policy": policy}),
                cases=cases,
                split=value.split,
                threshold_policy=policy,
            )
            db.add(row)
            db.flush()
            return {
                "dataset_id": row.id,
                "frozen_hash": row.frozen_hash,
                "split": row.split,
                "independent_groups": len(set(independence_groups(cases).values())),
                "captured_observations": sum(len(c["corpus"]) for c in cases),
                "external_calls": 0,
                "scope": "frozen_recorded_searches_not_fresh_github_search",
                "requires_individual_attestations": value.provenance == "human_gold",
            }

    def _check_research_corpus(
        self, db: Session, actor: str, namespace: str, case: dict[str, Any]
    ) -> None:
        for entry in case["corpus"]:
            run = db.get(AgentRun, entry["source_run_id"])
            node = db.get(ResearchNode, entry["node_id"])
            observation = db.get(SearchObservation, entry["observation_id"])
            if (
                not run
                or not node
                or not observation
                or run.principal_id != actor
                or run.namespace_id != namespace
                or run.status not in {"succeeded", "partially_succeeded"}
                or node.run_id != run.id
                or observation.node_id != node.id
                or observation.query_hash != entry["query_hash"]
                or fingerprint(node.payload) != entry["node_payload_hash"]
                or fingerprint(observation.payload) != entry["observation_hash"]
                or observation.payload != entry["observation"]
            ):
                raise DomainError(
                    "RESEARCH_CORPUS_CHANGED", "冻结研究观测来源、权限或内容已经变化。"
                )
            for frozen_receipt in entry["request_receipts"]:
                receipt = db.get(ExternalRequest, frozen_receipt["id"])
                if (
                    not receipt
                    or receipt.run_id != run.id
                    or {
                        "id": receipt.id,
                        "request_hash": receipt.request_hash,
                        "status": receipt.status,
                        "response_metadata": receipt.response_metadata,
                    }
                    != frozen_receipt
                ):
                    raise DomainError("RESEARCH_COST_RECEIPT", "冻结外部请求凭据已经变化。")

    @staticmethod
    def _score_research_replay(
        case: dict[str, Any], result: dict[str, Any], error: str | None
    ) -> dict[str, Any]:
        wanted = set(case["expected"]["relevant_repository_ids"])
        returned = set(result.get("repository_ids", []))
        recall = len(wanted & returned) / len(wanted)
        incomplete = (
            bool(result.get("unobserved_queries"))
            or bool(result.get("request_cost_unverified"))
            or bool(result.get("incomplete"))
        )
        hard = int(error in {"RESEARCH_QUERY_SCOPE", "REPLAY_CAPABILITY"})
        return {
            "score": recall if not error else 0.0,
            "repository_recall": recall,
            "hard_gate_violations": hard,
            "error": error,
            "incomplete_counterfactual": incomplete,
            "new_external_requests": result.get("new_external_requests", 0),
            "requests_used": result.get("requests_used", 0),
            "request_cap": result.get("request_cap"),
        }
