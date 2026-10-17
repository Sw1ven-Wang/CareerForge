from uuid import UUID, uuid4

import pytest
from careerforge.infrastructure.db.models import (
    Assessment,
    AssessmentConfirmation,
    AssessmentReview,
    BuildTask,
    LearningUnit,
    ProjectRecommendation,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.assessment.workflow import execute as assessment_execute
from careerforge.workflows.learning_path.workflow import execute as learning_execute
from careerforge.workflows.project_research.workflow import execute as research_execute
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_market_research import goal, post
from test_owner_scope import clients as clients
from test_planning import objective_body

pytestmark = pytest.mark.infrastructure


def data(response, status=201):
    assert response.status_code == status, response.text
    return response.json()["data"]


def objective(client, target):
    body = objective_body(target)
    body["criteria"].append(
        {
            **body["criteria"][0],
            "dimension": "explanation",
            "required_artifact_types": ["explanation_answer"],
        }
    )
    return data(post(client, "/learning-objectives", body))


def path(client, obj):
    catalog = client.get("/api/v1/repository-catalog").json()["data"][0]
    repo = data(
        post(
            client,
            "/repositories",
            {"github_url": catalog["github_url"], "requested_ref": catalog["requested_ref"]},
        )
    )
    run = data(
        post(
            client,
            "/project-research-runs",
            {
                "objective_version_ids": [obj["objective_version_id"]],
                "candidate_repository_ids": [repo["repository_id"]],
            },
        ),
        202,
    )
    research_execute(UUID(run["run_id"]), "test-research")
    with Session(get_engine()) as db:
        rec = db.scalar(
            select(ProjectRecommendation).where(ProjectRecommendation.run_id == UUID(run["run_id"]))
        )
        assert rec is not None
        identifier = str(rec.id)
    selected = data(
        post(
            client,
            "/project-selections",
            {
                "recommendation_id": identifier,
                "objective_version_ids": [obj["objective_version_id"]],
                "expected_goal_version_id": obj["goal_version_id"],
            },
        )
    )
    body = {
        "selection_id": selected["selection_id"],
        "objective_version_ids": [obj["objective_version_id"]],
        "criterion_version_ids": obj["criterion_version_ids"],
    }
    return identifier, body


def test_fixed_snapshot_citations_full_conditions_progress_and_feedback(clients):
    a, b = clients
    obj = objective(a, goal(a))
    rec_id, body = path(a, obj)
    rec = a.get("/api/v1/project-recommendations/" + rec_id).json()["data"]
    assert len(rec["commit"]) == 40 and rec["executed"] is False
    citation = rec["code_evidence"][0]["citation_id"]
    quote = a.get("/api/v1/citations/" + citation).json()["data"]
    assert "def " in quote["quote"] and quote["commit"] == rec["commit"]
    assert b.get("/api/v1/citations/" + citation).status_code == 404
    assert (
        a.get(
            "/api/v1/repository-snapshots/" + rec["repository_snapshot_id"] + "/file-content",
            params={"path": "../secret", "end_line": 1},
        ).status_code
        == 422
    )
    assert (
        post(
            a,
            "/learning-path-runs",
            {**body, "criterion_version_ids": body["criterion_version_ids"][:1]},
        ).status_code
        == 422
    )
    assert post(b, "/learning-path-runs", body).status_code == 404
    run = data(post(a, "/learning-path-runs", body), 202)
    learning_execute(UUID(run["run_id"]), "test-learning")
    with Session(get_engine()) as db:
        row = db.scalar(select(LearningUnit).where(LearningUnit.run_id == UUID(run["run_id"])))
        identifier = str(row.id)
    original = a.get("/api/v1/learning-units/" + identifier).json()["data"]
    assert original["reading_steps"] and original["task_drafts"]
    progress = a.patch(
        "/api/v1/learning-units/" + identifier + "/progress",
        json={
            "expected_version": 1,
            "completed_step_ids": [original["reading_steps"][0]["step_id"]],
            "actual_minutes": 5,
            "note": "synthetic",
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert data(progress, 200)["competence_updated"] is False
    feedback = data(
        post(
            a,
            "/learning-units/" + identifier + "/feedback",
            {
                "kind": "explanation_error",
                "detail": "synthetic correction",
                "citation_ids": [citation],
            },
        )
    )
    revised = data(
        post(
            a,
            "/learning-path-runs",
            {
                **body,
                "previous_learning_unit_id": identifier,
                "feedback_ids": [feedback["feedback_id"]],
            },
        ),
        202,
    )
    learning_execute(UUID(revised["run_id"]), "test-learning-revision")
    current = a.get("/api/v1/learning-units/" + identifier).json()["data"]
    assert current["reading_steps"] == original["reading_steps"] and current["needs_review"]
    with Session(get_engine()) as db:
        assert db.scalar(select(BuildTask).where(BuildTask.owner_id == row.owner_id)) is None


def setup_task(a):
    obj = objective(a, goal(a))
    project = data(
        post(a, "/build-projects", {"title": "Synthetic project", "problem_statement": "Test only"})
    )
    body = {
        "objective_version_id": obj["objective_version_id"],
        "criterion_version_ids": obj["criterion_version_ids"],
        "title": "Synthetic task",
        "description": "No actual learning claim",
        "deliverables": ["log", "explanation"],
    }
    task = data(post(a, "/build-projects/" + project["build_project_id"] + "/tasks", body))
    return task, body


def submit(a, task):
    upload = a.post(
        "/api/v1/attachments",
        data={"purpose": "submission"},
        files={
            "file": (
                "output.log",
                b"synthetic test fixture, not an actual experiment",
                "text/plain",
            )
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    attachment = data(upload)["attachment_id"]
    body = {
        "expected_version": task["version"],
        "task_version_id": task["current_task_version_id"],
        "criterion_version_ids": task["criterion_version_ids"],
        "summary": "synthetic only",
        "code_commit": "a" * 40,
        "attachment_ids": [attachment],
        "environment": {"os": "test"},
        "execution_records": [
            {
                "command_text": "synthetic command not executed",
                "reported_exit_code": 0,
                "output_attachment_id": attachment,
                "environment_refs": ["test"],
                "reported_at": "2026-10-12",
                "execution_provenance": "user_reported",
            }
        ],
        "explanation_answers": [
            {
                "question": "test",
                "answer": "synthetic",
                "criterion_version_ids": task["criterion_version_ids"],
            }
        ],
    }
    return data(post(a, "/build-tasks/" + task["task_id"] + "/submissions", body))


def assess(a, saved, criteria):
    run = data(
        post(
            a,
            "/submissions/" + saved["submission_id"] + "/assessment-runs",
            {"criterion_version_ids": criteria},
        ),
        202,
    )
    assessment_execute(UUID(run["run_id"]), "test-assessment")
    with Session(get_engine()) as db:
        row = db.scalar(select(Assessment).where(Assessment.run_id == UUID(run["run_id"])))
        identifier = str(row.id)
    return a.get("/api/v1/assessments/" + identifier).json()["data"]


def confirm_body(task, proposal, decision="accept", trace=True):
    return {
        "expected_version": proposal["version"],
        "expected_task_version": task["version"],
        "decision": decision,
        "criterion_decisions": [
            {
                "criterion_version_id": i,
                "verdict": "pass" if decision == "accept" else "unknown",
                "method": "human_review",
                "evidence_refs": [],
                "reason": "synthetic test only",
                "self_review_record": {
                    "procedure": "fixture",
                    "observed": "fixture",
                    "environment": "test",
                    "code_version": "a" * 40,
                }
                if trace
                else None,
            }
            for i in task["criterion_version_ids"]
        ],
        "scope_acknowledged": True,
    }


@pytest.mark.skip(reason="Independent build and assessment API retired; historical regression only")
def test_revision_resubmission_unknown_guard_confirmation_and_revoke(clients):
    a, b = clients
    task, body = setup_task(a)
    project = task["build_project_id"]
    assert (
        post(
            a,
            "/build-projects/" + project + "/tasks",
            {**body, "criterion_version_ids": body["criterion_version_ids"][:1]},
        ).status_code
        == 422
    )
    saved = submit(a, task)
    task = a.get("/api/v1/build-tasks/" + task["task_id"]).json()["data"]
    proposal = assess(a, saved, task["criterion_version_ids"])
    assert all(i["verdict"] == "unknown" for i in proposal["items"])
    endpoint = "/assessments/" + proposal["assessment_id"] + "/confirm"
    assert post(b, endpoint, confirm_body(task, proposal)).status_code == 404
    assert post(a, endpoint, confirm_body(task, proposal, trace=False)).status_code == 422
    revision = data(post(a, endpoint, confirm_body(task, proposal, "request_revision")))
    assert revision["task_status"] == "needs_revision"
    task = a.get("/api/v1/build-tasks/" + task["task_id"]).json()["data"]
    assert post(a, endpoint, confirm_body(task, proposal)).status_code == 409
    saved2 = submit(a, task)
    task = a.get("/api/v1/build-tasks/" + task["task_id"]).json()["data"]
    proposal2 = assess(a, saved2, task["criterion_version_ids"])
    key = str(uuid4())
    end2 = "/assessments/" + proposal2["assessment_id"] + "/confirm"
    payload = confirm_body(task, proposal2)
    accepted = data(post(a, end2, payload, key))
    assert accepted["task_status"] == "accepted"
    assert data(post(a, end2, payload, key)) == accepted
    assert (
        post(
            a,
            "/build-tasks/" + task["task_id"] + "/versions",
            {**body, "expected_version": accepted["task_revision"], "change_reason": "edit"},
        ).status_code
        == 409
    )
    original = a.get("/api/v1/assessments/" + proposal2["assessment_id"]).json()["data"]
    assert original["items"] == proposal2["items"] and len(original["reviews"]) == 1
    revoked = data(
        post(
            a,
            "/assessments/" + proposal2["assessment_id"] + "/revoke",
            {
                "expected_version": accepted["assessment_revision"],
                "expected_task_version": accepted["task_revision"],
                "reason": "synthetic revoke",
            },
        ),
        200,
    )
    assert revoked["task_status"] == "needs_revision"
    assert (
        post(
            a,
            end2,
            {
                **payload,
                "expected_version": revoked["assessment_revision"],
                "expected_task_version": revoked["task_revision"],
            },
        ).status_code
        == 409
    )


@pytest.mark.skip(reason="Independent build and assessment API retired; historical regression only")
def test_changed_task_clears_submission_and_old_assessment_is_historical(clients):
    a, _ = clients
    task, body = setup_task(a)
    saved = submit(a, task)
    task = a.get("/api/v1/build-tasks/" + task["task_id"]).json()["data"]
    proposal = assess(a, saved, task["criterion_version_ids"])
    revised = data(
        post(
            a,
            "/build-tasks/" + task["task_id"] + "/versions",
            {
                **body,
                "expected_version": task["version"],
                "change_reason": "new content",
                "description": "changed scope",
            },
        )
    )
    assert revised["current_submission_id"] is None and revised["status"] == "needs_revision"
    assert (
        post(
            a,
            "/assessments/" + proposal["assessment_id"] + "/confirm",
            confirm_body(revised, proposal),
        ).status_code
        == 409
    )
    assert a.get("/api/v1/submissions/" + saved["submission_id"]).status_code == 200


def test_deletion_blocks_replay_and_erases_private_body(clients):
    import os

    from careerforge.application.maintenance_service import clean_once
    from careerforge.infrastructure.db.models import Attachment, PersonalEvidenceVersion

    # Physical-erasure tests require a dedicated disposable database, never development data.
    if "careerforge_v1_validation_" not in os.getenv("CAREERFORGE_DATABASE_URL", ""):
        pytest.skip("Physical erasure requires dedicated validation database")
    a, _ = clients
    upload = a.post(
        "/api/v1/attachments",
        data={"purpose": "personal_evidence"},
        files={"file": ("personal.md", b"private synthetic marker", "text/markdown")},
        headers={"Idempotency-Key": str(uuid4())},
    )
    attachment = data(upload)["attachment_id"]
    key = str(uuid4())
    body = {
        "kind": "readme",
        "title": "private synthetic marker",
        "description": "private synthetic marker",
        "attachment_ids": [attachment],
    }
    evidence = data(post(a, "/evidence", body, key))
    deleted = a.request(
        "DELETE",
        "/api/v1/evidence/" + evidence["evidence_id"],
        json={"expected_version": 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert deleted.status_code == 202
    assert post(a, "/evidence", body, key).status_code == 404
    assert a.get("/api/v1/attachments/" + attachment + "/content").status_code == 404
    with Session(get_engine()) as db, db.begin():
        clean_once(db, limit=100)
    with Session(get_engine()) as db:
        version = db.get(PersonalEvidenceVersion, UUID(evidence["evidence_version_id"]))
        assert "private synthetic marker" not in str(version.payload)
        assert db.get(Attachment, UUID(attachment)).body is None
    assert a.get("/api/v1/evidence").status_code == 200


@pytest.mark.skip(reason="Independent build and assessment API retired; historical regression only")
def test_concurrent_confirmation_creates_one_review(clients):
    from concurrent.futures import ThreadPoolExecutor

    a, _ = clients
    task, _ = setup_task(a)
    saved = submit(a, task)
    task = a.get("/api/v1/build-tasks/" + task["task_id"]).json()["data"]
    proposal = assess(a, saved, task["criterion_version_ids"])
    endpoint = "/assessments/" + proposal["assessment_id"] + "/confirm"
    payload = confirm_body(task, proposal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(a, endpoint, payload), range(2)))
    assert sorted(r.status_code for r in responses) == [201, 409]
    with Session(get_engine()) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(AssessmentReview).where(
                            AssessmentReview.assessment_id == UUID(proposal["assessment_id"])
                        )
                    )
                )
            )
            == 1
        )
        assert (
            len(
                list(
                    db.scalars(
                        select(AssessmentConfirmation).where(
                            AssessmentConfirmation.assessment_id == UUID(proposal["assessment_id"])
                        )
                    )
                )
            )
            == 1
        )
