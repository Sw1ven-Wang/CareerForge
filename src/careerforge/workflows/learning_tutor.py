"""The actual paid per-Turn graph, not a wrapper around Conversation.process."""

from langgraph.graph import END, START, StateGraph

from ..errors import DomainError
from ..models import AgentRun
from ..services.session_turn import GRAPH_VERSION, SessionTurnStages, has_delivery_in
from .state import CareerForgeState

STAGES = (
    "validate_turn_and_load_module",
    "retrieve_reviewed_evidence",
    "tutor_checked_stream",
    "answer_completion_guard",
    "commit_answer",
    "finalize_turn_result",
)


class LearningTutorGraph:
    def __init__(self, app, run, epoch, worker):
        self.app, self.run, self.lease = app, run, (run.id, epoch, worker)
        self.stages = SessionTurnStages(app, run, epoch, worker)

    def delivered(self):
        with self.app.db.read() as db:
            return has_delivery_in(db, db.get(AgentRun, self.run.id))

    def fence(self, state):
        if (
            state.get("workflow_run_id"),
            state.get("graph_version"),
            state.get("state_schema_version"),
        ) != (self.run.id, GRAPH_VERSION, 1):
            raise DomainError("GRAPH_STATE", "导师 Checkpoint 与固定任务不一致。", 409)
        self.app.runtime.validate_execution(*self.lease)

    def reconcile(self):
        return self.stages.reconcile()

    def graph(self, saver, *, finalization_only=False):
        async def load(state):
            self.fence(state)
            if not self.delivered():
                self.stages.load()
            return {"current_stage": STAGES[0]}

        async def retrieve(state):
            self.fence(state)
            if not self.delivered():
                self.stages.retrieve()
            return {"current_stage": STAGES[1]}

        async def stream(state):
            self.fence(state)
            if not self.delivered():
                await self.stages.stream()
            return {"current_stage": STAGES[2]}

        async def completion(state):
            self.fence(state)
            if not self.delivered():
                await self.stages.complete()
            return {"current_stage": STAGES[3]}

        async def deliver(state):
            self.fence(state)
            if not self.delivered():
                self.stages.deliver()
            return {"current_stage": STAGES[4]}

        async def finalize(state):
            self.fence(state)
            self.stages.finalize()
            self.stages.reconcile()
            return {"current_stage": "completed", "result_stage_key": "result"}

        builder = StateGraph(CareerForgeState)
        # Keep every registered channel present when recording recovery END.
        # Removing old node channels can leave a stale paid task visible to the
        # normal topology. Every paid callback also checks the server witness,
        # so a previously scheduled task can never regenerate delivered bytes.
        for name, callback in zip(
            STAGES, [load, retrieve, stream, completion, deliver, finalize], strict=True
        ):
            builder.add_node(name, callback)
        builder.add_conditional_edges(
            START,
            lambda state: STAGES[-1] if finalization_only or self.delivered() else STAGES[0],
            [STAGES[0], STAGES[-1]],
        )
        for index, name in enumerate(STAGES[:-1]):
            following = STAGES[index + 1]
            builder.add_conditional_edges(
                name,
                lambda state, next_stage=following: STAGES[-1] if self.delivered() else next_stage,
                list(dict.fromkeys([following, STAGES[-1]])),
            )
        builder.add_edge(STAGES[-1], END)
        return builder.compile(checkpointer=saver)
