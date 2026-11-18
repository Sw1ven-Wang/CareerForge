"""Versioned knowledge scheduling. Checkpoints contain references, never evidence.

Every decision is reconstructed from fenced domain artifacts. Interrupts are
persisted before the adapter releases the parent lease; children remain ordinary
independently funded Analysis/Review runs.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..errors import DomainError
from ..services.build_execution import EVIDENCE_RETRY_CONTRACT
from ..services.mechanism_discovery import DISCOVERY_STAGE, child_discovery_manifest
from ..services.runtime import TERMINAL
from .events import project_event
from .registry import validate_binding
from .state import CareerForgeState

STAGES = {
    "validate",
    "goal_topics",
    "sources",
    "dispatch_analysis",
    "wait_analysis",
    "validate_analysis",
    "dispatch_review",
    "wait_review",
    "validate_review",
    "coverage",
    "prepare_retry",
    "dispatch_retry_analysis",
    "wait_retry_analysis",
    "validate_retry_analysis",
    "dispatch_retry_review",
    "wait_retry_review",
    "validate_retry_review",
    "finalize",
    "completed",
}


class KnowledgeBuildGraph:
    def __init__(self, app, run, epoch, worker):
        self.app, self.run, self.epoch, self.worker = app, run, epoch, worker
        self.build, self.runtime = app.build, app.runtime

    def fence(self, state=None):
        self.runtime.validate(self.run.id, self.epoch, self.worker)
        validate_binding(self.run)
        self.build.validate_root(self.run)
        if state is not None and (
            state.get("workflow_run_id"),
            state.get("graph_version"),
            state.get("state_schema_version"),
        ) != (self.run.id, self.run.graph_version, self.run.state_schema_version):
            raise DomainError("GRAPH_STATE", "Checkpoint 与冻结任务合同不兼容。", 409)

    def stage(self, key, required=True):
        value = self.runtime.stage(self.run.id, key)
        if value is None and required:
            raise DomainError("GRAPH_RECONCILIATION", "缺少业务阶段记录。", 409)
        return value

    def context(self):
        context = self.stage("research_context", required=False) or {}
        discovery = self.stage(DISCOVERY_STAGE, required=False)
        return (
            self.stage("sources"),
            context,
            child_discovery_manifest(discovery) if discovery else {},
        )

    def child(self, key):
        child = self.build._child(self.stage(key)["run_id"])
        # A checkpoint/reference cannot substitute another parent's child.
        if (
            child is None
            or child.parent_id != self.run.id
            or child.namespace_id != self.run.namespace_id
            or child.principal_id != self.run.principal_id
            or child.workflow != key.split("_", 1)[0]
        ):
            raise DomainError("GRAPH_RECONCILIATION", "子任务范围不匹配。", 409)
        return child

    def initial_results(self):
        analysis, review = self.child("analysis_child"), self.child("review_child")
        if any(c.status not in {"succeeded", "partially_succeeded"} for c in (analysis, review)):
            raise DomainError("GRAPH_RECONCILIATION", "独立分析或审核尚未成功。", 409)
        adata = self.runtime.stage(analysis.id, "result")
        if not adata or not adata.get("unit_version_ids"):
            raise DomainError("GRAPH_RECONCILIATION", "分析结果缺失。", 409)
        return adata, review, self.runtime.stage(review.id, "result") or {"review_ids": []}

    def retry_outcome(self):
        initial = self.stage("coordinator.initial_coverage", required=False)
        if initial and initial["complete"]:
            return {"status": "initial_reviewed_coverage_sufficient"}
        if self.run.input_manifest.get("evidence_retry_contract") != EVIDENCE_RETRY_CONTRACT:
            return {}
        plan = self.stage("evidence_retry_plan")
        outcome = {"plan": plan, "status": "no_new_read_target"}
        if not plan["retry_targets"]:
            return outcome
        for role in ("analysis", "review"):
            state = self.child(role + "_retry_child")
            if state.status not in TERMINAL:
                raise DomainError("GRAPH_RECONCILIATION", "补证子任务仍在执行。", 409)
            outcome = self.build.read_retry_outcome(state, role, outcome)
            if outcome["status"] in {"analysis_incomplete", "review_incomplete"}:
                return outcome
        return outcome

    def reconcile(self):
        self.fence()
        result = self.stage("result")
        for key in ("publication_ids", "gaps"):
            value = result.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise DomainError("GRAPH_RECONCILIATION", "业务结果结构无效。", 409)
        prepared = self.stage("sources")
        failure_gap = None
        if not prepared["source_ids"]:
            failure_gap = "no_sources"
        else:
            analysis = self.child("analysis_child")
            adata = self.runtime.stage(analysis.id, "result")
            if analysis.status not in TERMINAL:
                raise DomainError("GRAPH_RECONCILIATION", "分析仍在执行。", 409)
            if (
                analysis.status not in {"succeeded", "partially_succeeded"}
                or not adata
                or not adata.get("unit_version_ids")
            ):
                failure_gap = (
                    "analysis_no_units"
                    if analysis.status in {"succeeded", "partially_succeeded"} and adata is not None
                    else "analysis_failed"
                )
            else:
                review = self.child("review_child")
                if review.status not in TERMINAL:
                    raise DomainError("GRAPH_RECONCILIATION", "审核仍在执行。", 409)
                if review.status not in {"succeeded", "partially_succeeded"}:
                    failure_gap = "review_batch_incomplete"
        if failure_gap:
            if result["publication_ids"] or failure_gap not in result["gaps"]:
                raise DomainError("GRAPH_RECONCILIATION", "未完成业务结果不匹配。", 409)
            return
        adata, _, rdata = self.initial_results()
        retry = self.retry_outcome()
        review_ids = [*rdata["review_ids"], *retry.get("reviews", {}).get("review_ids", [])]
        unit_ids = [
            *adata["unit_version_ids"],
            *retry.get("analysis", {}).get("unit_version_ids", []),
        ]
        if result.get("review_ids") != review_ids or result.get("unit_version_ids") != unit_ids:
            raise DomainError("GRAPH_RECONCILIATION", "业务结果与分析审核不匹配。", 409)
        # Re-run domain admission for every expected review, not just publication
        # IDs claimed by the checkpoint/result. Missing IDs cannot hide revocation.
        accepted = []
        for review_id in review_ids:
            pub = self.build.knowledge.publish(
                self.run.principal_id, self.run.id, self.epoch, self.worker, review_id
            )
            if pub:
                accepted.append(pub)
        if result["publication_ids"] != accepted:
            raise DomainError("GRAPH_RECONCILIATION", "发布结果与独立审核不匹配。", 409)

    def graph(self, saver):
        run, epoch, worker = self.run, self.epoch, self.worker
        build = self.build

        async def validate():
            pass

        async def goal_topics():
            await build.prepare_goal_topics(run, epoch, worker)

        async def sources():
            topics, context = await build.prepare_goal_topics(run, epoch, worker)
            prepared = await build.prepare_sources(run, epoch, worker, topics, context)
            build.finish_no_sources(run, epoch, worker, prepared)

        async def dispatch_analysis():
            prepared, context, _ = self.context()
            await build.ensure_analysis_child(run, epoch, worker, prepared, context)

        def waiting(key):
            async def wait():
                while self.child(key).status not in TERMINAL:
                    interrupt({"kind": "waiting_children", "child_run_id": self.child(key).id})
                    # Resume data is only a wake signal, not success evidence.

            return wait

        async def validate_analysis():
            build.read_analysis_outcome(run, epoch, worker, self.child("analysis_child"))

        async def dispatch_review():
            prepared, context, discovery = self.context()
            child = self.child("analysis_child")
            if child.status not in {"succeeded", "partially_succeeded"}:
                raise DomainError("GRAPH_RECONCILIATION", "分析尚未成功。", 409)
            data = self.runtime.stage(child.id, "result")
            if not data or not data.get("unit_version_ids"):
                raise DomainError("GRAPH_RECONCILIATION", "分析结果缺失。", 409)
            build.ensure_review_child(run, epoch, worker, data, context, discovery)

        async def validate_review():
            prepared, _, _ = self.context()
            adata = self.runtime.stage(self.child("analysis_child").id, "result")
            build.read_review_outcome(
                run, epoch, worker, prepared, adata, self.child("review_child")
            )

        async def coverage():
            _, context, _ = self.context()
            adata, _, rdata = self.initial_results()
            build.evaluate_initial_coverage(run, epoch, worker, context, adata, rdata)

        def retry_needed():
            initial = self.stage("coordinator.initial_coverage", required=False)
            return run.input_manifest.get(
                "evidence_retry_contract"
            ) == EVIDENCE_RETRY_CONTRACT and not (initial and initial["complete"])

        async def prepare_retry():
            prepared, context, discovery = self.context()
            adata, reviewer, review = self.initial_results()
            build.prepare_evidence_retry(
                run,
                epoch,
                worker,
                prepared,
                context,
                adata,
                reviewer,
                review,
                discovery,
                self.stage("coordinator.initial_coverage", required=False),
            )

        def dispatch_retry(role):
            async def dispatch():
                self.initial_results()
                if not retry_needed():
                    raise DomainError("GRAPH_RECONCILIATION", "补证未获授权。", 409)
                prepared, context, discovery = self.context()
                analysis = None
                if role == "review":
                    child = self.child("analysis_retry_child")
                    analysis = self.runtime.stage(child.id, "result")
                    if (
                        child.status != "succeeded"
                        or not analysis
                        or not analysis.get("unit_version_ids")
                    ):
                        raise DomainError("GRAPH_RECONCILIATION", "补证分析尚未成功。", 409)
                build.ensure_retry_child(
                    run,
                    epoch,
                    worker,
                    prepared,
                    context,
                    discovery,
                    self.stage("evidence_retry_plan"),
                    role,
                    analysis,
                )

            return dispatch

        async def validate_retry_analysis():
            if self.child("analysis_retry_child").status not in TERMINAL:
                raise DomainError("GRAPH_RECONCILIATION", "补证分析未完成。", 409)

        async def validate_retry_review():
            self.retry_outcome()

        async def finalize():
            prepared, _, _ = self.context()
            adata, rstate, rdata = self.initial_results()
            await build.publish_and_finalize(
                run, epoch, worker, prepared, adata, rstate, rdata, self.retry_outcome()
            )

        async def completed():
            self.reconcile()

        nodes = {
            "validate": validate,
            "goal_topics": goal_topics,
            "sources": sources,
            "dispatch_analysis": dispatch_analysis,
            "wait_analysis": waiting("analysis_child"),
            "validate_analysis": validate_analysis,
            "dispatch_review": dispatch_review,
            "wait_review": waiting("review_child"),
            "validate_review": validate_review,
            "coverage": coverage,
            "prepare_retry": prepare_retry,
            "dispatch_retry_analysis": dispatch_retry("analysis"),
            "wait_retry_analysis": waiting("analysis_retry_child"),
            "validate_retry_analysis": validate_retry_analysis,
            "dispatch_retry_review": dispatch_retry("review"),
            "wait_retry_review": waiting("review_retry_child"),
            "validate_retry_review": validate_retry_review,
            "finalize": finalize,
            "completed": completed,
        }
        builder = StateGraph(CareerForgeState)
        for name, operation in nodes.items():

            async def node(state, operation=operation, name=name):
                self.fence(state)
                project_event(self.app, run.id, epoch, worker, name, "started")
                await operation()
                self.fence(state)
                project_event(self.app, run.id, epoch, worker, name, "completed")
                return {
                    "current_stage": name,
                    "result_stage_key": "result" if name == "completed" else "",
                }

            builder.add_node(name, node)
        builder.add_edge(START, "validate")
        builder.add_edge("validate", "goal_topics")
        builder.add_edge("goal_topics", "sources")
        for source, target in (
            ("sources", "dispatch_analysis"),
            ("validate_analysis", "dispatch_review"),
            ("validate_review", "coverage"),
        ):
            builder.add_conditional_edges(
                source,
                lambda state, target=target: (
                    "completed" if self.stage("result", False) is not None else target
                ),
                ["completed", target],
            )
        for role in ("analysis", "review"):
            builder.add_edge("dispatch_" + role, "wait_" + role)
            builder.add_edge("wait_" + role, "validate_" + role)
            builder.add_edge("dispatch_retry_" + role, "wait_retry_" + role)
            builder.add_edge("wait_retry_" + role, "validate_retry_" + role)
        builder.add_conditional_edges(
            "coverage",
            lambda state: "prepare_retry" if retry_needed() else "finalize",
            ["prepare_retry", "finalize"],
        )
        builder.add_conditional_edges(
            "prepare_retry",
            lambda state: (
                "dispatch_retry_analysis"
                if self.stage("evidence_retry_plan")["retry_targets"]
                else "finalize"
            ),
            ["dispatch_retry_analysis", "finalize"],
        )

        def analysis_retry_route(state):
            child = self.child("analysis_retry_child")
            outcome = build.read_retry_outcome(child, "analysis", {"status": "no_new_read_target"})
            return (
                "finalize"
                if outcome["status"] == "analysis_incomplete"
                else "dispatch_retry_review"
            )

        builder.add_conditional_edges(
            "validate_retry_analysis", analysis_retry_route, ["finalize", "dispatch_retry_review"]
        )
        builder.add_edge("validate_retry_review", "finalize")
        builder.add_edge("finalize", "completed")
        builder.add_edge("completed", END)
        return builder.compile(checkpointer=saver)
