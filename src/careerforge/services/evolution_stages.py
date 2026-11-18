"""Explicit M6 stage identities and private artifacts. No release authority in state."""

from __future__ import annotations

import inspect
from importlib import import_module

from sqlalchemy import select

from ..contracts import ModelOutput
from ..errors import DomainError
from ..models import (
    AgentRun,
    EvolutionCandidate,
    EvolutionEvaluation,
    FundingAgreement,
    ModelReceipt,
    Namespace,
    ObjectRow,
    StageArtifact,
)
from ..models_learning_ext import PolicyReplayManifest
from ..util import fingerprint
from .dependencies import check_closure
from .evolution_feedback import model_feedback_witnesses
from .evolution_replay import independence_groups
from .runtime import leased
from .security import namespace_admin

CANDIDATE_VERSION = "evolution-candidate-v1"
REPLAY_VERSION = "evolution-replay-v1"
CONTRACT = "evolution-paid-graph-v1"
TOPOLOGY = {
    "candidate": ["validate", "invoke_reflection", "validate_patch", "commit_candidate", "finish"],
    "replay": [
        "validate",
        "select_arm",
        "execute_arm",
        "score_arm",
        "aggregate",
        "commit_evaluation",
        "finish",
    ],
    "replay_steps": {"fixed": 6, "per_arm": 3, "arms_per_case": 2},
}


def graph_identity():
    # The legacy engine_versions calculation is intentionally untouched. This
    # new contract covers all extracted scheduling behavior and its topology.
    modules = (
        "services.evolution_stages",
        "services.evolution_privacy",
        "services.privacy",
        "services.dependencies",
        "services.knowledge",
        "services.taxonomy",
        "services.evolution_workflow",
        "contracts_evolution_graph",
        "workflows.evolution_paid",
        "workflows.evolution_session",
        "services.evolution_commands",
        "workflows.runtime_adapter",
        "workflows.registry",
        "application",
        "config",
        "db",
        "services.session_execution",
        "services.workflow_controls",
        "services.conversation_actions",
        "services.workflow_continuation",
        "services.evolution",
        "services.evolution_replay",
        "services.runtime",
        "models_dialogue",
        "adapters.model",
        "services.evolution_feedback",
        "services.policy_inputs",
    )
    return {
        "contract": CONTRACT,
        "topology": fingerprint(TOPOLOGY),
        "implementation": fingerprint(
            {name: inspect.getsource(import_module("careerforge." + name)) for name in modules}
        ),
    }


def fail(message="演化阶段绑定缺失、被擦除或不一致。"):
    raise DomainError("EVOLUTION_GRAPH_BINDING", message, 409)


def validate_graph_identity(manifest):
    marker = manifest.get("evolution_graph")
    if not isinstance(marker, dict) or marker.get("identity") != graph_identity():
        fail("演化图实现或拓扑身份已变化，禁止升级历史运行。")
    return marker


def stage_in(db, run, key, *, required=False):
    row = db.scalar(
        select(StageArtifact).where(StageArtifact.run_id == run.id, StageArtifact.stage_key == key)
    )
    if row is None:
        if required:
            fail("领域阶段缺失；检查点不能代替业务结果。")
        return None
    if (
        row.input_hash != run.input_hash
        or not isinstance(row.payload, dict)
        or not row.payload
        or row.payload.get("consent_withdrawn")
    ):
        fail()
    return row.payload


def validate_run_in(app, db, run, *, terminal=False, active=True):
    if (
        run is None
        or run.workflow_engine != "langgraph"
        or run.state_schema_version != 1
        or (run.workflow, run.graph_version)
        not in {("evolution", CANDIDATE_VERSION), ("evolution_replay", REPLAY_VERSION)}
        or run.input_hash != fingerprint(run.input_manifest)
    ):
        fail()
    if terminal and (run.status != "succeeded" or not run.budget_settled):
        fail("评测后继必须读取已成功结算的原始运行。")
    marker = validate_graph_identity(run.input_manifest)
    from .evolution_workflow import validate_run_binding_in

    validate_run_binding_in(
        app, db, run.principal_id, run.namespace_id, run.input_manifest, run=run
    )
    namespace_admin(db, run.principal_id, run.namespace_id)
    if active or run.workflow == "evolution":
        model_feedback_witnesses(db, run)
    else:
        # Historical reconciliation is not new paid/release admission. Preserve
        # all live consent and frozen lineage checks, not active candidate status.
        from .evolution_feedback import policy_feedback_inputs

        frozen = run.input_manifest
        candidate = db.get(EvolutionCandidate, frozen["candidate_id"])
        if (
            not candidate
            or candidate.namespace_id != run.namespace_id
            or candidate.content_hash != frozen["candidate_hash"]
            or fingerprint(candidate.content) != frozen["candidate_hash"]
            or candidate.base_policy_id != frozen["base_id"]
        ):
            fail()
        _, witnesses, dependencies = policy_feedback_inputs(
            db,
            run.principal_id,
            run.namespace_id,
            candidate.experience_ids,
            candidate.base_policy_id,
        )
        if (
            witnesses != frozen["feedback_witnesses"]
            or dependencies != frozen["policy_dependencies"]
        ):
            fail()
        check_closure(db, run.principal_id, [frozen["selection_manifest_id"], *dependencies])
    if run.input_manifest.get("model_profile") != app.provider.profile_version:
        fail("冻结模型版本已变化。")
    if active:
        if run.workflow == "evolution_replay":
            app.evolution._validate_replay(db, run.principal_id, run.input_manifest)
        else:
            app.evolution.prepare_reflection(run)
    return marker


def receipts_in(db, run, execution, prefix):
    ids = execution.get("receipt_ids", [])
    if (
        not isinstance(ids, list)
        or len(ids) != len(set(ids))
        or execution.get("model_calls") != len(ids)
        or len(ids) != len(execution.get("receipt_request_hashes", []))
        or len(ids) != len(execution.get("receipt_response_hashes", []))
    ):
        fail()
    actual = list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))
    actual = [r for r in actual if r.stage_key.startswith(prefix)]
    if {r.id for r in actual} != set(ids):
        fail("臂工件与实际付费回执集合不一致。")
    for identifier, request_hash, response_hash in zip(
        ids,
        execution.get("receipt_request_hashes", []),
        execution.get("receipt_response_hashes", []),
        strict=True,
    ):
        receipt = db.get(ModelReceipt, identifier)
        if (
            not receipt
            or receipt.run_id != run.id
            or not receipt.stage_key.startswith(prefix)
            or receipt.status != "completed"
            or receipt.response is None
            or receipt.profile_version != run.input_manifest["model_profile"]
            or receipt.request_hash != request_hash
            or fingerprint(receipt.response) != response_hash
        ):
            fail("原付费回执不可核验；禁止重发或评分。")
        model_authority_in(db, run, receipt)


def arm_binding(run, index, arm):
    frozen = run.input_manifest
    if (
        type(index) is not int
        or not 0 <= index < len(frozen["cases"])
        or arm not in {"baseline", "candidate"}
    ):
        fail()
    return {
        "run_id": run.id,
        "input_hash": run.input_hash,
        "index": index,
        "arm": arm,
        "case_id": frozen["cases"][index]["case_id"],
        "policy_hash": frozen["base_hash" if arm == "baseline" else "candidate_hash"],
    }


def arm_execution_in(db, run, index, arm, *, required=False):
    key = f"replay:{index}:{arm}:"
    payload = stage_in(db, run, key + "execution.v1", required=required)
    if payload is None:
        return None
    if (
        payload.get("contract") != "evolution-arm-execution-v1"
        or payload.get("binding") != arm_binding(run, index, arm)
        or payload.get("execution_hash") != fingerprint(payload.get("execution"))
    ):
        fail()
    from .evolution_privacy import validate_run_generations_in

    validate_run_generations_in(db, run, dependencies=payload["access_dependencies"])
    receipts_in(db, run, payload["execution"], key)
    return payload["execution"]


def arm_result_in(app, db, run, index, arm, *, required=False):
    trace = stage_in(db, run, f"replay:{index}:{arm}:result", required=required)
    if trace is None:
        return None
    execution = arm_execution_in(db, run, index, arm, required=True)
    if trace != app.evolution.score_replay_arm(run, index, arm, execution):
        fail("臂结果与原始执行工件或独立评分不一致。")
    return trace


def arms(run):
    for index in range(len(run.input_manifest["cases"])):
        for arm in ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline"):
            yield index, arm


def next_arm(app, run):
    with app.db.read() as db:
        validate_run_in(app, db, run)
        found = None
        for index, arm in arms(run):
            trace = arm_result_in(app, db, run, index, arm)
            if trace is None and found is None:
                found = (index, arm)
            elif trace is not None and found is not None:
                fail("后续臂存在而前序结果缺失，禁止采用检查点跳过。")
        return found


def paired_inputs(app, db, run):
    traces = [arm_result_in(app, db, run, index, arm, required=True) for index, arm in arms(run)]
    clusters = independence_groups(run.input_manifest["cases"])
    samples = []
    by_pair = {(t["case_id"], t["arm"]): t for t in traces}
    for case in run.input_manifest["cases"]:
        baseline, candidate = (by_pair[(case["case_id"], arm)] for arm in ("baseline", "candidate"))
        if baseline["frozen_input_hash"] != candidate["frozen_input_hash"]:
            fail("两臂输入不一致。")
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
    return traces, samples


def reflection_receipt_in(app, db, run):
    from ..contracts import PolicyPatch
    from .dependencies import assert_source_scope
    from .privacy import assert_personal_access

    witness = stage_in(db, run, "reflection.validated.v1", required=True)
    receipt = db.get(ModelReceipt, witness.get("receipt_id"))
    if (
        not receipt
        or receipt.run_id != run.id
        or receipt.stage_key != "reflection"
        or receipt.status != "completed"
        or receipt.response is None
        or receipt.role != "reflection"
        or receipt.profile_version != run.input_manifest["model_profile"]
        or witness
        != {
            "receipt_id": receipt.id,
            "request_hash": receipt.request_hash,
            "response_hash": fingerprint(receipt.response),
        }
    ):
        fail("反思终态与原验证回执不一致。")
    prepared = app.evolution.prepare_reflection(run)
    request = app.gateway.request_payload(
        "reflection",
        prepared["system"],
        prepared["context"],
        PolicyPatch,
        data_classes=prepared["data_classes"],
        access_dependencies=prepared["dependencies"],
        output_schema=prepared["output_schema"],
    )
    funding = db.get(FundingAgreement, run.funding_id)
    if (
        not funding
        or "reflection" not in funding.allowed_roles
        or not set(request["data_classes"]) <= set(funding.allowed_data_classes)
    ):
        fail("反思权限范围已变化。")
    closure = check_closure(db, run.principal_id, request["access_dependencies"])
    assert_source_scope(db, request["access_dependencies"], funding.allowed_source_ids)
    owners = set(
        db.scalars(
            select(Namespace.owner_id)
            .join(ObjectRow, ObjectRow.namespace_id == Namespace.id)
            .where(
                ObjectRow.id.in_(request["access_dependencies"]), Namespace.kind != "shared_public"
            )
        )
    )
    owners.add(run.principal_id)
    namespace = db.get(Namespace, run.namespace_id)
    if namespace.kind != "shared_public":
        owners.add(namespace.owner_id)
    request.update(
        funding_source_scope=sorted(funding.allowed_source_ids),
        access_closure_hash=closure.closure_hash,
        privacy_generations={owner: assert_personal_access(db, owner) for owner in sorted(owners)},
    )
    if fingerprint(request) != receipt.request_hash:
        fail("反思请求哈希与冻结输入不一致。")
    model_authority_in(db, run, receipt)
    return receipt


def reconcile(app, run, epoch=None, worker=None, *, terminal=False):
    if not terminal:
        app.runtime.validate(run.id, epoch, worker)
    with app.db.read() as db:
        return reconcile_in(app, db, run, terminal=terminal)


def reconcile_in(app, db, run, *, terminal=False):
    """Validate within the caller's release transaction, without another effect."""
    current = db.get(AgentRun, run.id)
    validate_run_in(app, db, current, terminal=terminal, active=False)
    result = stage_in(db, current, "result", required=True)
    if current.workflow == "evolution":
        candidate = db.get(EvolutionCandidate, result.get("candidate_id"))
        receipt = reflection_receipt_in(app, db, current)
        patch = app.evolution.validate_reflection_patch(
            current, ModelOutput.model_validate(receipt.response)
        )
        from .policies import validate_policy

        if (
            not candidate
            or candidate.namespace_id != current.namespace_id
            or candidate.base_policy_id != current.input_manifest["base_policy_id"]
            or candidate.target != patch.target
            or candidate.content != validate_policy(patch.target, patch.content)
            or candidate.content_hash != fingerprint(candidate.content)
            or set(candidate.experience_ids) != set(patch.experience_ids)
            or set(candidate.training_group_ids) != set(patch.training_group_ids)
            or result != {"candidate_id": candidate.id, "published": False}
        ):
            fail()
        app.evolution._episodes(
            db, current.principal_id, current.namespace_id, candidate.experience_ids
        )
        require_effect_in(
            db,
            current,
            {
                "candidate_id": candidate.id,
                "candidate_hash": candidate.content_hash,
                "base_policy_id": candidate.base_policy_id,
                "base_hash": current.input_manifest["base_hash"],
                "published": False,
            },
        )
        return result
    manifest = db.scalar(
        select(PolicyReplayManifest).where(PolicyReplayManifest.run_id == run.id)
    )
    evaluation = db.get(EvolutionEvaluation, result.get("evaluation_id"))
    if (
        not manifest
        or not evaluation
        or manifest.frozen_hash != current.input_hash
        or manifest.frozen_input != current.input_manifest
        or manifest.evaluation_id != evaluation.id
        or evaluation.run_id != run.id
        or evaluation.candidate_id != manifest.candidate_id
        or evaluation.dataset_id != manifest.dataset_id
        or manifest.trace_hash != fingerprint(manifest.trace)
    ):
        fail()
    traces, samples = paired_inputs(app, db, current)
    report = app.evolution.build_replay_report(current, traces, samples)
    if (
        manifest.trace != traces
        or evaluation.report != report
        or result != {"evaluation_id": evaluation.id, **report}
    ):
        fail()
    require_effect_in(
        db,
        current,
        {
            "evaluation_id": evaluation.id,
            "candidate_id": current.input_manifest["candidate_id"],
            "candidate_hash": current.input_manifest["candidate_hash"],
            "base_hash": current.input_manifest["base_hash"],
            "trace_hash": manifest.trace_hash,
            "published": False,
        },
    )
    return result


def replay_step_limit(run):
    validate_graph_identity(run.input_manifest)
    count = len(run.input_manifest["cases"])
    cap = run.input_manifest["per_arm_call_cap"]
    if not 1 <= count <= 50 or type(cap) is not int or not 1 <= cap <= 20 or count * 2 * cap > 100:
        fail("冻结回放规模不在原始预算合同内。")
    # Actual topology: validate + (select + execute + score)*2N + final
    # select + aggregate + commit + finish. One END allowance, bounded by 306.
    return TOPOLOGY["replay_steps"]["fixed"] + count * 2 * TOPOLOGY["replay_steps"]["per_arm"]


def require_effect_in(db, run, result):
    """Read the original atomic witness; absence is never repaired by a reader."""
    from ..models import Idempotency

    receipt = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == run.principal_id,
            Idempotency.operation == "evolution.paid.effect.v1",
            Idempotency.key == run.id,
        )
    )
    effect = {
        "contract": "evolution-paid-effect-v1",
        "run_id": run.id,
        "workflow": run.workflow,
        "input_hash": run.input_hash,
        "result": result,
    }
    expected = {"effect": effect, "effect_hash": fingerprint(effect)}
    if (
        not receipt
        or receipt.request_hash != run.input_hash
        or receipt.result != expected
        # JSON identity also distinguishes false from 0 (Python equality does not).
        or fingerprint(receipt.result) != fingerprint(expected)
    ):
        fail("原始事务效果回执缺失或与领域结果不一致。")
    return receipt


def save_effect_in(db, run, result):
    """Body-free historical proof survives required private StageArtifact redaction."""
    from ..models import Idempotency

    operation = "evolution.paid.effect.v1"
    payload = {
        "contract": "evolution-paid-effect-v1",
        "run_id": run.id,
        "workflow": run.workflow,
        "input_hash": run.input_hash,
        "result": result,
    }
    old = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == run.principal_id,
            Idempotency.operation == operation,
            Idempotency.key == run.id,
        )
    )
    if old:
        if old.request_hash != run.input_hash or old.result != {
            "effect": payload,
            "effect_hash": fingerprint(payload),
        }:
            fail()
        return
    db.add(
        Idempotency(
            principal_id=run.principal_id,
            operation=operation,
            key=run.id,
            request_hash=run.input_hash,
            result={"effect": payload, "effect_hash": fingerprint(payload)},
        )
    )
    db.flush()


def historical_summary(app, actor, run_id):
    """History is not current authority. Never exposes redacted input/output bodies."""
    from ..models import Idempotency
    from ..models_dialogue import WorkflowEvolutionRequest, WorkflowRunLink
    from .privacy import assert_personal_access

    with app.db.read() as db:
        assert_personal_access(db, actor)
        run = db.get(AgentRun, run_id)
        if not run or run.principal_id != actor:
            fail()
        namespace_admin(db, actor, run.namespace_id)
        request = db.scalar(
            select(WorkflowEvolutionRequest).where(WorkflowEvolutionRequest.run_id == run.id)
        )
        link = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == run.id))
        receipt = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == "evolution.paid.effect.v1",
                Idempotency.key == run.id,
            )
        )
        if (
            not request
            or request.principal_id != actor or request.namespace_id != run.namespace_id
            or not link
            or link.workflow_id != request.workflow_id
            or request.admission_manifest_hash != run.input_hash
            or not receipt
            or receipt.request_hash != run.input_hash
        ):
            fail("历史效果没有原始事务回执。")
        if not isinstance(receipt.result, dict) or set(receipt.result) != {"effect", "effect_hash"}:
            fail()
        effect = receipt.result.get("effect")
        if (
            not isinstance(effect, dict)
            or set(effect) != {"contract", "run_id", "workflow", "input_hash", "result"}
            or receipt.result.get("effect_hash") != fingerprint(effect)
            or (
                effect.get("contract"),
                effect.get("run_id"),
                effect.get("workflow"),
                effect.get("input_hash"),
            )
            != ("evolution-paid-effect-v1", run.id, run.workflow, run.input_hash)
        ):
            fail()
        result = effect["result"]
        allowed = ({"candidate_id", "candidate_hash", "base_policy_id", "base_hash", "published"}
                   if run.workflow == "evolution" else
                   {"evaluation_id", "candidate_id", "candidate_hash", "base_hash", "trace_hash", "published"})
        if not isinstance(result, dict) or set(result) != allowed or result.get("published") is not False:
            fail("历史效果仅允许精确的无正文引用合同。")
        if run.workflow == "evolution":
            from ..models import PolicyVersion
            candidate = db.get(EvolutionCandidate, result.get("candidate_id"))
            base = db.get(PolicyVersion, result.get("base_policy_id"))
            if (
                not candidate
                or candidate.content_hash != result.get("candidate_hash")
                or candidate.namespace_id != run.namespace_id
                or not base or candidate.base_policy_id != base.id or base.content_hash != result["base_hash"]
                or base.namespace_id != run.namespace_id or base.target != candidate.target
            ):
                fail()
        else:
            evaluation = db.get(EvolutionEvaluation, result.get("evaluation_id"))
            manifest = db.scalar(
                select(PolicyReplayManifest).where(PolicyReplayManifest.run_id == run.id)
            )
            if (
                not evaluation
                or evaluation.run_id != run.id
                or evaluation.candidate_id != result["candidate_id"]
                or not manifest
                or manifest.evaluation_id != evaluation.id
                or manifest.candidate_id != result["candidate_id"] or manifest.dataset_id != evaluation.dataset_id
                or manifest.frozen_hash != run.input_hash
                or manifest.trace_hash != result.get("trace_hash")
                or evaluation.candidate_hash != result.get("candidate_hash")
                or evaluation.base_hash != result.get("base_hash")
            ):
                fail()
        summary = {
            "run_id": run.id,
            "historical_effect_committed": True,
            "run_status": run.status,
            "effect_receipt_id": receipt.id,
            "effect_hash": receipt.result["effect_hash"],
            **{key: result[key] for key in sorted(allowed)},
        }
    try:
        reconcile(app, run, terminal=True)
        available = True
    except DomainError:
        available = False
    return {**summary, "current_evidence_available": available, "release_authorized": False}


def record_model_authority(runtime, run_id, epoch, worker, key, role, data_classes, dependencies):
    """Persist exact new-contract egress scope without changing legacy wire receipts."""
    from .dependencies import assert_source_scope

    with runtime.db.transaction() as db:
        run = leased(db, run_id, epoch, worker)
        receipt = db.scalar(
            select(ModelReceipt).where(ModelReceipt.run_id == run.id, ModelReceipt.stage_key == key)
        )
        if (
            not receipt
            or receipt.status != "completed"
            or receipt.response is None
            or receipt.role != role
        ):
            fail()
        funding = db.get(FundingAgreement, run.funding_id)
        classes = sorted(set(data_classes or ["public_source"]))
        dependencies = sorted(set(dependencies or []))
        if role not in funding.allowed_roles or not set(classes) <= set(
            funding.allowed_data_classes
        ):
            fail("模型返回后原费用角色或数据范围已撤回。")
        assert_source_scope(db, dependencies, funding.allowed_source_ids)
        access = stage_in(db, run, "model-access:" + receipt.id, required=True)
        if (
            access.get("request_hash") != receipt.request_hash
            or access.get("access_dependencies") != dependencies
        ):
            fail()
        runtime.save_stage_in(
            db,
            run,
            "evolution-access:" + receipt.id,
            {
                "contract": "evolution-model-authority-v1",
                "receipt_id": receipt.id,
                "request_hash": receipt.request_hash,
                "role": role,
                "data_classes": classes,
                "access_dependencies": dependencies,
                "closure_hash": access["closure_hash"],
            },
        )


def model_authority_in(db, run, receipt):
    from .dependencies import assert_source_scope

    authority = stage_in(db, run, "evolution-access:" + receipt.id, required=True)
    access = stage_in(db, run, "model-access:" + receipt.id, required=True)
    if (
        authority.get("contract") != "evolution-model-authority-v1"
        or authority.get("receipt_id") != receipt.id
        or authority.get("request_hash") != receipt.request_hash
        or authority.get("role") != receipt.role
        or access.get("request_hash") != receipt.request_hash
        or authority.get("access_dependencies") != access.get("access_dependencies")
        or authority.get("closure_hash") != access.get("closure_hash")
    ):
        fail()
    funding = db.get(FundingAgreement, run.funding_id)
    if (
        not funding
        or receipt.role not in funding.allowed_roles
        or not set(authority["data_classes"]) <= set(funding.allowed_data_classes)
    ):
        fail("原模型角色或数据授权已变化。")
    assert_source_scope(db, authority["access_dependencies"], funding.allowed_source_ids)
    from .evolution_privacy import validate_run_generations_in

    validate_run_generations_in(db, run, dependencies=authority["access_dependencies"])
    closure = check_closure(db, run.principal_id, authority["access_dependencies"])
    if closure.closure_hash != authority["closure_hash"]:
        fail()
