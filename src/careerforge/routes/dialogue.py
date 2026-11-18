"""Additive dialogue control HTTP surface. Existing Turn and Run endpoints remain authoritative."""

import asyncio
import json

from fastapi import Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..contracts_conversation_actions_v1 import (
    DialogueMessage,
    GoalRevisionInput,
    WorkflowControl,
    WorkflowFocus,
)
from ..contracts_conversation_actions_v2 import GoalAmendmentInput
from ..contracts_conversation_actions_v3 import (
    LearningDialogueMessageV1,
    LearningPlanConfirmationV1,
    LearningSessionAdmissionV1,
    ModuleSelectionV1,
    TutorRequestAuthorizationV1,
)
from ..contracts_practice_dialogue_v1 import PracticeDialogueMessageV1
from ..errors import DomainError
from ..models_dialogue import WorkflowSession


def _read_activity(service, user, identifier, cursor):
    rows = service.workflows.activity(user, identifier, cursor)
    turns = {}
    for row in rows:
        ref = row["reference"]
        turn_id = ref.get("turn_id")
        if not turn_id or "source_seq" not in ref:
            continue
        if turn_id not in turns:
            try:
                events = service.answers.events(user, turn_id, ref["source_seq"] - 1)["events"]
            except DomainError:
                events = []
            turns[turn_id] = {event["seq"]: event for event in events}
        event = turns[turn_id].get(ref["source_seq"])
        if event:
            row["kind"] = event["type"]
            row["answer_event"] = event
    return rows


def register_dialogue_routes(app, service, actor):
    from ..contracts_learning_language import (
        ImplementationLanguageInput,
        KnowledgeImplementationLinkInput,
    )

    @app.get("/api/v1/workflows/{identifier}/projects")
    def project_pool(identifier: str, include_pending: bool = False, user=Depends(actor)):
        from ..services.project_pool import ProjectPoolService

        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            return {
                "projects": ProjectPoolService(service).list_in(
                    db, user, workflow.goal_version_id, include_pending=include_pending
                ),
                "knowledge_publications_created": False,
            }

    @app.get("/api/v1/projects/{identifier}/quality")
    def project_quality(identifier: str, user=Depends(actor)):
        from ..services.dependencies import check_closure, require_object

        with service.db.read() as db:
            entry = require_object(db, user, identifier, "project_pool_entry")
            check_closure(db, user, [identifier])
            return require_object(
                db, user, entry.payload["quality_report_id"], "project_quality_report"
            ).payload

    @app.post("/api/v1/workflows/{identifier}/implementation-language", status_code=202)
    def implementation_language(
        identifier: str, body: ImplementationLanguageInput, user=Depends(actor)
    ):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
        message = DialogueMessage(
            text="/language " + ",".join(body.languages),
            expected_focus_revision=body.expected_focus_revision,
            idempotency_key=body.idempotency_key,
        )
        return service.dialogue.dispatch(
            user,
            workflow.conversation_id,
            message,
            forced_actions=[("select_implementation_language", {})],
            workflow_id=identifier,
            expected_workflow_revision=body.expected_revision,
        )

    @app.post("/api/v1/knowledge/implementation-links", status_code=201)
    def implementation_link(body: KnowledgeImplementationLinkInput, user=Depends(actor)):
        from ..services.knowledge_implementations import create_link_in

        with service.db.transaction() as db:
            return create_link_in(service, db, user, body)

    @app.get("/api/v1/workflows/{identifier}/knowledge-materials")
    def language_materials(identifier: str, user=Depends(actor)):
        from ..services.knowledge_implementations import linked_materials_in
        from ..services.learning_language import read_profile_in

        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            profile = read_profile_in(db, user, workflow)
            if profile is None or profile["status"] == "awaiting_user_choice":
                raise DomainError("LANGUAGE_CHOICE_REQUIRED", "请先确认实现语言。", 409)
            return {
                "materials": linked_materials_in(service, db, user, profile),
                "architecture_language_constraint": None,
            }

    @app.get("/api/v1/conversations/{identifier}/practices/{practice_id}")
    def practice_detail(identifier: str, practice_id: str, user=Depends(actor)):
        return service.practice_workflows.detail(user, identifier, practice_id)

    @app.post("/api/v1/conversations/{identifier}/learning-messages", status_code=202)
    async def learning_message(
        identifier: str, body: LearningDialogueMessageV1, user=Depends(actor)
    ):
        return await asyncio.to_thread(service.dialogue.dispatch, user, identifier, body)

    @app.post("/api/v1/conversations/{identifier}/learning-sessions", status_code=202)
    def learning_admit(identifier: str, body: LearningSessionAdmissionV1, user=Depends(actor)):
        message = LearningDialogueMessageV1(
            text="start_learning_session",
            expected_focus_revision=body.expected_focus_revision,
            idempotency_key=body.idempotency_key,
            authorization_ref=body.authorization_ref,
            tutor_request=body.tutor_request,
        )
        return service.dialogue.dispatch(
            user,
            identifier,
            message,
            forced_actions=[("start_learning_session", {})],
            learning_input=body,
        )

    @app.post("/api/v1/workflows/{identifier}/tutor-authorizations", status_code=202)
    def tutor_authorize(identifier: str, body: TutorRequestAuthorizationV1, user=Depends(actor)):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            conv = service.workflows.conversation(db, user, workflow.conversation_id)
        message = LearningDialogueMessageV1(
            text="authorize_tutor_request",
            expected_focus_revision=conv.revision,
            idempotency_key=body.idempotency_key,
            authorization_ref=body.authorization_ref,
        )
        return service.dialogue.dispatch(
            user,
            conv.id,
            message,
            forced_actions=[("authorize_tutor_request", {})],
            workflow_id=identifier,
            expected_workflow_revision=body.expected_revision,
            learning_input=body,
        )

    @app.post("/api/v1/workflows/{identifier}/module-selection", status_code=202)
    def module_select(identifier: str, body: ModuleSelectionV1, user=Depends(actor)):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
        message = LearningDialogueMessageV1(
            text="select_learning_module",
            expected_focus_revision=body.expected_focus_revision,
            idempotency_key=body.idempotency_key,
        )
        return service.dialogue.dispatch(
            user,
            workflow.conversation_id,
            message,
            forced_actions=[("select_learning_module", {})],
            workflow_id=identifier,
            expected_workflow_revision=body.expected_revision,
            learning_input=body,
        )

    @app.post("/api/v1/workflows/{identifier}/learning-confirmations/{version_id}", status_code=202)
    def learning_confirm(
        identifier: str, version_id: str, body: LearningPlanConfirmationV1, user=Depends(actor)
    ):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if not workflow or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            if workflow.target.get("proposal", {}).get("version_id") != version_id:
                raise DomainError("CONFIRMATION_STALE", "待确认版本已变化。", 409)
            conv = service.workflows.conversation(db, user, workflow.conversation_id)
        message = LearningDialogueMessageV1(
            text="accept_plan",
            expected_focus_revision=conv.revision,
            idempotency_key=body.idempotency_key,
            authorization_ref=body.authorization_ref,
            tutor_request=body.tutor_request,
        )
        return service.dialogue.dispatch(
            user,
            conv.id,
            message,
            forced_actions=[("accept_plan", {}), ("start_tutor", {})],
            workflow_id=identifier,
            expected_workflow_revision=body.expected_revision,
            learning_input=body,
        )

    @app.get("/api/v1/conversations/{identifier}/tutor-requests/{request_id}")
    def tutor_request(identifier: str, request_id: str, user=Depends(actor)):
        from ..models_dialogue import WorkflowTutorRequest

        with service.db.read() as db:
            service.workflows.conversation(db, user, identifier)
            row = db.get(WorkflowTutorRequest, request_id)
            if not row or row.conversation_id != identifier:
                raise DomainError("NOT_FOUND", "学习请求不存在。", 404)
            return {
                "request_id": row.id,
                "request_hash": row.request_hash,
                "workflow_id": row.workflow_id,
                "execution_id": row.execution_id,
                "sequence": row.sequence,
                "status": row.status,
                "failure_code": row.failure_code,
                "turn_id": row.turn_id,
                "run_id": row.run_id,
            }

    @app.post("/api/v1/conversations/{identifier}/messages", status_code=202)
    async def message(
        identifier: str, body: PracticeDialogueMessageV1 | DialogueMessage, user=Depends(actor)
    ):
        result = await asyncio.to_thread(service.dialogue.dispatch, user, identifier, body)
        turn_id = result.get("turn_id")
        if turn_id and not result.get("run_id"):
            if service.answers.events(user, turn_id)["status"] == "queued":
                try:
                    await service.conversations.process(turn_id)
                except DomainError as exc:
                    service.conversations.mark_failed(turn_id, exc.code)
                    raise
        return result

    def workflow_action(
        identifier,
        body,
        user,
        action,
        *,
        additional_actions=None,
        command_source=None,
        legacy_goal_revision=False,
        **args,
    ):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if workflow is None or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            conv = service.workflows.conversation(db, user, workflow.conversation_id)
            cid, revision = conv.id, conv.revision
        return service.dialogue.dispatch(
            user,
            cid,
            DialogueMessage(
                text=action,
                expected_focus_revision=revision,
                idempotency_key=body.idempotency_key,
                authorization_ref=body.authorization_ref,
            ),
            forced_actions=[(action, args), *(additional_actions or [])],
            workflow_id=identifier,
            expected_workflow_revision=body.expected_revision,
            expected_goal=getattr(body, "source_goal_version_id", None),
            expected_goal_revision=getattr(body, "expected_goal_revision", None),
            command_source=command_source,
            legacy_goal_revision=legacy_goal_revision,
        )

    @app.post("/api/v1/workflows/{identifier}/pause", status_code=202)
    def pause(identifier: str, body: WorkflowControl, user=Depends(actor)):
        return workflow_action(identifier, body, user, "request_pause")

    @app.post("/api/v1/workflows/{identifier}/resume", status_code=202)
    def resume(identifier: str, body: WorkflowControl, user=Depends(actor)):
        return workflow_action(identifier, body, user, "resume_workflow")

    @app.post("/api/v1/workflows/{identifier}/cancel", status_code=202)
    def cancel(identifier: str, body: WorkflowControl, user=Depends(actor)):
        return workflow_action(identifier, body, user, "cancel_workflow")

    @app.post("/api/v1/workflows/{identifier}/goal-amendments", status_code=202)
    def amend_v2(identifier: str, body: GoalAmendmentInput, user=Depends(actor)):
        from ..services.goal_amendments import structured_command_source

        return workflow_action(
            identifier,
            body,
            user,
            "add_learning_objective",
            target=body.topic,
            target_depth=body.target_depth,
            learning_priority=body.learning_priority,
            command_source=structured_command_source(body.model_dump(mode="json")),
        )

    @app.post("/api/v1/workflows/{identifier}/goal-revisions", status_code=202)
    def amend(identifier: str, body: GoalRevisionInput, user=Depends(actor)):
        # Preserve the v1 endpoint's stored request hash and addendum-only contract.
        # Genuine command-origin GoalVersion creation uses the explicit v2 endpoint.
        return workflow_action(
            identifier,
            body,
            user,
            "add_learning_topic",
            target=body.topic,
            legacy_goal_revision=True,
        )

    @app.post("/api/v1/workflows/{identifier}/confirmations/{action_id}", status_code=202)
    def confirm(identifier: str, action_id: str, body: WorkflowControl, user=Depends(actor)):
        with service.db.read() as db:
            workflow = db.get(WorkflowSession, identifier)
            if workflow is None or workflow.principal_id != user:
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            proposal = workflow.target.get("proposal", {})
        if action_id == "authorize_extraction":
            return workflow_action(identifier, body, user, "start_extraction")
        if action_id == "confirm_goal":
            return workflow_action(identifier, body, user, "start_build")
        if action_id == "authorize_plan":
            return workflow_action(identifier, body, user, "propose_plan")
        if action_id == proposal.get("version_id"):
            return workflow_action(
                identifier, body, user, "accept_plan", additional_actions=[("start_tutor", {})]
            )
        raise DomainError("CONFIRMATION_STALE", "需要确认的版本已经变化。", 409)

    @app.post("/api/v1/conversations/{identifier}/focus", status_code=202)
    def focus(identifier: str, body: WorkflowFocus, user=Depends(actor)):
        return service.dialogue.dispatch(
            user,
            identifier,
            DialogueMessage(
                text="switch_focus",
                expected_focus_revision=body.expected_focus_revision,
                idempotency_key=body.idempotency_key,
            ),
            forced_actions=[("switch_focus", {"mode": "selected"})],
            workflow_id=body.workflow_id,
        )

    @app.get("/api/v1/conversations/{identifier}/workflows")
    def workflows(identifier: str, user=Depends(actor)):
        return service.workflows.list(user, identifier)

    @app.get("/api/v1/conversations/{identifier}/workflow-status")
    def status(identifier: str, workflow_id: str | None = None, user=Depends(actor)):
        return service.workflows.status(user, identifier, workflow_id)

    @app.get("/api/v1/conversations/{identifier}/activity-events")
    async def activity(
        identifier: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
        once: bool = False,
        user=Depends(actor),
    ):
        if last_event_id and (not last_event_id.isascii() or not last_event_id.isdecimal()):
            raise DomainError("CURSOR", "事件游标无效。", 422)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError:
            raise DomainError("CURSOR", "事件游标无效。", 422) from None
        if cursor > 2_147_483_647:
            raise DomainError("CURSOR", "事件游标无效。", 422)
        await asyncio.to_thread(service.workflows.status, user, identifier)

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(_read_activity, service, user, identifier, cursor)
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if once:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
