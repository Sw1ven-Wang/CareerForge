"""Yield while durable children run; publish only under the live parent's lease."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from careerforge.application import delegation_service as delegation
from careerforge.application import review_batch_service as tasks
from careerforge.application import study_service as study
from careerforge.application import study_validation as validation
from careerforge.application.common import fingerprint
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import TERMINAL, LeaseLost
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.session import get_engine
from careerforge.schemas.study import ReviewOutput


def execute(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    try:
        with factory.begin() as db:
            parent = leased(db, run_id, epoch, worker)
            study.validate_run(db, parent)
            for frozen in parent.input_manifest["candidates"]:
                delegation.create_child(
                    db,
                    parent,
                    frozen["candidate_id"],
                    tasks.child_manifest(parent, frozen),
                    parent.input_manifest["per_child_call_limit"],
                    parent.input_manifest["max_output_tokens"],
                )
            children = db.scalars(
                select(AgentRun).where(AgentRun.parent_run_id == parent.id).order_by(AgentRun.id)
            ).all()
            if any(c.status not in TERMINAL for c in children):
                transition(db, parent, "waiting_children")
                return
            track = study.track(db, parent.owner_id, UUID(parent.input_manifest["track_id"]), True)
            results = []
            for child in children:
                saved = artifact(db, child, "result", delegation.CHILD_VERSION)
                if child.status not in {"succeeded", "partially_succeeded"} or not saved:
                    results.append(
                        {"child_run_id": str(child.id), "status": child.status, "published": False}
                    )
                    continue
                value = saved.payload
                # Revalidate under the parent/track locks; child text is not publication authority.
                study.validate_run(db, parent)
                output, matches, checked = validation.partial_review(
                    ReviewOutput.model_validate(value["output"]),
                    child.input_manifest,
                    value["reading"],
                    insufficient_to_uncertain=True,
                )
                prior = artifact(db, parent, "published:" + str(child.id), delegation.BATCH_VERSION)
                if prior:
                    result = prior.payload
                else:
                    item = study.publish_review(
                        db,
                        parent,
                        track,
                        child.input_manifest,
                        value["material"],
                        output,
                        value["reading"],
                        matches,
                        value["synthetic"],
                        evidence_validation=checked,
                    )
                    result = {
                        "child_run_id": str(child.id),
                        "status": child.status,
                        "published": True,
                        "study_artifact_id": str(item.id),
                        "admitted": item.payload["admitted"],
                        "handoff_stage_id": str(saved.id),
                        "handoff_hash": fingerprint(value),
                    }
                    commit_stage(
                        db,
                        parent.id,
                        epoch,
                        worker,
                        "published:" + str(child.id),
                        delegation.BATCH_VERSION,
                        result,
                    )
                results.append(result)
            summary = {
                "children": results,
                "budget": delegation.family_state(db, parent),
                "parent_model_calls": 0,
                "source_validation": "rechecked_before_publication",
            }
            commit_stage(db, parent.id, epoch, worker, "result", delegation.BATCH_VERSION, summary)
            event(
                db,
                parent,
                "task.joined",
                {"child_count": len(children), "published": sum(r["published"] for r in results)},
            )
            transition(
                db,
                parent,
                "succeeded" if all(r["published"] for r in results) else "partially_succeeded",
            )
    except LeaseLost:
        return
    except (BusinessError, ValueError, KeyError) as error:
        with factory.begin() as db:
            try:
                parent = leased(db, run_id, epoch, worker)
            except LeaseLost:
                return
            event(
                db,
                parent,
                "stage.failed",
                {"error": getattr(error, "code", "INVALID_CHILD_RESULT"), "automatic_retry": False},
            )
            transition(db, parent, "failed")
