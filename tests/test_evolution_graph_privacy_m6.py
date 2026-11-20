"""Synthetic original-generation fences, not privacy/legal acceptance."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from test_evolution_graph_m6 import candidate_session, replay_session

from careerforge.errors import DomainError
from careerforge.models import AgentRun, Idempotency
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.evolution_privacy import validate_run_generations_in
from careerforge.util import fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["candidate", "replay"])
async def test_original_generation_changed_before_domain_commit_never_persists_effect(
    env, monkeypatch, kind
):
    app = env["app"]
    if kind == "candidate":
        conv, _, _, admission = await candidate_session(env)
        name = "commit_reflection"
    else:
        conv, admission = await replay_session(env, cases=1, cap=1)
        name = "commit_replay_evaluation"
    original = getattr(app.evolution, name)
    captured = {}

    def change_generation_then_commit(run, *args, **kwargs):
        captured["run_id"] = run.id
        captured["calls"] = len(env["model"].calls)
        with app.db.transaction() as db:
            state = db.get(PrivacyState, env["actor"])
            assert state.blocked is False
            state.generation += 1
        return original(run, *args, **kwargs)

    monkeypatch.setattr(app.evolution, name, change_generation_then_commit)
    await app.drain()
    assert captured
    assert len(env["model"].calls) == captured["calls"]
    assert app.runtime.stage(captured["run_id"], "result") is None
    with app.db.read() as db:
        assert not db.scalar(
            select(Idempotency.id).where(
                Idempotency.operation == "evolution.paid.effect.v1",
                Idempotency.key == captured["run_id"],
            )
        )
        run = db.get(AgentRun, captured["run_id"])
        assert run.calls_consumed == (1 if kind == "candidate" else 0)
        assert run.status == "failed"
    # No derived private effect and no retry under a fresh model key.
    await app.execute(captured["run_id"])
    assert len(env["model"].calls) == captured["calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "bool", "changed_and_rehashed"])
async def test_generation_map_is_bound_to_original_admission_not_current_repair(env, fault):
    app = env["app"]
    conv, _, _, admission = await candidate_session(env)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    with app.db.transaction() as db:
        run = db.get(AgentRun, detail["run_id"])
        manifest = deepcopy(run.input_manifest)
        if fault == "missing":
            manifest["evolution_graph"].pop("privacy_generations")
        else:
            manifest["evolution_graph"]["privacy_generations"][env["actor"]] = (
                False if fault == "bool" else 1
            )
        run.input_manifest = manifest
        run.input_hash = fingerprint(manifest)
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        with pytest.raises(DomainError):
            validate_run_generations_in(db, run)


@pytest.mark.asyncio
async def test_pre_dispatch_owner_change_without_closure_hash_change_has_zero_egress(
    env, monkeypatch
):
    from careerforge.models import AccessGrant, ModelReceipt, Namespace
    from careerforge.services import evolution_privacy
    from careerforge.services.dependencies import check_closure
    from careerforge.services.security import create_principal

    app = env["app"]
    with app.db.transaction() as db:
        new_owner, _, _ = create_principal(db, "m6-pre-dispatch-owner")
        new_owner_id = new_owner.id
        # Preserve the actor's explicit administrator authority if ownership
        # changes. The negative assertion concerns frozen owner coverage, not ACL.
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=env["actor"], role="admin"))
    _, _, _, _ = await candidate_session(env)
    calls_before = len(env["model"].calls)
    original = evolution_privacy.validate_run_generations_in
    observed = {"dependency_checks": 0}

    def change_at_pre_dispatch(db, run, *, dependencies=None):
        if dependencies is not None and run.workflow == "evolution":
            observed["dependency_checks"] += 1
            if observed["dependency_checks"] == 2:
                # First check was the committed receipt reservation. This is
                # the actual last authorization window before provider dispatch.
                receipts = list(
                    db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
                )
                assert len(receipts) == 1 and receipts[0].response is None
                assert (
                    new_owner_id not in run.input_manifest["evolution_graph"]["privacy_generations"]
                )
                before = check_closure(db, run.principal_id, dependencies).closure_hash
                db.get(Namespace, run.namespace_id).owner_id = new_owner_id
                db.flush()
                assert check_closure(db, run.principal_id, dependencies).closure_hash == before
                observed["run_id"] = run.id
        return original(db, run, dependencies=dependencies)

    monkeypatch.setattr(evolution_privacy, "validate_run_generations_in", change_at_pre_dispatch)
    await app.drain()
    assert observed.get("run_id") and observed["dependency_checks"] == 2
    assert len(env["model"].calls) == calls_before
    assert app.runtime.stage(observed["run_id"], "result") is None
