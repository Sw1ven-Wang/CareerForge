"""Internal S02 contract fixture; never exposed as a sixth public workflow."""

import asyncio
import time
from typing import Any
from uuid import UUID

from sqlalchemy.orm import sessionmaker

from careerforge.application.model_call_service import invoke_model
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
    wait_for_choice,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.fake import FakeModel
from careerforge.infrastructure.llm.provider import ModelError
from careerforge.schemas.model_io import ModelRequest

VERSION = "s02-demo-v2"
SUPPORTED_VERSIONS = {"s02-demo-v1", VERSION}


def execute(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    with factory.begin() as db:
        run = leased(db, run_id, epoch, worker)
        if run.workflow_version not in SUPPORTED_VERSIONS:
            transition(db, run, "failed")
            event(db, run, "workflow.incompatible", {"workflow_version": run.workflow_version})
            return
        draft = artifact(db, run, "draft", "1")
        needs_draft = draft is None
    if needs_draft:
        # No business transaction is held during a provider call.
        model = FakeModel(
            {
                "id": "s02-synthetic",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"draft":"Synthetic S02 draft","synthetic":true}'},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        try:
            result = asyncio.run(
                invoke_model(
                    run_id,
                    epoch,
                    worker,
                    model,
                    ModelRequest(
                        model="fake",
                        prompt="S02 offline contract fixture",
                        response_schema={"type": "object"},
                    ),
                )
            )
            if result is None:
                return
            if result.finish_reason != "stop" or result.structured is None:
                raise ModelError("invalid_response", retryable=False)
        except ModelError as error:
            with factory.begin() as db:
                run = leased(db, run_id, epoch, worker)
                event(db, run, "stage.failed", {"stage_key": "draft", "error": error.code})
                transition(db, run, "failed")
            return
        with factory.begin() as db:
            leased(db, run_id, epoch, worker)
            commit_stage(db, run_id, epoch, worker, "draft", "1", result.structured or {})
    if needs_draft:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            # Explicit internal fixture only; no public workflow accepts fault controls.
            pause = run.input_manifest.get("test_pause_after_draft_seconds", 0)
        if isinstance(pause, int) and 0 < pause <= 30:
            time.sleep(pause)
    with factory.begin() as db:
        run = leased(db, run_id, epoch, worker)
        draft = artifact(db, run, "draft", "1")
        assert draft is not None
        question: dict[str, Any] = {"text": "接受此合成测试草稿？", "synthetic": True}
        question_version = "1"
        if run.workflow_version == VERSION:
            question = {**question, "draft": draft.payload}
            question_version = "2"
        decision = wait_for_choice(
            db,
            run_id,
            epoch,
            worker,
            "review",
            question,
            ["accept", "reject"],
            question_version=question_version,
        )
    if decision is None:
        return
    with factory.begin() as db:
        run = leased(db, run_id, epoch, worker)
        draft = artifact(db, run, "draft", "1")
        if draft is None:
            raise RuntimeError("Missing committed draft")
        commit_stage(
            db,
            run_id,
            epoch,
            worker,
            "result",
            "1",
            {"draft": draft.payload, "decision": decision, "synthetic": True},
        )
        transition(db, run, "succeeded")
