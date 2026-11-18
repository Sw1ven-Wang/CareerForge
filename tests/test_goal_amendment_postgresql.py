"""Opt-in real PostgreSQL concurrency/constraints; no external model calls.

Uses CAREERFORGE_TEST_POSTGRES_URL naming a disposable `_validation_` database
already at this candidate's schema head. Never points at native QA databases.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_goal_amendments_v1 import base_goal
from test_runtime_postgresql import pg_runtime  # noqa: F401

from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, GoalVersion

pytestmark = pytest.mark.production


def goal_env(runtime_env):
    app, actor, namespace, funding = runtime_env
    app.settings = replace(
        app.settings, workflow_engine="langgraph", session_graph_version="knowledge-session-v2"
    )
    app.db.settings = app.settings
    env = {"app": app, "actor": actor, "ns": namespace, "funding": funding}
    return env, base_goal(env)


@pytest.mark.parametrize("same_key", [True, False])
def test_goal_commands_serialize_and_exact_replay_precedes_cas(pg_runtime, same_key):  # noqa: F811
    env, (cid, _, old, _) = goal_env(pg_runtime)
    app = env["app"]
    with app.db.read() as db:
        revision = db.get(Conversation, cid).revision
    barrier = Barrier(2)

    def amend(index):
        message = DialogueMessage(
            text="补充 LangGraph",
            expected_focus_revision=revision,
            idempotency_key="concurrent" if same_key else "concurrent-" + str(index),
        )
        barrier.wait(timeout=10)
        try:
            return app.dialogue.dispatch(env["actor"], cid, message)
        except DomainError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(amend, range(2)))
    successes = [
        item for item in results if item.get("action_receipts", [{}])[0].get("status") == "applied"
    ]
    assert len(successes) == (2 if same_key else 1), results
    if same_key:
        assert results[0] == results[1]
    else:
        assert any(item.get("error") == "REVISION_CONFLICT" for item in results)
    with app.db.read() as db:
        goal_id = db.get(GoalVersion, old).goal_id
        versions = list(db.scalars(select(GoalVersion).where(GoalVersion.goal_id == goal_id)))
        assert len(versions) == 2
        assert versions[1].run_id is None or versions[0].run_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.principal_id == env["actor"])
            )
            == 1
        )
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            db.execute(
                text("UPDATE career_goal_versions SET run_id=NULL WHERE id=:id"), {"id": old}
            )
