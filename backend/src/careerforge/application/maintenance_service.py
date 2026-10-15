"""Logical revocation is immediate; this bounded sweep erases affected stored text."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import (
    build_service,
    learning_service,
    planning_service,
    repository_service,
)
from careerforge.application.market_report_service import inputs
from careerforge.application.run_service import transition
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import TERMINAL
from careerforge.infrastructure.db.models import (
    AgentRun,
    AlignmentReport,
    ArtifactFeedback,
    Assessment,
    AssessmentReview,
    Attachment,
    BuildProject,
    BuildTaskVersion,
    Citation,
    Deletion,
    EvidenceDeletion,
    IdempotencyRecord,
    LearningObjectiveVersion,
    LearningPlanVersion,
    LearningUnit,
    MaintenanceCursor,
    MarketReport,
    PersonalEvidence,
    PersonalEvidenceVersion,
    PlanDeltaProposal,
    ProjectRecommendation,
    Repository,
    RepositoryFile,
    RepositorySnapshot,
    Requirement,
    ResumeDecision,
    RunEvent,
    Source,
    SourceSnapshot,
    StageArtifact,
    Submission,
    TaskDraft,
    WaitRequest,
)


def authorized(db: Session, run: AgentRun) -> None:
    if run.input_manifest.get("_revoked"):
        raise BusinessError(404, "NOT_FOUND", "输入已撤销。")
    data = run.input_manifest
    if run.workflow_version == "agent-study-2.0":
        from careerforge.application.study_service import validate_run
        validate_run(db, run)
        return
    if run.workflow_type == "market_research" and "source_snapshot_ids" in data:
        inputs(db, run.owner_id, data["source_snapshot_ids"])
    elif run.workflow_type == "alignment":
        planning_service.validate_alignment(db, run.owner_id, data)
    elif run.workflow_type == "project_research":
        if run.workflow_version == "learning-studio-research-1.0":
            from careerforge.application.studio_learning_service import validate_inputs

            validate_inputs(db, run)
        elif run.workflow_version == "learning-studio-agent-1.0":
            from careerforge.application.studio_service import workspace

            row = workspace(db, run.owner_id, UUID(data["workspace_id"]))
            if data.get("practice_id"):
                from careerforge.application.studio_practice_service import validate

                validate(db, run.owner_id, row, data["practice_id"])
            for identifier in data["citation_ids"]:
                repository_service.citation_detail(db, run.owner_id, UUID(identifier))
            if data.get("objective_version_id"):
                planning_service.objective_version(
                    db, run.owner_id, UUID(data["objective_version_id"])
                )
        else:
            repository_service.validate_research(db, run.owner_id, data)
    elif run.workflow_type == "learning_path":
        learning_service.validate(db, run.owner_id, data)
    elif run.workflow_type == "assessment":
        build_service.submission(db, run.owner_id, UUID(data["submission_id"]))


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        try:
            UUID(value)
            return value
        except ValueError:
            return "[已删除]"
    return value


def contains(value: Any, ids: set[str]) -> bool:
    if isinstance(value, dict):
        return any(contains(v, ids) for v in value.values())
    if isinstance(value, list):
        return any(contains(v, ids) for v in value)
    return isinstance(value, str) and value in ids


def page(db: Session, model: Any, query: Any, limit: int) -> list[Any]:
    name = model.__tablename__
    cursor = db.get(MaintenanceCursor, name)
    if cursor is None:
        cursor = MaintenanceCursor(name=name)
        db.add(cursor)
        db.flush()
    scan = query.where(model.id > cursor.last_id) if cursor.last_id else query
    rows = list(db.scalars(scan.order_by(model.id).limit(limit)))
    if not rows and cursor.last_id:
        rows = list(db.scalars(query.order_by(model.id).limit(limit)))
    cursor.last_id = rows[-1].id if rows else None
    return rows


def clean_once(db: Session, limit: int = 100) -> dict[str, int]:
    revoked = 0
    erased = 0
    # Invalidate dependent rows before erasing upstream text. Guards are authoritative.
    from careerforge.application.job_contract_service import read as contract_read
    from careerforge.infrastructure.db.models import JobContractArtifact

    checks: list[tuple[Any, Any]] = [
        (JobContractArtifact, contract_read),
        (PersonalEvidenceVersion, planning_service.evidence_version),
        (LearningObjectiveVersion, planning_service.objective_version),
        (ProjectRecommendation, repository_service.recommendation),
        (LearningUnit, learning_service.unit),
        (BuildProject, build_service.project),
        (BuildTaskVersion, build_service.task_version),
        (Submission, build_service.submission),
        (Assessment, build_service.assessment),
    ]
    invalid: set[str] = set()
    for model, check in checks:
        for row in page(db, model, select(model).where(~model.payload.has_key("_revoked")), limit):
            try:
                check(db, row.owner_id, row.id)
            except BusinessError as error:
                if error.status != 404:
                    raise
                row.payload = {**scrub(row.payload), "_revoked": True}
                if isinstance(row, JobContractArtifact):
                    row.raw_bytes = None
                invalid.add(str(row.id))
                revoked += 1
    for run in page(
        db,
        AgentRun,
        select(AgentRun)
        .where(~AgentRun.input_manifest.has_key("_revoked"))
        .with_for_update(skip_locked=True),
        limit,
    ):
        try:
            authorized(db, run)
        except BusinessError as error:
            if error.status != 404:
                raise
            invalid.add(str(run.id))
            run.input_manifest = {**scrub(run.input_manifest), "_revoked": True}
            run.epoch += 1
            if run.status not in TERMINAL:
                transition(db, run, "failed", event_kind="input.revoked")
            for stage in db.scalars(select(StageArtifact).where(StageArtifact.run_id == run.id)):
                stage.payload = {"_revoked": True}
            for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)):
                event.payload = {"_revoked": True}
            for wait in db.scalars(select(WaitRequest).where(WaitRequest.run_id == run.id)):
                wait.question = {"_revoked": True}
                decision = db.get(ResumeDecision, wait.id)
                if decision:
                    decision.response = {"_revoked": True}
            output_models: list[Any] = [
                MarketReport,
                AlignmentReport,
                ProjectRecommendation,
                LearningUnit,
                Assessment,
            ]
            for model in output_models:
                outputs: list[Any] = list(db.scalars(select(model).where(model.run_id == run.id)))
                for output in outputs:
                    output.payload = {**scrub(output.payload), "_revoked": True}
                    invalid.add(str(output.id))
            revoked += 1
    dependent_models: list[Any] = [
        TaskDraft,
        AssessmentReview,
        PlanDeltaProposal,
        ArtifactFeedback,
        LearningPlanVersion,
    ]
    for model in dependent_models:
        for row in page(db, model, select(model).where(~model.payload.has_key("_revoked")), limit):
            identity = (
                getattr(row, "unit_id", None)
                or getattr(row, "assessment_id", None)
                or getattr(row, "artifact_id", None)
            )
            revoked_input = str(identity) in invalid or contains(row.payload, invalid)
            try:
                if model == TaskDraft:
                    learning_service.unit(db, row.owner_id, row.unit_id)
                elif model in {AssessmentReview, PlanDeltaProposal}:
                    build_service.assessment(db, row.owner_id, row.assessment_id)
                elif model == ArtifactFeedback:
                    if row.artifact_kind == "learning_unit":
                        learning_service.unit(db, row.owner_id, row.artifact_id)
                    else:
                        repository_service.recommendation(db, row.owner_id, row.artifact_id)
                elif model == LearningPlanVersion:
                    planning_service.plan_result(db, row.owner_id, row)
            except BusinessError as error:
                if error.status != 404:
                    raise
                revoked_input = True
            if revoked_input:
                invalid.add(str(row.id))
                row.payload = {**scrub(row.payload), "_revoked": True}
    for root in page(db, Source, select(Source).where(Source.revoked_at.is_not(None)), limit):
        root.title = None
        for snapshot in db.scalars(
            select(SourceSnapshot).where(SourceSnapshot.source_id == root.id)
        ):
            snapshot.content = ""
            snapshot.metadata_values = {"_revoked": True}
            erased += 1
        citations = list(db.scalars(select(Citation.id).where(Citation.source_id == root.id)))
        for requirement in db.scalars(
            select(Requirement).where(Requirement.citation_id.in_(citations))
        ):
            requirement.payload = {"_revoked": True}
        for a in db.scalars(select(Attachment).where(Attachment.source_id == root.id)):
            a.revoked_at = root.revoked_at
            a.body = None
    for evidence_root in page(
        db,
        PersonalEvidence,
        select(PersonalEvidence).where(PersonalEvidence.revoked_at.is_not(None)),
        limit,
    ):
        for row in db.scalars(
            select(PersonalEvidenceVersion).where(
                PersonalEvidenceVersion.evidence_id == evidence_root.id
            )
        ):
            row.payload = {**scrub(row.payload), "_revoked": True}
            invalid.add(str(row.id))
            erased += 1
    for a in db.scalars(
        select(Attachment)
        .where(Attachment.revoked_at.is_not(None), Attachment.body.is_not(None))
        .limit(limit)
    ):
        a.body = None
        a.name = "deleted"
        erased += 1
    for repo_root in page(
        db, Repository, select(Repository).where(Repository.revoked_at.is_not(None)), limit
    ):
        snapshots = list(
            db.scalars(
                select(RepositorySnapshot.id).where(
                    RepositorySnapshot.repository_id == repo_root.id
                )
            )
        )
        for file in db.scalars(
            select(RepositoryFile)
            .where(RepositoryFile.snapshot_id.in_(snapshots), RepositoryFile.content != "")
            .limit(limit)
        ):
            file.content = ""
            erased += 1
    private_models: list[Any] = [
        PersonalEvidenceVersion,
        LearningObjectiveVersion,
        BuildTaskVersion,
        Submission,
        LearningUnit,
        ProjectRecommendation,
        MarketReport,
        AlignmentReport,
        Assessment,
        LearningPlanVersion,
    ]
    for model in private_models:
        identifiers: list[Any] = list(
            db.scalars(select(model.id).where(model.payload.has_key("_revoked")))
        )
        invalid.update(str(i) for i in identifiers)
    for row in page(
        db,
        IdempotencyRecord,
        select(IdempotencyRecord).where(IdempotencyRecord.response_summary.is_not(None)),
        limit,
    ):
        if contains(row.response_summary, invalid):
            row.response_summary = {"_revoked": True}
    job_models: list[Any] = [Deletion, EvidenceDeletion]
    for model in job_models:
        jobs: list[Any] = list(
            db.scalars(select(model).where(model.status == "revoked").limit(limit))
        )
        for job in jobs:
            if model == EvidenceDeletion:
                pending = any(
                    not v.payload.get("_revoked")
                    for v in db.scalars(
                        select(PersonalEvidenceVersion).where(
                            PersonalEvidenceVersion.evidence_id == job.evidence_id
                        )
                    )
                )
                pending = (
                    pending
                    or db.scalar(
                        select(Attachment.id)
                        .where(
                            Attachment.evidence_id == job.evidence_id, Attachment.body.is_not(None)
                        )
                        .limit(1)
                    )
                    is not None
                )
            elif job.source_id:
                pending = (
                    db.scalar(
                        select(SourceSnapshot.id)
                        .where(
                            SourceSnapshot.source_id == job.source_id, SourceSnapshot.content != ""
                        )
                        .limit(1)
                    )
                    is not None
                )
            else:
                attachment = db.get(Attachment, job.attachment_id)
                pending = attachment is not None and attachment.body is not None
            if not pending:
                job.status = "erased"
    return {"invalidated": revoked, "erased": erased}
