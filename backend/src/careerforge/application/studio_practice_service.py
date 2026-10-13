"""User-selected migrations from grounded learning reports; no model or execution I/O."""

from uuid import UUID, uuid4

from careerforge.application import studio_service as studio
from careerforge.application.common import fingerprint, idempotency
from careerforge.application.repository_service import citation_detail
from careerforge.application.studio_learning_service import visible_action
from careerforge.domain.errors import BusinessError


def report_hash(report):
    return fingerprint({k: report[k] for k in ("objective_version_id", "guide", "sources")})


def valid_report(db, owner, row, identifier):
    report = studio.action(row, identifier)
    visible = visible_action(db, owner, report) if report["kind"] == "learning_report" else {}
    if (
        report["kind"] != "learning_report"
        or visible.get("status") not in {"completed", "recovered"}
        or visible.get("guide", {}).get("status") != "grounded"
        or not visible.get("sources")
    ):
        raise BusinessError(404, "PRACTICE_SOURCE_UNAVAILABLE", "学习说明或原文依据不可用。")
    return report


def validate(db, owner, row, identifier):
    item = studio.action(row, identifier)
    if item["kind"] != "practice":
        raise BusinessError(404, "NOT_FOUND", "练习不存在。")
    report = valid_report(db, owner, row, item["report_id"])
    index = item["exercise_index"]
    if (
        report_hash(report) != item["source_hash"]
        or not 0 <= index < len(report["guide"]["practice"])
        or report["guide"]["practice"][index] != item["exercise"]
        or report["objective_version_id"] != item["objective_version_id"]
    ):
        raise BusinessError(404, "PRACTICE_SOURCE_UNAVAILABLE", "练习依据已变化，请重新选择练习。")
    return item, report


def visible(db, owner, row, item):
    identifier = item["id"] if item["kind"] == "practice" else item.get("practice_id")
    try:
        validate(db, owner, row, identifier)
    except (BusinessError, ValueError, KeyError):
        return {
            "id": item["id"],
            "kind": item["kind"],
            "status": "unavailable",
            "practice_id": identifier,
            "error": "PRACTICE_SOURCE_UNAVAILABLE",
        }
    return item


def create(db, owner, workspace_id, report_id, body, key):
    row = studio.workspace(db, owner, workspace_id, True)
    record = idempotency(
        db, owner, f"studio.practice:{workspace_id}:{report_id}", key, body.model_dump(mode="json")
    )
    report = valid_report(db, owner, row, report_id)
    if record.response_summary:
        validate(db, owner, row, record.response_summary["practice_id"])
        return {**record.response_summary, "workspace": studio.detail(row, db)}
    studio.revision(row, body.expected_revision)
    exercises = report["guide"]["practice"]
    if body.exercise_index >= len(exercises):
        raise BusinessError(422, "INVALID_PRACTICE", "请选择说明中已有的迁移练习。")
    exercise = exercises[body.exercise_index]
    if not exercise.strip() or len(exercise) > 4000:
        raise BusinessError(422, "INVALID_PRACTICE", "练习为空或超过 4,000 字符。")
    source_hash = report_hash(report)
    existing = next(
        (
            a
            for a in row.payload["actions"]
            if a["kind"] == "practice"
            and a["report_id"] == str(report_id)
            and a["exercise_index"] == body.exercise_index
            and a["source_hash"] == source_hash
        ),
        None,
    )
    identifier = existing["id"] if existing else str(uuid4())
    if not existing:
        studio.append(
            row,
            {
                "id": identifier,
                "kind": "practice",
                "status": "draft",
                "report_id": str(report_id),
                "source_hash": source_hash,
                "objective_version_id": report["objective_version_id"],
                "objective_title": report.get("objective_title", ""),
                "exercise_index": body.exercise_index,
                "exercise": exercise,
                "citation_ids": [s["citation_id"] for s in report["sources"]],
                "synthetic_source": report.get("synthetic", False),
                "note": "",
                "verified": False,
            },
        )
        row.revision += 1
    record.response_summary = {"practice_id": identifier}
    return {**record.response_summary, "workspace": studio.detail(row, db)}


def context(db, owner, row, identifier):
    item, report = validate(db, owner, row, identifier)
    references = [citation_detail(db, owner, UUID(i)) for i in item["citation_ids"]]
    return (
        item,
        references,
        {
            "practice_id": item["id"],
            "exercise": item["exercise"],
            "summary": report["guide"]["summary"][:1000],
            "learning_points": [
                {
                    "title": p["title"],
                    "explanation": p["explanation"][:600],
                    "citation_id": p["citation_id"],
                }
                for p in report["guide"]["points"]
            ],
            "questions": report["guide"]["questions"],
            "limitations": report["guide"]["limitations"],
            "note_untrusted": item.get("note", "")[:2000],
            "synthetic_source": item.get("synthetic_source", False),
            "verified": False,
        },
    )


def note(db, owner, workspace_id, identifier, body):
    row = studio.workspace(db, owner, workspace_id, True)
    studio.revision(row, body.expected_revision)
    validate(db, owner, row, identifier)
    studio.update_action(row, identifier, {"note": body.note})
    row.revision += 1
    return studio.detail(row, db)
