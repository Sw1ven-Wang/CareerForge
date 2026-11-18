"""Explicit, budgeted requests for provisional model rubric assessment."""

from fastapi import Depends

from ..contracts_assessment_v166 import AssessmentInput


def register_routes(app, service, actor_dependency):
    @app.post("/api/v1/learning/assessments", status_code=202)
    def assessment(body: AssessmentInput, user=Depends(actor_dependency)):
        return service.assessments.start(user, body)
