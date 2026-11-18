from copy import deepcopy
import pytest
from careerforge.desk_continuation import renew_if_stale
from careerforge.errors import DomainError
from careerforge.models import Conversation
from careerforge.services import ordinary_checker


def stale(env, monkeypatch):
    identity = ordinary_checker.ordinary_checker_identity()
    with monkeypatch.context() as patch:
        patch.setattr(ordinary_checker, "ordinary_checker_identity", lambda: {**identity, "implementation_hash": "old-test-implementation"})
        return env["app"].conversations.create(env["actor"])


def test_current_conversation_unchanged(env):
    app, actor = env["app"], env["actor"]
    cid = app.conversations.create(actor)
    assert renew_if_stale(app, actor, cid) == cid


def test_successor_preserves_old_receipts_and_is_idempotent(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    cid = stale(env, monkeypatch)
    before = deepcopy(app.conversations.detail(actor, cid))
    successor = renew_if_stale(app, actor, cid)
    assert successor != cid
    assert renew_if_stale(app, actor, cid) == successor
    assert app.conversations.detail(actor, cid) == before
    detail = app.conversations.detail(actor, successor)
    assert detail["preferences"]["predecessor_conversation_id"] == cid
    assert detail["preferences"]["renewal_notice"]
    assert not detail["focus"].get("active_plan_id")


def test_busy_and_inaccessible_source_do_not_create_successor(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    cid = stale(env, monkeypatch)
    with pytest.raises(DomainError, match="尚未结束"):
        renew_if_stale(app, actor, cid, busy=True)
    with app.db.transaction() as db:
        db.get(Conversation, cid).focus = {"project_source_ids": ["missing-source"]}
    with pytest.raises(DomainError):
        renew_if_stale(app, actor, cid)


@pytest.mark.asyncio
async def test_successor_carries_only_accessible_project(env, monkeypatch):
    from conftest import publish_fixture
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    cid = stale(env, monkeypatch)
    with app.db.transaction() as db:
        conv = db.get(Conversation, cid)
        app.workflows.create_in(db, conv, target={"target": "https://github.com/example/repo", "source_ids": [source]})
        conv.focus = {**conv.focus, "evidence_answer_state": {"unsafe": True}}
    new = renew_if_stale(app, actor, cid)
    detail = app.conversations.detail(actor, new)
    assert detail["focus"]["project_source_ids"] == [source]
    assert "evidence_answer_state" not in detail["focus"]
