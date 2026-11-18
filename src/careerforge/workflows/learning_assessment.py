"""Real assessment stages; only references and stage names enter checkpoints."""

from langgraph.graph import END, START, StateGraph

from ..errors import DomainError
from ..models import AgentRun
from .state import CareerForgeState

GRAPH_VERSION = "learning-assessment-v1"
STAGES = ("validate_frozen_input", "evaluate_candidate", "persist_candidate", "finish")


class LearningAssessmentGraph:
    def __init__(self, app, run, epoch, worker):
        self.app, self.run, self.epoch, self.worker = app, run, epoch, worker

    def fence(self, state):
        run = self.run
        if (
            run.workflow != "learning_assessment"
            or run.graph_version != GRAPH_VERSION
            or run.state_schema_version != 1
            or (
                state.get("workflow_run_id"),
                state.get("graph_version"),
                state.get("state_schema_version"),
            )
            != (run.id, GRAPH_VERSION, 1)
        ):
            raise DomainError("GRAPH_STATE", "评价检查点身份不一致。", 409)
        self.app.runtime.validate(run.id, self.epoch, self.worker)
        self.app.assessments.prepare_candidate(run)

    def reconcile(self):
        self.app.runtime.validate(self.run.id, self.epoch, self.worker)
        result = self.app.runtime.stage(self.run.id, "result")
        if result is None:
            raise DomainError("GRAPH_RECONCILIATION", "评价候选缺少真实领域结果。", 409)
        with self.app.db.read() as db:
            current = db.get(AgentRun, self.run.id)
            if current is None:
                raise DomainError("GRAPH_RUN", "评价运行不存在。", 404)
            return self.app.assessments.validate_result(db, current.principal_id, current, result)

    def graph(self, saver):
        async def validate(state):
            self.fence(state)
            return {"current_stage": "validate_frozen_input"}

        async def evaluate(state):
            self.fence(state)
            await self.app.assessments.evaluate_candidate(self.run, self.epoch, self.worker)
            self.fence(state)
            return {"current_stage": "evaluate_candidate"}

        async def persist(state):
            self.fence(state)
            self.app.assessments.persist_candidate(self.run, self.epoch, self.worker)
            self.fence(state)
            return {"current_stage": "persist_candidate"}

        async def finish(state):
            self.fence(state)
            self.reconcile()
            return {"current_stage": "completed", "result_stage_key": "result"}

        builder = StateGraph(CareerForgeState)
        for name, node in zip(STAGES, (validate, evaluate, persist, finish), strict=True):
            builder.add_node(name, node)
        builder.add_edge(START, STAGES[0])
        for before, after in zip(STAGES, STAGES[1:], strict=False):
            builder.add_edge(before, after)
        builder.add_edge(STAGES[-1], END)
        return builder.compile(checkpointer=saver)
