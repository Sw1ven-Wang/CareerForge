"""Synthetic, offline source-navigation regressions, not model-quality evidence."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from careerforge.contracts import ImportSourceInput, RetrievalTaskSpec, TaskType, TurnInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, AnswerBlock, Conversation, SourceRead, SourceSet, Turn
from careerforge.services.orchestration import LEGACY_ROUTING_CONTRACT_VERSION, rule_intent
from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT
from careerforge.services.security import create_principal, set_grant
from careerforge.util import tokens


@pytest.mark.parametrize(
    "question",
    [
        "Locate Target.run",
        "定位 DefaultAgent.run",
        "Locate `Target.run`",
        'Locate "Target.run"',
        'Locate "Dispatcher.compare"',
        "Locate `Dispatcher.compare`",
        "Locate Dispatcher.compare in src/agent.py",
    ],
)
def test_qualified_identifiers_are_not_second_actions(question):
    assert rule_intent(question, {}).interpreted_intent == TaskType.LOCATE


@pytest.mark.parametrize(
    "question",
    [
        "Locate Target.run; then run tests",
        "Locate `Target.run` and modify it",
        "Locate Target.run. Run tests",
        "定位 Target.run，然后运行测试",
        'Explain "compare the projects"',
        "Do not locate Target.run",
    ],
)
def test_qualified_identifiers_do_not_hide_real_actions_or_quoted_intent(question):
    assert rule_intent(question, {}) is None


@pytest.mark.parametrize("version", [None, LEGACY_ROUTING_CONTRACT_VERSION])
def test_legacy_routing_retains_its_original_qualified_action_behavior(version):
    assert rule_intent("Locate Target.run", {}, routing_contract_version=version) is None


@pytest.mark.parametrize(
    "question,scope",
    [
        ("导入JD 熟悉 Python", "project"),
        ("搜索项目 https://github.com/example/project", "project"),
        ("搜索项目 tool invocation edge case", "focused"),
    ],
)
@pytest.mark.asyncio
async def test_authorized_background_build_freezes_project_or_focused_scope(
    env, monkeypatch, question, scope
):
    app, actor = env["app"], env["actor"]
    conversation = app.conversations.create(actor, auto_build=True)
    request = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question=question,
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="background-scope",
        ),
    )
    seen = []

    def capture_build(owner, value):
        assert owner == actor and value.namespace_id == env["ns"]
        seen.append(value)
        return "synthetic-background-id"

    monkeypatch.setattr(app.build, "start", capture_build)
    epoch = app.runtime.claim(request["run_id"], "scope-worker")
    await app.conversations.process(request["turn_id"], (request["run_id"], epoch, "scope-worker"))
    assert len(seen) == 1 and seen[0].research_scope == scope


def import_navigation_source(env, files=None, *, namespace=None, key="fixtures/navigation"):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=namespace or env["ns"],
            repository_key=key,
            commit_sha="b" * 40,
            license_id="MIT",
            source_metadata={"synthetic": True},
            files=files
            or {"src/agent.py": "class Target:\n    def run(self):\n        return 42\n"},
        ),
    )


def submit_navigation(
    env, source, question="Locate symbol Target.run", *, funded=False, legacy=False
):
    app = env["app"]
    conv = app.conversations.create(env["actor"])
    with app.db.transaction() as db:
        db.get(Conversation, conv).focus = {"project_source_ids": [source]}
    result = app.conversations.submit(
        env["actor"],
        conv,
        TurnInput(
            question=question,
            expected_revision=1,
            idempotency_key="navigation",
            funding_id=env["funding"] if funded else None,
        ),
    )
    if legacy:
        with app.db.transaction() as db:
            turn = db.get(Turn, result["turn_id"])
            turn.focus = {k: v for k, v in turn.focus.items() if k != "source_navigation_contract"}
    return result


def raw_blocks(env, turn_id):
    return [
        event["payload"]["content"]
        for event in env["app"].answers.events(env["actor"], turn_id)["events"]
        if event["type"] == "answer.block.validated"
        and event["payload"]["content"]["kind"] == "raw_source_observation"
    ]


@pytest.mark.parametrize("distractors", [9, 35])
@pytest.mark.asyncio
async def test_exact_qualified_target_ranks_before_limit_even_at_original_rank_eleven(
    env, distractors
):
    body = "".join(f"class TargetNoise{i}:\n    pass\n\n" for i in range(distractors))
    body += "class Target:\n    def run(self):\n        return 42\n"
    source = import_navigation_source(env, {"src/agent.py": body})
    request = RetrievalTaskSpec(
        turn_id="exact-symbol",
        task_type=TaskType.LOCATE,
        user_question="Locate symbol Target.run",
        focus_revision=1,
        project_source_ids=[source],
    )
    with env["app"].db.read() as db:
        old = env["app"].retrieval._symbols(
            db, env["actor"], [source], tokens(request.user_question)
        )
    if distractors == 9:
        assert old[10]["symbol"] == "Target.run"
    else:
        assert not any(row["symbol"] == "Target.run" for row in old)
    result = await env["app"].retrieval.retrieve(env["actor"], request)
    assert result["symbols"][0]["symbol"] == "Target.run"
    turn = submit_navigation(env, source)
    await env["app"].conversations.process(turn["turn_id"])
    blocks = raw_blocks(env, turn["turn_id"])
    assert blocks[0]["navigation"]["symbol"] == "Target.run"
    assert "return 42" in blocks[0]["text"]
    assert all(not block["fact_atom_ids"] and not block["publication_ids"] for block in blocks)
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_exact_path_disambiguates_same_qualified_symbol(env):
    body = "class Target:\n    def run(self):\n        return 42\n"
    source = import_navigation_source(env, {"src/a.py": body, "src/z.py": body})
    spec = RetrievalTaskSpec(
        turn_id="path",
        task_type=TaskType.LOCATE,
        user_question="Locate Target.run in src/z.py",
        focus_revision=1,
        project_source_ids=[source],
    )
    result = await env["app"].retrieval.retrieve(env["actor"], spec)
    assert (result["symbols"][0]["path"], result["symbols"][0]["symbol"]) == (
        "src/z.py",
        "Target.run",
    )


@pytest.mark.asyncio
async def test_explicit_path_and_leaf_name_outweigh_exact_name_in_other_file(env):
    source = import_navigation_source(
        env,
        {
            "src/a.py": "def run():\n    return 0\n",
            "src/z.py": "class Target:\n    def run(self):\n        return 42\n",
        },
    )
    spec = RetrievalTaskSpec(
        turn_id="path-leaf",
        task_type=TaskType.LOCATE,
        user_question="Locate run in src/z.py",
        focus_revision=1,
        project_source_ids=[source],
    )
    result = await env["app"].retrieval.retrieve(env["actor"], spec)
    assert (result["symbols"][0]["path"], result["symbols"][0]["symbol"]) == (
        "src/z.py",
        "Target.run",
    )


@pytest.mark.asyncio
async def test_character_pages_are_exact_and_long_nearby_symbol_does_not_suppress_target(env):
    # The early class is >2600 characters in <70 lines, reproducing the prior
    # fail-all shape without executing any imported source.
    broad = (
        "class TargetNoise:\n"
        + "".join("    # " + "x" * 95 + "\n" for _ in range(35))
        + "    pass\n"
    )
    target = (
        "class Target:\n    def run(self):\n"
        + "".join("        value = " + str(i) + "\n" for i in range(170))
        + "        return value\n"
    )
    source = import_navigation_source(env, {"src/agent.py": broad + target})
    turn = submit_navigation(env, source)
    await env["app"].conversations.process(turn["turn_id"])
    blocks = raw_blocks(env, turn["turn_id"])
    target_pages = [block for block in blocks if block["navigation"]["symbol"] == "Target.run"]
    assert len(target_pages) >= 3
    assert "".join(block["text"] for block in target_pages) == target.split("\n", 1)[1]
    assert target_pages[-1]["navigation_read"]["symbol_complete"]
    assert any(block["navigation"]["symbol"] == "TargetNoise" for block in blocks)
    with env["app"].db.read() as db:
        snapshot = db.get(SourceSet, source)
        for block in blocks:
            citation = block["citations"][0]
            assert len(block["text"]) <= 2600
            assert citation["end_line"] - citation["start_line"] + 1 <= 70
            assert block["text"] == "".join(
                snapshot.files[citation["path"]].splitlines(keepends=True)[
                    citation["start_line"] - 1 : citation["end_line"]
                ]
            )
            receipt, _ = env["app"].sources.validated_read(db, env["actor"], citation["read_id"])
            assert receipt.session_key == turn["turn_id"]


@pytest.mark.asyncio
async def test_failed_nearby_read_preserves_pages_and_reports_remaining_range(env):
    source = import_navigation_source(
        env,
        {
            "src/agent.py": "class Target:\n    def run(self):\n        return 42\n\nclass TargetNoise:\n    def payload(self, value='"
            + "x" * 2700
            + "'):\n        return value\n"
        },
    )
    turn = submit_navigation(env, source)
    await env["app"].conversations.process(turn["turn_id"])
    packet = env["app"].answers.events(env["actor"], turn["turn_id"])
    blocks = [
        e["payload"]["content"] for e in packet["events"] if e["type"] == "answer.block.validated"
    ]
    assert packet["status"] == "completed"
    assert blocks[0]["navigation"]["symbol"] == "Target.run"
    assert any(b.get("details") == ["source_navigation:READ_LINE_LIMIT"] for b in blocks)
    assert any(b.get("navigation", {}).get("symbol") == "TargetNoise" for b in blocks)


@pytest.mark.asyncio
async def test_page_budget_is_explicit_and_returns_cursor_not_fake_completion(env):
    source = import_navigation_source(
        env,
        {
            "src/agent.py": "class Target:\n    def run(self):\n"
            + "        value = 1\n" * 500
            + "        return value\n"
        },
    )
    turn = submit_navigation(env, source)
    await env["app"].conversations.process(turn["turn_id"])
    pages = [
        b for b in raw_blocks(env, turn["turn_id"]) if b["navigation"]["symbol"] == "Target.run"
    ]
    assert len(pages) == 5
    assert pages[-1]["navigation_read"]["budget_limited"]
    assert not pages[-1]["navigation_read"]["symbol_complete"]
    assert (
        pages[-1]["navigation_read"]["continuation"]["start_line"]
        == pages[-1]["citations"][0]["end_line"] + 1
    )


@pytest.mark.asyncio
async def test_new_pages_replay_idempotently_after_later_read_interruption(env, monkeypatch):
    source = import_navigation_source(
        env,
        {
            "src/agent.py": "class Target:\n    def run(self):\n"
            + "        value = 1\n" * 100
            + "        return value\n"
        },
    )
    turn = submit_navigation(env, source, funded=True)
    epoch = env["app"].runtime.claim(turn["run_id"], "navigation-worker")
    run = (turn["run_id"], epoch, "navigation-worker")
    original = env["app"].sources.read_span
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DomainError("TEST_CRASH", "Synthetic crash after first committed page.")
        return original(*args, **kwargs)

    monkeypatch.setattr(env["app"].sources, "read_span", interrupt)
    with pytest.raises(DomainError, match="Synthetic crash"):
        await env["app"].conversations.process(turn["turn_id"], run)
    before = deepcopy(raw_blocks(env, turn["turn_id"]))
    assert len(before) == 1
    monkeypatch.setattr(env["app"].sources, "read_span", original)
    await env["app"].conversations.process(turn["turn_id"], run)
    after = raw_blocks(env, turn["turn_id"])
    assert after[0] == before[0]
    with env["app"].db.read() as db:
        rows = db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn["turn_id"])).all()
        assert sorted(row.ordinal for row in rows) == list(range(len(rows)))
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_cancel_after_first_page_stops_next_read_without_erasing_first(env, monkeypatch):
    source = import_navigation_source(
        env,
        {
            "src/agent.py": "class Target:\n    def run(self):\n"
            + "        value = 1\n" * 100
            + "        return value\n"
        },
    )
    turn = submit_navigation(env, source, funded=True)
    epoch = env["app"].runtime.claim(turn["run_id"], "navigation-worker")
    original = env["app"].answers.commit_block

    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[4] == 0:
            with env["app"].db.read() as db:
                revision = db.get(AgentRun, turn["run_id"]).revision
            env["app"].runtime.cancel(env["actor"], turn["run_id"], revision)
        return result

    monkeypatch.setattr(env["app"].answers, "commit_block", cancel)
    with pytest.raises(DomainError) as error:
        await env["app"].conversations.process(
            turn["turn_id"], (turn["run_id"], epoch, "navigation-worker")
        )
    assert error.value.code in {"LEASE_LOST", "RUN_TERMINAL", "ANSWER_INTERRUPTED"}
    assert len(raw_blocks(env, turn["turn_id"])) == 1
    with env["app"].db.read() as db:
        assert (
            len(
                db.scalars(
                    select(SourceRead).where(SourceRead.session_key == turn["turn_id"])
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("tamper", ["source", "receipt"])
@pytest.mark.asyncio
async def test_historical_navigation_rechecks_source_and_receipt_hashes(env, tamper):
    source = import_navigation_source(env)
    turn = submit_navigation(env, source)
    await env["app"].conversations.process(turn["turn_id"])
    blocks = raw_blocks(env, turn["turn_id"])
    assert blocks
    with env["app"].db.transaction() as db:
        if tamper == "source":
            row = db.get(SourceSet, source)
            row.files = {**row.files, "src/agent.py": row.files["src/agent.py"].replace("42", "99")}
        else:
            row = db.get(SourceRead, blocks[0]["citations"][0]["read_id"])
            row.quote_hash = "0" * 64
    packet = env["app"].answers.events(env["actor"], turn["turn_id"])
    assert any(event["type"] == "answer.evidence_invalidated" for event in packet["events"])
    assert not any(
        event["type"] == "answer.block.validated" and event["payload"]["content"] == blocks[0]
        for event in packet["events"]
    )


@pytest.mark.asyncio
async def test_source_acl_is_not_bypassed_by_exact_name_or_saved_navigation(env):
    app = env["app"]
    source = import_navigation_source(env)
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "navigation-reader")
        set_grant(db, env["actor"], env["ns"], reader.id, "reader", True)
    spec = RetrievalTaskSpec(
        turn_id="acl",
        task_type=TaskType.LOCATE,
        user_question="Locate symbol Target.run",
        focus_revision=1,
        project_source_ids=[source],
    )
    result = await app.retrieval.retrieve(reader.id, spec)
    assert result["symbols"][0]["symbol"] == "Target.run"
    with app.db.transaction() as db:
        set_grant(db, env["actor"], env["ns"], reader.id, "reader", False)
    with pytest.raises(DomainError):
        await app.retrieval.retrieve(reader.id, spec, local_navigation=result["symbols"][0])


@pytest.mark.asyncio
async def test_unmarked_legacy_navigation_keeps_original_read_contract(env):
    source = import_navigation_source(env)
    turn = submit_navigation(env, source, legacy=True)
    await env["app"].conversations.process(turn["turn_id"])
    blocks = raw_blocks(env, turn["turn_id"])
    assert blocks and all("navigation_contract" not in block for block in blocks)
    assert all("continuation" not in block["citations"][0] for block in blocks)
    assert blocks[0]["navigation"]["symbol"] == "Target"
    assert SOURCE_NAVIGATION_CONTRACT not in str(blocks)
