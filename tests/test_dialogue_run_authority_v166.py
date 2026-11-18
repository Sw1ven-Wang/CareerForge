"""Direct gateway defense against multifield DB corruption, not an API exploit."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from careerforge.contracts import TurnInput
from careerforge.contracts_ordinary_checker_v166 import ORDINARY_CHECKER_KEY
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Idempotency, ModelReceipt, ObjectRow, Turn
from careerforge.services.ordinary_checker import ORDINARY_RUN_CONTRACT
from careerforge.util import fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "workflow_reclassified",
        "reverse_link_removed",
        "turn_and_run_markers_removed",
        "run_authority_removed",
        "run_authority_turn_changed",
        "run_authority_hash_changed",
        "turn_authority_marker_resealed",
    ],
)
async def test_new_ordinary_run_identity_survives_removed_mutable_links(env, change):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    started = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="fixture question",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="bound",
        ),
    )
    run = (
        started["run_id"],
        app.runtime.claim(started["run_id"], "identity-test"),
        "identity-test",
    )
    with app.db.transaction() as db:
        active, turn = db.get(AgentRun, run[0]), db.get(Turn, started["turn_id"])
        authority = db.scalar(
            select(Idempotency).where(
                Idempotency.operation == "conversation.ordinary_run", Idempotency.key == run[0]
            )
        )
        token = turn.focus[ORDINARY_CHECKER_KEY]
        assert authority.result == {
            "contract": ORDINARY_RUN_CONTRACT,
            "run_id": run[0],
            "turn_id": turn.id,
            "binding": token,
        }
        if change in {
            "workflow_reclassified",
            "reverse_link_removed",
            "turn_and_run_markers_removed",
        }:
            active.input_manifest = {
                key: value
                for key, value in active.input_manifest.items()
                if key not in {ORDINARY_CHECKER_KEY, "turn_id"}
            }
            active.input_hash = fingerprint(active.input_manifest)
            if change == "workflow_reclassified":
                active.workflow = "knowledge_build"
            else:
                turn.run_id = None
                if change == "turn_and_run_markers_removed":
                    turn.focus = {
                        key: value
                        for key, value in turn.focus.items()
                        if key != ORDINARY_CHECKER_KEY
                    }
        elif change == "run_authority_removed":
            db.delete(authority)
        elif change == "run_authority_turn_changed":
            authority.result = {**authority.result, "turn_id": "missing"}
        elif change == "run_authority_hash_changed":
            authority.request_hash = "0" * 64
        else:
            obj = db.get(ObjectRow, token["manifest_id"])
            payload = deepcopy(obj.payload)
            payload.pop("run_authority_contract")
            obj.payload, obj.content_hash = payload, fingerprint(payload)
            new_token = {**token, "manifest_hash": obj.content_hash}
            turn.focus = {**turn.focus, ORDINARY_CHECKER_KEY: new_token}
            active.input_manifest = {**active.input_manifest, ORDINARY_CHECKER_KEY: new_token}
            active.input_hash = fingerprint(active.input_manifest)
            receipt = db.scalar(
                select(Idempotency).where(
                    Idempotency.operation == "conversation.checker_turn", Idempotency.key == turn.id
                )
            )
            receipt.request_hash, receipt.result = obj.content_hash, new_token
    with pytest.raises(DomainError):
        await app.gateway.call(
            *run, "generic", "answer_checker", "test", {}, data_classes=["learning_state"]
        )
    assert not env["model"].calls
    with app.db.read() as db:
        assert not list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run[0])))
