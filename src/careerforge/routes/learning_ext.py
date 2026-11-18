"""Explicit, private learning actions; no implicit paid calls or external reminders."""

from fastapi import Depends, Query

from ..contracts_learning_events_v166 import (
    LearningIntentInput,
    NavigationAckInput,
    PreferenceEventInput,
)
from ..contracts_learning_ext import (
    ActivityInput,
    AdjustmentInput,
    AttemptInput,
    AttemptSubmission,
    DatasetAttestationInput,
    ObservationInput,
    ObservationWithdraw,
    PersonalizedPlanInput,
    ProfileUpdate,
    ReplayDatasetInput,
    ReplayInput,
    ResearchReplayDatasetInput,
    TraceCaptureInput,
    TypedReplayDatasetInput,
)
from ..contracts_retrieval_replay import RetrievalR3ReplayDatasetInput
from ..contracts_session_plan_v166 import SessionPlanInput


def register_routes(app, service, actor_dependency):
    @app.get("/api/v1/learning/profile")
    def profile(user=Depends(actor_dependency)):
        return service.learning.profile(user)

    @app.put("/api/v1/learning/profile")
    def update_profile(body: ProfileUpdate, user=Depends(actor_dependency)):
        return service.learning.update_profile(user, body)

    @app.post("/api/v1/learning/intent")
    def learning_intent(body: LearningIntentInput, user=Depends(actor_dependency)):
        return service.learning.interpret_learning_intent(user, body)

    @app.post("/api/v1/learning/preference-events", status_code=201)
    def preference_event(body: PreferenceEventInput, user=Depends(actor_dependency)):
        return service.learning.preference_event(user, body)

    @app.get("/api/v1/learning/preference-events")
    def preference_events(user=Depends(actor_dependency)):
        return service.learning.preference_events(user)

    @app.get("/api/v1/learning/effective-preferences")
    def effective_preferences(
        conversation_id: str | None = None,
        topic_keys: list[str] | None = Query(default=None),
        source_ids: list[str] | None = Query(default=None),
        user=Depends(actor_dependency),
    ):
        return service.learning.effective_preferences(
            user, conversation_id=conversation_id, topic_keys=topic_keys, source_ids=source_ids
        )

    @app.post("/api/v1/learning/navigation-acks", status_code=201)
    def navigation_ack(body: NavigationAckInput, user=Depends(actor_dependency)):
        return service.learning.acknowledge_navigation(user, body)

    @app.get("/api/v1/learning/navigation/{block_id}")
    def navigation_status(
        block_id: str,
        citation_index: int = Query(default=0, ge=0, le=100),
        user=Depends(actor_dependency),
    ):
        return service.learning.navigation_status(user, block_id, citation_index)

    @app.post("/api/v1/learning/session-plan-proposals", status_code=202)
    def session_plan(body: SessionPlanInput, user=Depends(actor_dependency)):
        return service.learning.start_session_plan(user, body)

    @app.get("/api/v1/learning/session-plan-gaps/{identifier}")
    def session_plan_gap(identifier: str, user=Depends(actor_dependency)):
        return service.learning.session_plan_gap(user, identifier)

    @app.post("/api/v1/learning/plan-proposals", status_code=202)
    def personalized_plan(body: PersonalizedPlanInput, user=Depends(actor_dependency)):
        return service.learning.start_personalized_plan(user, body)

    @app.post("/api/v1/learning/activities", status_code=201)
    def activity(body: ActivityInput, user=Depends(actor_dependency)):
        return service.learning.create_activity(user, body)

    @app.get("/api/v1/learning/activities/{identifier}")
    def activity_detail(identifier: str, user=Depends(actor_dependency)):
        return service.learning.activity_detail(user, identifier)

    @app.post("/api/v1/learning/activities/{identifier}/attempts", status_code=201)
    def start_attempt(identifier: str, body: AttemptInput, user=Depends(actor_dependency)):
        return service.learning.start_attempt(user, identifier, body)

    @app.post("/api/v1/learning/attempts/{identifier}/submit")
    def submit_attempt(identifier: str, body: AttemptSubmission, user=Depends(actor_dependency)):
        return service.learning.submit_attempt(user, identifier, body)

    @app.get("/api/v1/learning/attempts/{identifier}")
    def attempt_detail(identifier: str, user=Depends(actor_dependency)):
        return service.learning.attempt_detail(user, identifier)

    @app.post("/api/v1/learning/attempts/{identifier}/close")
    def close_attempt(identifier: str, user=Depends(actor_dependency)):
        return service.learning.close_attempt(user, identifier)

    @app.post("/api/v1/learning/attempts/{identifier}/observations", status_code=201)
    def observation(identifier: str, body: ObservationInput, user=Depends(actor_dependency)):
        return service.learning.record_observation(user, identifier, body)

    @app.get("/api/v1/learning/evidence")
    def observations(user=Depends(actor_dependency)):
        return service.learning.observations(user)

    @app.post("/api/v1/learning/evidence/{identifier}/withdraw")
    def withdraw_observation(
        identifier: str, body: ObservationWithdraw, user=Depends(actor_dependency)
    ):
        return service.learning.withdraw_observation(user, identifier, body.expected_revision)

    @app.post("/api/v1/plans/{identifier}/adjustment-proposals", status_code=201)
    def adjustment(identifier: str, body: AdjustmentInput, user=Depends(actor_dependency)):
        return service.learning.propose_adjustment(user, identifier, body)

    @app.get("/api/v1/learning/review-suggestions")
    def review_suggestions(user=Depends(actor_dependency)):
        return service.learning.review_suggestions(user)

    @app.post("/api/v1/evolution/replay-datasets", status_code=201)
    def replay_dataset(body: ReplayDatasetInput, user=Depends(actor_dependency)):
        return service.evolution.register_replay_dataset(user, body)

    @app.post("/api/v1/evolution/research-replay-datasets", status_code=201)
    def research_replay_dataset(body: ResearchReplayDatasetInput, user=Depends(actor_dependency)):
        return service.evolution.register_research_replay_dataset(user, body)

    @app.post("/api/v1/evolution/datasets/{identifier}/attest", status_code=201)
    def attest_dataset(
        identifier: str, body: DatasetAttestationInput, user=Depends(actor_dependency)
    ):
        return service.evolution.attest_dataset(user, identifier, body)

    @app.post("/api/v1/evolution/candidates/{identifier}/replays", status_code=202)
    def replay(identifier: str, body: ReplayInput, user=Depends(actor_dependency)):
        return service.evolution.start_replay(user, identifier, body)

    @app.get("/api/v1/evolution/replays/{identifier}")
    def replay_detail(identifier: str, user=Depends(actor_dependency)):
        return service.evolution.replay_detail(user, identifier)

    @app.post("/api/v1/evolution/trace-feedback", status_code=201)
    def capture_feedback(body: TraceCaptureInput, user=Depends(actor_dependency)):
        return service.evolution.capture_trace_feedback(user, body)

    @app.post("/api/v1/evolution/typed-replay-datasets", status_code=201)
    def typed_replay_dataset(body: TypedReplayDatasetInput, user=Depends(actor_dependency)):
        return service.evolution.register_typed_replay_dataset(user, body)

    @app.post("/api/v1/evolution/r3-retrieval-datasets", status_code=201)
    def r3_retrieval_dataset(body: RetrievalR3ReplayDatasetInput, user=Depends(actor_dependency)):
        return service.evolution.register_r3_retrieval_dataset(user, body)
