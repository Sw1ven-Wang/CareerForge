"""Logical plan families, frozen authorization and compatible history."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.study_models import StudyArtifact


def family(db: Session, plan: StudyArtifact) -> str:
    from careerforge.application.study_service import artifact_row

    seen: set[str] = set()
    current = plan
    while current.payload.get("source_plan_id"):
        if str(current.id) in seen or len(seen) >= 100:
            raise BusinessError(409, "PLAN_LINEAGE_INVALID", "计划版本链异常。")
        seen.add(str(current.id))
        current = artifact_row(
            db, plan.owner_id, plan.track_id, current.payload["source_plan_id"], "plan"
        )
    return str(current.id)


def versions(db: Session, plan: StudyArtifact) -> list[StudyArtifact]:
    root = family(db, plan)
    return [
        p
        for p in db.scalars(
            select(StudyArtifact)
            .where(
                StudyArtifact.owner_id == plan.owner_id,
                StudyArtifact.track_id == plan.track_id,
                StudyArtifact.kind == "plan",
            )
            .order_by(StudyArtifact.created_at, StudyArtifact.id)
        )
        if family(db, p) == root
    ]


def module_signature(db: Session, owner: UUID, module: Json) -> str:
    from careerforge.application.repository_service import citation_detail

    points = [citation_detail(db, owner, UUID(v)) for v in module["highlight_ids"]]
    return fingerprint(
        {
            "module": {
                k: v for k, v in module.items() if k not in {"project_review_ids", "highlight_ids"}
            },
            "references": sorted(
                (p["commit"], p["path"], p["start_line"], p["end_line"], p["content_hash"])
                for p in points
            ),
        }
    )


def requirement_signature(
    requirements: list[Json], keys: list[str]
) -> list[tuple[str, str, str | None]]:
    return sorted(
        (r["technology"].casefold().strip(), r["depth"], r.get("depth_basis"))
        for r in requirements
        if r["key"] in keys
    )


def compatible(db: Session, old: StudyArtifact, new: StudyArtifact, key: str) -> bool:
    if family(db, old) != family(db, new):
        return False
    if any(
        old.payload["intent"].get(k) != new.payload["intent"].get(k)
        for k in ("kind", "focus", "execution_runtime")
    ):
        return False
    before = next((m for m in old.payload["modules"] if m["key"] == key), None)
    after = next((m for m in new.payload["modules"] if m["key"] == key), None)
    if (
        not before
        or not after
        or module_signature(db, old.owner_id, before) != module_signature(db, new.owner_id, after)
    ):
        return False
    from careerforge.application.study_service import artifact_row

    signatures = []
    for plan, module in ((old, before), (new, after)):
        requirements = artifact_row(
            db, old.owner_id, old.track_id, plan.payload["requirements_id"], "requirements"
        )
        signatures.append(
            requirement_signature(requirements.payload["requirements"], module["requirement_keys"])
        )
    return signatures[0] == signatures[1]


def frozen_scope(db: Session, plan: StudyArtifact) -> None:
    from careerforge.application import repository_service as repositories
    from careerforge.application import study_service as study
    from careerforge.application.studio_learning_service import read_requirements

    requirement = study.artifact_row(
        db, plan.owner_id, plan.track_id, plan.payload["requirements_id"], "requirements"
    )
    source_ids = [s["source_snapshot_id"] for s in requirement.payload.get("sources", [])]
    if source_ids:
        read_requirements(db, plan.owner_id, source_ids)
    for identifier in plan.payload["review_ids"]:
        review = study.artifact_row(db, plan.owner_id, plan.track_id, identifier, "review")
        if (
            not review.payload.get("admitted")
            or review.payload["requirements_id"] != plan.payload["requirements_id"]
        ):
            raise BusinessError(409, "STUDY_PROJECT_UNAVAILABLE", "计划的冻结审核依据不可用。")
        repositories.snapshot(db, plan.owner_id, UUID(review.payload["repository_snapshot_id"]))
        for point in review.payload["highlights"]:
            repositories.citation_detail(db, plan.owner_id, UUID(point["citation_id"]))
    for point in plan.payload.get("focus_highlights", []):
        repositories.citation_detail(db, plan.owner_id, UUID(point["citation_id"]))


def heads(db: Session, owner: UUID, track_id: UUID) -> list[StudyArtifact]:
    result: dict[str, StudyArtifact] = {}
    for item in db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.owner_id == owner,
            StudyArtifact.track_id == track_id,
            StudyArtifact.kind == "plan",
        )
        .order_by(StudyArtifact.created_at, StudyArtifact.id)
    ):
        result[family(db, item)] = item
    return list(result.values())


def metadata(db: Session, plan: StudyArtifact, current_requirements: str | None) -> Json:
    from careerforge.application.study_service import approved, track

    valid = {str(v.id) for v in approved(db, track(db, plan.owner_id, plan.track_id))}
    return {
        "historical_reviews": not set(plan.payload["review_ids"]) <= valid,
        "plan_family_id": family(db, plan),
        "historical_requirements": plan.payload["requirements_id"] != current_requirements,
    }
