"""True candidate and per-arm replay stages; checkpoints contain references only."""

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from ..models import AgentRun, ModelReceipt
from ..services import evolution_stages as stages
from ..services.runtime import leased
from ..util import fingerprint
from .state import CareerForgeState


class EvolutionState(CareerForgeState, total=False):
    arm_index: int
    arm_name: str


class EvolutionPaidGraph:
    def __init__(self, app, run, epoch, worker):
        self.app, self.run, self.epoch, self.worker = app, run, epoch, worker

    def fence(self, state, *, active=True):
        if tuple(
            state.get(k) for k in ("workflow_run_id", "graph_version", "state_schema_version")
        ) != (self.run.id, self.run.graph_version, 1):
            stages.fail("检查点与冻结付费运行不匹配。")
        self.app.runtime.validate(self.run.id, self.epoch, self.worker)
        with self.app.db.read() as db:
            current = db.get(AgentRun, self.run.id)
            stages.validate_run_in(self.app, db, current, active=active)

    def result_exists(self):
        with self.app.db.read() as db:
            return stages.stage_in(db, self.run, "result") is not None

    def reconcile(self):
        return stages.reconcile(self.app, self.run, self.epoch, self.worker)

    def graph(self, saver):
        app, run, epoch, worker = self.app, self.run, self.epoch, self.worker

        async def validate(state):
            self.fence(state, active=not self.result_exists())
            if self.result_exists():
                self.reconcile()
            return {"current_stage": "validate"}

        async def finish(state):
            self.fence(state, active=False)
            self.reconcile()
            return {"current_stage": "completed", "result_stage_key": "result"}

        builder = StateGraph(EvolutionState)
        builder.add_node("validate", validate)
        builder.add_node("finish", finish)
        builder.add_edge(START, "validate")
        builder.add_edge("finish", END)
        if run.workflow == "evolution":

            async def invoke(state):
                self.fence(state)
                if not self.result_exists():
                    await app.evolution.invoke_reflection(run, epoch, worker)
                return {"current_stage": "invoke_reflection"}

            async def validate_patch(state):
                self.fence(state)
                if not self.result_exists():
                    # Exact gateway reconstruction verifies request hash and live
                    # authority without ever reserving or sending another call.
                    output = await app.evolution.invoke_reflection(
                        run, epoch, worker, replay_only=True
                    )
                    app.evolution.validate_reflection_patch(run, output)
                    with app.db.transaction() as db:
                        current = leased(db, run.id, epoch, worker)
                        stages.validate_run_in(app, db, current)
                        receipt = db.scalar(
                            select(ModelReceipt).where(
                                ModelReceipt.run_id == run.id,
                                ModelReceipt.stage_key == "reflection",
                            )
                        )
                        if not receipt or receipt.status != "completed" or receipt.response is None:
                            stages.fail()
                        app.runtime.save_stage_in(
                            db,
                            current,
                            "reflection.validated.v1",
                            {
                                "receipt_id": receipt.id,
                                "request_hash": receipt.request_hash,
                                "response_hash": fingerprint(receipt.response),
                            },
                        )
                return {"current_stage": "validate_patch"}

            async def commit(state):
                self.fence(state)
                if not self.result_exists():
                    from ..contracts import ModelOutput

                    with app.db.read() as db:
                        witness = stages.stage_in(db, run, "reflection.validated.v1", required=True)
                        receipt = db.get(ModelReceipt, witness["receipt_id"])
                        if not receipt or fingerprint(receipt.response) != witness["response_hash"]:
                            stages.fail()
                        patch = app.evolution.validate_reflection_patch(
                            run, ModelOutput.model_validate(receipt.response)
                        )
                    app.evolution.commit_reflection(run, epoch, worker, patch)
                self.reconcile()
                return {"current_stage": "commit_candidate"}

            for name, fn in (
                ("invoke_reflection", invoke),
                ("validate_patch", validate_patch),
                ("commit_candidate", commit),
            ):
                builder.add_node(name, fn)
            builder.add_conditional_edges(
                "validate",
                lambda state: "finish" if self.result_exists() else "invoke_reflection",
                ["finish", "invoke_reflection"],
            )
            builder.add_edge("invoke_reflection", "validate_patch")
            builder.add_edge("validate_patch", "commit_candidate")
            builder.add_edge("commit_candidate", "finish")
        else:

            def selected(state):
                index, arm = state.get("arm_index"), state.get("arm_name")
                stages.arm_binding(run, index, arm)
                with app.db.read() as db:
                    prior = stages.arm_result_in(app, db, run, index, arm)
                if prior is None and stages.next_arm(app, run) != (index, arm):
                    stages.fail("检查点臂游标不能跳过领域结果。")
                return index, arm

            async def select_arm(state):
                self.fence(state)
                next_pair = stages.next_arm(app, run)
                return {
                    "current_stage": "select_arm",
                    "arm_index": next_pair[0] if next_pair else -1,
                    "arm_name": next_pair[1] if next_pair else "",
                }

            async def execute_arm(state):
                self.fence(state)
                index, arm = selected(state)
                with app.db.read() as db:
                    existing = stages.arm_execution_in(db, run, index, arm)
                if existing is None:
                    output = await app.evolution.execute_replay_arm(run, epoch, worker, index, arm)
                    with app.db.transaction() as db:
                        current = leased(db, run.id, epoch, worker)
                        stages.validate_run_in(app, db, current)
                        # Source erasure scans these direct dependency references,
                        # even when generated prose contains no incidental IDs.
                        from ..models import ObjectRow

                        refs = set(run.input_manifest.get("policy_dependencies", []))
                        refs.add(run.input_manifest["selection_manifest_id"])

                        def collect(value):
                            if (
                                isinstance(value, str)
                                and len(value) == 36
                                and db.get(ObjectRow, value)
                            ):
                                refs.add(value)
                            elif isinstance(value, dict):
                                for item in value.values():
                                    collect(item)
                            elif isinstance(value, list):
                                for item in value:
                                    collect(item)

                        collect(run.input_manifest["cases"][index])
                        from ..services.evolution_privacy import validate_run_generations_in

                        validate_run_generations_in(db, current, dependencies=sorted(refs))
                        stages.receipts_in(db, current, output, f"replay:{index}:{arm}:")
                        app.runtime.save_stage_in(
                            db,
                            current,
                            f"replay:{index}:{arm}:execution.v1",
                            {
                                "contract": "evolution-arm-execution-v1",
                                "binding": stages.arm_binding(current, index, arm),
                                "access_dependencies": sorted(refs),
                                "execution": output,
                                "execution_hash": fingerprint(output),
                            },
                        )
                return {"current_stage": "execute_arm"}

            async def score_arm(state):
                self.fence(state)
                index, arm = selected(state)
                with app.db.transaction() as db:
                    current = leased(db, run.id, epoch, worker)
                    stages.validate_run_in(app, db, current)
                    output = stages.arm_execution_in(db, current, index, arm, required=True)
                    trace = app.evolution.score_replay_arm(current, index, arm, output)
                    app.runtime.save_stage_in(db, current, f"replay:{index}:{arm}:result", trace)
                return {"current_stage": "score_arm"}

            async def aggregate(state):
                self.fence(state)
                with app.db.transaction() as db:
                    current = leased(db, run.id, epoch, worker)
                    stages.validate_run_in(app, db, current)
                    traces, samples = stages.paired_inputs(app, db, current)
                    report = app.evolution.build_replay_report(current, traces, samples)
                    # Only hashes/references are needed between aggregation and
                    # commit; scoring data remains in private arm artifacts.
                    app.runtime.save_stage_in(
                        db,
                        current,
                        "replay.aggregate.v1",
                        {"trace_hash": fingerprint(traces), "report_hash": fingerprint(report)},
                    )
                return {"current_stage": "aggregate"}

            async def commit_evaluation(state):
                self.fence(state)
                if not self.result_exists():
                    with app.db.read() as db:
                        traces, samples = stages.paired_inputs(app, db, run)
                        report = app.evolution.build_replay_report(run, traces, samples)
                        witness = stages.stage_in(db, run, "replay.aggregate.v1", required=True)
                        if witness != {
                            "trace_hash": fingerprint(traces),
                            "report_hash": fingerprint(report),
                        }:
                            stages.fail()
                    app.evolution.commit_replay_evaluation(run, epoch, worker, traces, report)
                self.reconcile()
                return {"current_stage": "commit_evaluation"}

            for name, fn in (
                ("select_arm", select_arm),
                ("execute_arm", execute_arm),
                ("score_arm", score_arm),
                ("aggregate", aggregate),
                ("commit_evaluation", commit_evaluation),
            ):
                builder.add_node(name, fn)
            builder.add_conditional_edges(
                "validate",
                lambda state: "finish" if self.result_exists() else "select_arm",
                ["finish", "select_arm"],
            )
            builder.add_conditional_edges(
                "select_arm",
                lambda state: "aggregate" if state["arm_index"] == -1 else "execute_arm",
                ["aggregate", "execute_arm"],
            )
            for before, after in (
                ("execute_arm", "score_arm"),
                ("score_arm", "select_arm"),
                ("aggregate", "commit_evaluation"),
                ("commit_evaluation", "finish"),
            ):
                builder.add_edge(before, after)
        return builder.compile(checkpointer=saver)
