"""Opt-in real PostgreSQL governance CAS and source maintenance, no live model calls.

Use only an explicitly provisioned disposable database, one harness run for this file.
The test never drops a database/schema and never starts external provider traffic.
"""

import asyncio
import os

import pytest
from sqlalchemy import select
from support import FixtureModel, FixtureRepositories
from test_incremental_maintenance_v16 import create_plan, import_next
from test_taxonomy_governance_v16 import approve, definition, evidence_fixture, propose

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_governance import TaxonomyDecisionInput, UpdateExecutionInput
from careerforge.errors import DomainError
from careerforge.models import ProjectSelection
from careerforge.models_governance import TaxonomyRelease


@pytest.mark.production
@pytest.mark.skipif(
    os.environ.get("CAREERFORGE_NATIVE_GOVERNANCE_TEST") != "1",
    reason="requires explicitly isolated native PostgreSQL governance database",
)
@pytest.mark.asyncio
async def test_postgres_namespace_serializes_approval_cas_and_reviewed_source_updates(tmp_path):
    url = os.environ["CAREERFORGE_TEST_DATABASE_URL"]
    assert url.startswith("postgresql")
    model, repositories = FixtureModel(), FixtureRepositories()
    app = Application(
        Settings(
            mode="test",
            database_url=url,
            allow_fixture_publication=True,
            embedding_dimensions=8,
            state_dir=tmp_path,
        ),
        model,
        repositories,
    )
    try:
        owner = app.bootstrap("governance-native-fixture")
        actor, namespace = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, namespace, 1000)
        env = {
            "app": app,
            "owner": owner,
            "actor": actor,
            "ns": namespace,
            "funding": funding,
            "model": model,
            "repos": repositories,
        }
        old, evidence = await evidence_fixture(env)
        service, first = propose(env, evidence, [definition("race.first")], key="first")
        _, second = propose(env, evidence, [definition("race.second")], key="second")
        decision = TaxonomyDecisionInput(
            expected_revision=1,
            expected_release_revision=0,
            approve=True,
            reason="Real PostgreSQL competing namespace release CAS",
        )

        def competing(proposal):
            try:
                return service.decide(actor, proposal["proposal_id"], decision)
            except DomainError as exc:
                return {"error": exc.code}

        results = await asyncio.gather(
            asyncio.to_thread(competing, first), asyncio.to_thread(competing, second)
        )
        assert sum(result.get("status") == "approved" for result in results) == 1
        assert sum(result.get("error") == "TAXONOMY_CONFLICT" for result in results) == 1
        with app.db.read() as db:
            assert len(list(db.scalars(select(TaxonomyRelease)))) == 1
        pending = second if results[0].get("status") == "approved" else first
        approve(env, service, pending)
        assert service.status(actor, namespace)["revision"] == 2
        new = import_next(env)
        plan = create_plan(env, old, new)
        assert plan["manifest"]["units"][0]["route"] == "B_targeted_review"
        scheduled = app.lifecycle.execute_update(
            actor,
            plan["plan_id"],
            UpdateExecutionInput(expected_revision=1, funding_id=funding, call_cap=10),
        )
        await app.drain()
        assert app.runtime.stage(scheduled["run_id"], "result")["publication_ids"]
        with app.db.read() as db:
            assert db.scalar(select(ProjectSelection)).source_id == old
    finally:
        app.db.engine.dispose()
