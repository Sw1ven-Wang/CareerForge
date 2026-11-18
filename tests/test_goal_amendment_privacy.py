"""Command sources share the existing narrow workflow ownership/erasure scope."""

import json

import pytest
from test_goal_amendments_v1 import amend_in, base_goal
from test_privacy_v16 import confirm

from careerforge.errors import DomainError
from careerforge.models import GoalVersion, ObjectRow
from careerforge.models_dialogue import GoalAddendum
from careerforge.services.research_coverage import read_goal_version
from careerforge.services.security import create_principal


async def test_export_and_erasure_scrub_command_source_keep_origin_check(env):
    cid, wid, _, _ = base_goal(env)
    app, actor = env["app"], env["actor"]
    secret = "Private-Technology-Z918"
    with app.db.transaction() as db:
        child, result, _, _ = amend_in(env, db, cid, wid, technology=secret)
        version_id = child.goal_version_id
    export = app.privacy.export(actor)
    assert secret in json.dumps(export, ensure_ascii=False)
    assert export["records"]["workflow_goal_addenda"]
    await confirm(app.privacy, actor, app.privacy.propose(actor))
    with app.db.read() as db:
        addendum = db.get(GoalAddendum, result["goal_addendum_id"])
        version = db.get(GoalVersion, version_id)
        assert addendum.manifest == {}
        assert version.origin_contract == "command-goal-v1" and version.run_id is None
        assert version.source_text == "" and version.requirements == {}
        assert db.get(ObjectRow, addendum.id).payload == {}
        with pytest.raises(DomainError):
            read_goal_version(db, actor, version_id)


def test_cross_owner_goal_and_command_source_cannot_be_read(env):
    cid, wid, _, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "other-amendment-owner")
        other_id = other.id
        child, _, _, _ = amend_in(env, db, cid, wid)
        with pytest.raises(DomainError):
            read_goal_version(db, other_id, child.goal_version_id)
