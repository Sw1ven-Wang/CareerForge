"""Synthetic offline regressions. These are not fresh model-quality evidence."""

import pytest
from test_source_navigation_v163 import import_navigation_source

from careerforge.contracts import RetrievalTaskSpec, TaskType


def spec_for(source, question, *, task=TaskType.LOCATE):
    return RetrievalTaskSpec(
        turn_id="synthetic-path-navigation",
        task_type=task,
        user_question=question,
        focus_revision=1,
        project_source_ids=[source] if source else [],
    )


@pytest.mark.asyncio
async def test_absent_explicit_path_never_falls_back_to_similar_symbols(env):
    source = import_navigation_source(
        env, {"src/nearby.py": "class Agent:\n    def run(self):\n        return 42\n"}
    )
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate Agent.run in src/missing.py")
    )
    assert result["symbols"] == []
    assert result["knowledge_units"] == []
    assert "requested_source_path_missing_from_snapshot" in result["uncovered_questions"]


@pytest.mark.asyncio
async def test_existing_explicit_path_does_not_return_same_symbol_from_other_files(env):
    body = "class Target:\n    def run(self):\n        return 42\n"
    source = import_navigation_source(env, {"src/a.py": body, "src/z.py": body})
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate Target.run in src/z.py")
    )
    assert result["symbols"]
    assert {symbol["path"] for symbol in result["symbols"]} == {"src/z.py"}


@pytest.mark.asyncio
async def test_snapshot_missing_question_with_later_negation_preserves_target(env):
    source = import_navigation_source(env, {"src/agent.py": "def run_agent():\n    return 42\n"})
    question = (
        "仅限当前已导入的固定快照，定位 src/_agent_loop.py 的模型调用与工具执行循环并给出源码行号。"
        "若该文件未导入，请明确当前快照缺失，不推断仓库不存在。只做源码定位。"
    )
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, question))
    assert result["symbols"] == []
    assert "requested_source_path_missing_from_snapshot" in result["uncovered_questions"]


@pytest.mark.parametrize(
    "question,path",
    [
        ("Locate src/a.py", "src/a.py"),
        ("Locate `src/a.py`", "src/a.py"),
        ('Locate "src/a.py"', "src/a.py"),
        ("Locate Target.run in src/a.py", "src/a.py"),
        ("Where is Target.run in src/a.py?", "src/a.py"),
        ("Locate Target.run in file src/a.py", "src/a.py"),
        ("Locate file root.custom_language", "root.custom_language"),
        ("Locate file README", "README"),
        ("Locate Target.run in a.py", "a.py"),
        ("定位 src/a.py 中的 Target.run", "src/a.py"),
        ("在 src/a.py 中定位 Target.run", "src/a.py"),
        ("src/a.py 中的 Target.run 在哪里", "src/a.py"),
        ("Locate Target.run in src/a.py, not src/b.py", "src/a.py"),
        ("Locate src/a.py. Do not infer global absence.", "src/a.py"),
        ("Locate src/a.py rather than src/b.py", "src/a.py"),
    ],
)
def test_question_only_explicit_path_grammar(question, path):
    from careerforge.services.explicit_source_paths import requested_source_path

    assert requested_source_path(question) == {"status": "exact", "path": path}


@pytest.mark.parametrize(
    "question",
    [
        "Locate Target.run",
        'Locate "Target.run"',
        "Locate fixtures/navigation 的 Target.run",
        "Compare src/a.py and src/b.py",
        "Explain src/a.py",
        'Explain "locate src/a.py"',
        "Explain `locate src/a.py`",
        "> Locate src/a.py\nExplain this example.",
        "The docs mention src/a.py. Locate Target.run.",
        "Locate the function mentioned in src/a.py",
        "In comparison to src/a.py, locate Target.run",
    ],
)
def test_context_and_comparisons_do_not_create_hard_path_scope(question):
    from careerforge.services.explicit_source_paths import requested_source_path

    assert requested_source_path(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "Do not locate src/a.py",
        "不要定位 src/a.py",
        "Locate Target.run in src/a.py or src/b.py",
        "Locate Target.run in src/a.py and src/b.py",
        "Locate src/a.py; locate src/b.py",
    ],
)
def test_negated_or_multiple_navigation_targets_require_clarification(question):
    from careerforge.services.explicit_source_paths import requested_source_path

    assert requested_source_path(question)["status"] == "ambiguous"


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "src/../../secret.py",
        "src/./agent.py",
        "src//agent.py",
        "/etc/passwd",
        "/tmp/agent.py",
        "C:\\private\\agent.py",
        "src\\agent.py",
        "src/%2e%2e/agent.py",
        "src/agent.py?token=secret",
        "src/agent.py#L1",
        "https://example.test/agent.py",
        ".git/config.py",
        "src/.env/key.py",
        "src/agent\x00.py",
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_escaping_paths_never_read_a_substitute(env, path):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, "Locate " + path))
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["source_path_resolution"] == {
        "status": "clarification_required",
        "reason": "requested_source_path_invalid",
    }
    assert not env["model"].calls


@pytest.mark.parametrize("path", ["src/Agent.py", "agent.py", "other/agent.py", "src/agent.py.bak"])
@pytest.mark.asyncio
async def test_no_case_fold_basename_suffix_or_similar_path_substitution(env, path):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate Target.run in " + path)
    )
    assert result["symbols"] == []
    assert result["source_path_resolution"]["status"] == "missing_from_selected_snapshot"


def answer_blocks(env, turn):
    return [
        event["payload"]["content"]
        for event in env["app"].answers.events(env["actor"], turn["turn_id"])["events"]
        if event["type"] == "answer.block.validated"
    ]


@pytest.mark.asyncio
async def test_absent_path_turn_reports_snapshot_gap_and_zero_source_reads(env):
    from sqlalchemy import select
    from test_source_navigation_v163 import submit_navigation

    from careerforge.models import SourceRead

    source = import_navigation_source(env)
    turn = submit_navigation(env, source, "Locate Target.run in src/missing.py")
    await env["app"].conversations.process(turn["turn_id"])
    blocks = answer_blocks(env, turn)
    assert len(blocks) == 1 and blocks[0]["kind"] == "evidence_gap"
    assert blocks[0]["task_satisfied"] is False
    assert blocks[0]["details"] == ["requested_source_path_missing_from_snapshot"]
    assert "当前所选" in blocks[0]["text"] and "这不表示" in blocks[0]["text"]
    assert "agent.py" not in str(blocks)
    with env["app"].db.read() as db:
        assert db.scalar(select(SourceRead.id)) is None
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_exact_target_keeps_verified_whole_line_paging_only_in_that_file(env):
    from sqlalchemy import select
    from test_source_navigation_v163 import raw_blocks, submit_navigation

    from careerforge.models import SourceRead

    body = (
        "class Target:\n    def run(self):\n"
        + "        value = 1\n" * 100
        + "        return value\n"
    )
    source = import_navigation_source(env, {"src/a.py": body, "src/z.py": body})
    turn = submit_navigation(env, source, "Locate Target.run in src/z.py")
    await env["app"].conversations.process(turn["turn_id"])
    blocks = raw_blocks(env, turn["turn_id"])
    assert blocks and {b["navigation"]["path"] for b in blocks} == {"src/z.py"}
    pages = [b for b in blocks if b["navigation"]["symbol"] == "Target.run"]
    assert len(pages) == 2 and pages[-1]["navigation_read"]["symbol_complete"]
    assert "".join(b["text"] for b in pages) == "".join(body.splitlines(keepends=True)[1:])
    with env["app"].db.read() as db:
        assert set(db.scalars(select(SourceRead.path))) == {"src/z.py"}
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_present_file_without_indexed_symbol_is_unresolved_not_missing(env):
    from test_source_navigation_v163 import submit_navigation

    source = import_navigation_source(env, {"src/settings.py": "VALUE = 42\n"})
    turn = submit_navigation(env, source, "Locate Target.run in src/settings.py")
    await env["app"].conversations.process(turn["turn_id"])
    blocks = answer_blocks(env, turn)
    assert blocks[0]["details"] == ["requested_source_symbol_unresolved"]
    assert blocks[0]["source_path_resolution"]["status"] == "available"
    assert blocks[0]["task_satisfied"] is False


@pytest.mark.asyncio
async def test_hard_topics_do_not_disappear_when_path_exists_without_reviewed_units(env):
    source = import_navigation_source(env)
    spec = spec_for(source, "Locate Target.run in src/agent.py")
    spec.topic_keys = ["agent.tool_calling"]
    result = await env["app"].retrieval.retrieve(env["actor"], spec)
    assert result["symbols"] == []
    assert result["uncovered_questions"] == ["requested_source_symbol_unresolved"]
    assert result["source_path_resolution"]["status"] == "available"


@pytest.mark.asyncio
async def test_unrelated_reviewed_publication_cannot_clear_absent_path_gap(env):
    from conftest import publish_fixture

    source, publication, _ = await publish_fixture(env)
    before = len(env["model"].calls)
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate echo in src/missing.py")
    )
    assert result["knowledge_units"] == result["symbols"] == []
    assert result["candidate_count"] == result["total_project_groups"] == 0
    assert result["sampled_source_ids"] == []
    assert publication not in str(result)
    assert result["uncovered_questions"] == ["requested_source_path_missing_from_snapshot"]
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_path_requires_explicit_single_snapshot_not_account_wide_search(env):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(None, "Locate Target.run in src/agent.py")
    )
    assert result["source_path_resolution"] == {
        "status": "clarification_required",
        "reason": "requested_source_scope_required",
    }
    assert result["symbols"] == [] and source not in str(result)
    assert result["candidate_count"] == result["total_project_groups"] == 0
    spec = spec_for(source, "Locate Target.run in src/agent.py")
    second = import_navigation_source(env, key="fixtures/second")
    spec.project_source_ids.append(second)
    result = await env["app"].retrieval.retrieve(env["actor"], spec)
    assert result["source_path_resolution"]["reason"] == "requested_source_scope_required"
    assert source not in str(result) and second not in str(result)


@pytest.mark.asyncio
async def test_inaccessible_file_existence_does_not_change_authorized_snapshot_response(env):
    from careerforge.services.security import create_principal

    app = env["app"]
    public_source = import_navigation_source(env)
    first = await app.retrieval.retrieve(
        env["actor"], spec_for(public_source, "Locate Target.run in src/secret.py")
    )
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "synthetic-private-owner")
    private_env = {**env, "actor": other.id, "ns": namespace.id}
    hidden = import_navigation_source(
        private_env,
        {"src/secret.py": "class Target:\n    def run(self):\n        return 'PRIVATE'\n"},
        key="private/hidden-repository",
    )
    second = await app.retrieval.retrieve(
        env["actor"], spec_for(public_source, "Locate Target.run in src/secret.py")
    )
    assert first["source_path_resolution"] == second["source_path_resolution"]
    assert second["symbols"] == second["knowledge_units"] == []
    assert all(value not in str(second) for value in (hidden, "PRIVATE", "hidden-repository"))
    from careerforge.errors import DomainError

    with pytest.raises(DomainError) as failure:
        await app.retrieval.retrieve(env["actor"], spec_for(hidden, "Locate src/secret.py"))
    assert "secret.py" not in str(failure.value) and "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("path", ["src/agent.py", "src/missing.py"])
@pytest.mark.asyncio
async def test_revoked_grant_invalidates_path_cache_and_historical_gap_or_read(env, path):
    from test_source_navigation_v163 import submit_navigation

    from careerforge.errors import DomainError
    from careerforge.services.security import create_principal, set_grant

    app, owner = env["app"], env["actor"]
    source = import_navigation_source(env)
    with app.db.transaction() as db:
        reader, namespace, _ = create_principal(db, "synthetic-path-reader")
        set_grant(db, owner, env["ns"], reader.id, "reader", True)
    reader_env = {**env, "actor": reader.id, "ns": namespace.id}
    question = "Locate Target.run in " + path
    request = spec_for(source, question)
    await app.retrieval.retrieve(reader.id, request)
    turn = submit_navigation(reader_env, source, question)
    await app.conversations.process(turn["turn_id"])
    assert answer_blocks(reader_env, turn)
    with app.db.transaction() as db:
        set_grant(db, owner, env["ns"], reader.id, "reader", False)
    with pytest.raises(DomainError):
        await app.retrieval.retrieve(reader.id, request)
    assert answer_blocks(reader_env, turn) == []
    events = app.answers.events(reader.id, turn["turn_id"])["events"]
    assert any(e["type"] == "answer.evidence_invalidated" for e in events)


@pytest.mark.parametrize("tamper", ["files", "commit", "scope_hash"])
@pytest.mark.asyncio
async def test_source_hash_identity_is_rechecked_for_missing_path_and_saved_gap(env, tamper):
    from test_source_navigation_v163 import submit_navigation

    from careerforge.errors import DomainError
    from careerforge.models import SourceSet

    app = env["app"]
    source = import_navigation_source(env)
    question = "Locate Target.run in src/missing.py"
    turn = submit_navigation(env, source, question)
    await app.conversations.process(turn["turn_id"])
    with app.db.transaction() as db:
        row = db.get(SourceSet, source)
        if tamper == "files":
            row.files = {**row.files, "src/missing.py": "def injected(): pass\n"}
        elif tamper == "commit":
            row.commit_sha = "c" * 40
        else:
            row.scope_hash = "forged"
    with pytest.raises(DomainError):
        await app.retrieval.retrieve(env["actor"], spec_for(source, question))
    assert answer_blocks(env, turn) == []


@pytest.mark.asyncio
async def test_poisoned_cache_and_local_hint_cannot_override_exact_path(env, monkeypatch):
    source = import_navigation_source(
        env,
        {
            "src/a.py": "class Target:\n    def run(self):\n        return 1\n",
            "src/z.py": "class Target:\n    def run(self):\n        return 2\n",
        },
    )
    outside = {
        "source_id": source,
        "path": "src/a.py",
        "symbol": "Target.run",
        "start_line": 2,
        "end_line": 3,
        "parser": "python-ast",
    }

    async def poisoned(*args, **kwargs):
        return {
            "ids": ["forged-publication"],
            "trace": {"ast_symbol": [outside]},
            "query_rewrite": {"variants": ["Locate Target.run in src/a.py"]},
        }

    monkeypatch.setattr(env["app"].cache, "singleflight", poisoned)
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate Target.run in src/z.py"), local_navigation=outside
    )
    assert result["symbols"] and {s["path"] for s in result["symbols"]} == {"src/z.py"}
    assert result["knowledge_units"] == []


@pytest.mark.asyncio
async def test_missing_path_and_legacy_cache_contracts_are_separate(env):
    source = import_navigation_source(env)
    spec = spec_for(source, "Locate Target.run in src/missing.py")
    legacy = await env["app"].retrieval.retrieve(env["actor"], spec, source_path_contract=None)
    assert legacy["symbols"]
    old_keys = set(env["app"].cache.values)
    current = await env["app"].retrieval.retrieve(env["actor"], spec)
    assert current["symbols"] == []
    assert set(env["app"].cache.values) > old_keys
    assert "source_path_resolution" not in legacy


@pytest.mark.asyncio
async def test_old_completed_receipt_is_read_unchanged_not_relabelled_as_fixed(env):
    from copy import deepcopy

    from test_source_navigation_v163 import submit_navigation

    from careerforge.models import Turn

    source = import_navigation_source(env)
    turn = submit_navigation(env, source, "Locate Target.run in src/missing.py")
    with env["app"].db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        row.focus = {
            key: value for key, value in row.focus.items() if key != "source_path_contract"
        }
    await env["app"].conversations.process(turn["turn_id"])
    before = deepcopy(answer_blocks(env, turn))
    assert before and all(b["kind"] == "raw_source_observation" for b in before)
    await env["app"].conversations.process(turn["turn_id"])
    assert answer_blocks(env, turn) == before
    assert not env["model"].calls


@pytest.mark.parametrize(
    "model_task", [TaskType.LOCATE, TaskType.IMPLEMENTATION, TaskType.DISCOVER]
)
@pytest.mark.asyncio
async def test_model_cannot_replace_user_target_or_change_navigation_into_other_action(
    env, model_task
):
    from test_source_navigation_v163 import submit_navigation

    from careerforge.contracts import ModelOutput

    source = import_navigation_source(env)
    question = "Locate Target.run in src/missing.py. Do not infer repository absence."
    env["model"].overrides["coordinator"] = lambda request: ModelOutput(
        structured={
            "interpreted_intent": model_task.value,
            "confidence": 0.99,
            "proposed_next_action": "direct_tool"
            if model_task == TaskType.LOCATE
            else "retrieve_and_tutor",
            "project_source_ids": [source],
            "code_focus_ref": "src/agent.py",
        },
        synthetic=True,
    )
    turn = submit_navigation(env, source, question, funded=True)
    app = env["app"]
    epoch = app.runtime.claim(turn["run_id"], "synthetic-path-worker")
    await app.conversations.process(
        turn["turn_id"], (turn["run_id"], epoch, "synthetic-path-worker")
    )
    blocks = answer_blocks(env, turn)
    assert len(blocks) == 1 and blocks[0]["kind"] == "evidence_gap"
    assert blocks[0]["task_satisfied"] is False
    assert "src/agent.py" not in str(blocks)
    assert [request["role"] for request in env["model"].calls] == ["coordinator"]
    assert blocks[0]["details"] == [
        "requested_source_path_missing_from_snapshot"
        if model_task == TaskType.LOCATE
        else "requested_source_navigation_intent_conflict"
    ]
    result = app.runtime.stage(turn["run_id"], "result")
    assert result["task_satisfied"] is False
    assert result["uncovered_questions"] == blocks[0]["details"]
    assert result["navigation_outcome"] == "not_completed"


@pytest.mark.asyncio
async def test_model_cannot_manufacture_snapshot_scope_when_only_path_was_requested(env):
    from careerforge.contracts import ModelOutput, TurnInput

    app = env["app"]
    source = import_navigation_source(env)
    question = "Locate src/agent.py. Do not infer repository absence."
    env["model"].overrides["coordinator"] = lambda request: ModelOutput(
        structured={
            "interpreted_intent": "locate_symbol",
            "confidence": 0.99,
            "proposed_next_action": "direct_tool",
            "project_source_ids": [source],
        },
        synthetic=True,
    )
    conv = app.conversations.create(env["actor"])
    turn = app.conversations.submit(
        env["actor"],
        conv,
        TurnInput(
            question=question,
            expected_revision=1,
            idempotency_key="manufactured-scope",
            funding_id=env["funding"],
        ),
    )
    epoch = app.runtime.claim(turn["run_id"], "synthetic-path-worker")
    await app.conversations.process(
        turn["turn_id"], (turn["run_id"], epoch, "synthetic-path-worker")
    )
    blocks = answer_blocks(env, turn)
    assert len(blocks) == 1 and blocks[0]["gap_category"] == "scope"
    assert source not in str(blocks) and "fixtures/navigation" not in str(blocks)


@pytest.mark.asyncio
async def test_literal_user_repository_scope_resolves_original_style_request(env):
    from careerforge.contracts import ModelOutput, TurnInput

    app = env["app"]
    source = import_navigation_source(env)
    question = (
        "仅限本次已导入的 fixtures/navigation 固定快照，定位 src/_agent_loop.py 的模型调用与工具执行循环并给出源码行号。"
        "若该文件未导入，请明确当前快照缺失，不推断仓库不存在。只做源码定位。"
    )
    env["model"].overrides["coordinator"] = lambda request: ModelOutput(
        structured={
            "interpreted_intent": "locate_symbol",
            "confidence": 0.99,
            "proposed_next_action": "direct_tool",
            "project_source_ids": [source],
            "code_focus_ref": "src/agent.py",
        },
        synthetic=True,
    )
    turn = app.conversations.submit(
        env["actor"],
        app.conversations.create(env["actor"]),
        TurnInput(
            question=question,
            expected_revision=1,
            idempotency_key="literal-scope",
            funding_id=env["funding"],
        ),
    )
    epoch = app.runtime.claim(turn["run_id"], "synthetic-path-worker")
    await app.conversations.process(
        turn["turn_id"], (turn["run_id"], epoch, "synthetic-path-worker")
    )
    blocks = answer_blocks(env, turn)
    assert blocks[0]["details"] == ["requested_source_path_missing_from_snapshot"]
    assert [r["role"] for r in env["model"].calls] == ["coordinator"]


@pytest.mark.asyncio
async def test_tampered_retrieval_stage_cannot_inject_outside_path_before_read(env):
    from copy import deepcopy

    from test_source_navigation_v163 import submit_navigation

    from careerforge.errors import DomainError

    source = import_navigation_source(
        env, {"src/agent.py": "def run(): return 1\n", "src/other.py": "def run(): return 2\n"}
    )
    turn = submit_navigation(env, source, "Locate run in src/agent.py")
    spec = spec_for(source, "Locate run in src/agent.py").model_copy(
        update={"turn_id": turn["turn_id"]}
    )
    evidence = await env["app"].retrieval.retrieve(env["actor"], spec)
    forged = deepcopy(evidence)
    forged["symbols"][0]["path"] = "src/other.py"
    from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT

    with pytest.raises(DomainError) as failure:
        env["app"].conversations._navigate_source(
            env["actor"], turn["turn_id"], forged, None, SOURCE_NAVIGATION_CONTRACT
        )
    assert failure.value.code == "SOURCE_PATH_CONFLICT"
    assert answer_blocks(env, turn) == []


@pytest.mark.parametrize("remove_contract", [False, True])
@pytest.mark.asyncio
async def test_raw_answer_commit_rechecks_user_target_even_with_valid_other_file_receipt(
    env, remove_contract
):
    from test_source_navigation_v163 import submit_navigation

    from careerforge.errors import DomainError
    from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT

    app = env["app"]
    source = import_navigation_source(env, {"src/other.py": "def run(): return 2\n"})
    turn = submit_navigation(env, source, "Locate run in src/missing.py")
    read = app.sources.read_span(
        env["actor"], source, "src/other.py", 1, 1, "tutor", turn["turn_id"]
    )
    content = {
        "kind": "raw_source_observation",
        "text": read["content"],
        "source_id": source,
        "publication_ids": [],
        "fact_atom_ids": [],
        "citations": [read],
        "checked": "source_position",
        "navigation": {
            "source_id": source,
            "path": "src/other.py",
            "symbol": "run",
            "start_line": 1,
            "end_line": 1,
        },
    }
    if not remove_contract:
        content["navigation_contract"] = SOURCE_NAVIGATION_CONTRACT
    with pytest.raises(DomainError) as failure:
        app.answers.commit_block(env["actor"], turn["turn_id"], content, None, 0)
    assert failure.value.code == "ANSWER_SOURCE_PATH"


@pytest.mark.asyncio
async def test_path_constraint_does_not_sidestep_cancellation_between_pages(env, monkeypatch):
    from sqlalchemy import select
    from test_source_navigation_v163 import raw_blocks, submit_navigation

    from careerforge.errors import DomainError
    from careerforge.models import AgentRun, SourceRead

    app = env["app"]
    body = (
        "class Target:\n    def run(self):\n"
        + "        value = 1\n" * 100
        + "        return value\n"
    )
    source = import_navigation_source(env, {"src/agent.py": body})
    turn = submit_navigation(env, source, "Locate Target.run in src/agent.py", funded=True)
    epoch = app.runtime.claim(turn["run_id"], "synthetic-path-worker")
    original = app.answers.commit_block

    def cancel(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[4] == 0:
            with app.db.read() as db:
                revision = db.get(AgentRun, turn["run_id"]).revision
            app.runtime.cancel(env["actor"], turn["run_id"], revision)
        return value

    monkeypatch.setattr(app.answers, "commit_block", cancel)
    with pytest.raises(DomainError) as failure:
        await app.conversations.process(
            turn["turn_id"], (turn["run_id"], epoch, "synthetic-path-worker")
        )
    assert failure.value.code in {"LEASE_LOST", "RUN_TERMINAL", "ANSWER_INTERRUPTED"}
    assert len(raw_blocks(env, turn["turn_id"])) == 1
    with app.db.read() as db:
        assert (
            len(
                db.scalars(
                    select(SourceRead).where(SourceRead.session_key == turn["turn_id"])
                ).all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_wrong_retrieval_task_cannot_use_exact_target_as_soft_hint(env):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(
        env["actor"],
        spec_for(source, "Locate Target.run in src/agent.py", task=TaskType.IMPLEMENTATION),
    )
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["uncovered_questions"] == ["requested_source_navigation_intent_conflict"]
    assert not env["model"].calls


@pytest.mark.parametrize(
    "question,status",
    [
        ("Please show me src/missing.py", "missing_from_selected_snapshot"),
        ("Please show us src/missing.py", "missing_from_selected_snapshot"),
        ("Please show the contents of src/missing.py", "clarification_required"),
        ("定位 src/a.py或src/b.py", "clarification_required"),
        ("定位 src/a.py和src/b.py", "clarification_required"),
        ("定位 src/a.py、src/b.py", "clarification_required"),
        ("Locate src/a.py, src/b.py", "clarification_required"),
    ],
)
@pytest.mark.asyncio
async def test_unhandled_path_attachment_or_unspaced_alternatives_never_fall_back(
    env, question, status
):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, question))
    assert result["source_path_resolution"]["status"] == status
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["uncovered_questions"]


@pytest.mark.parametrize(
    "question",
    [
        'The keyword "locate" is in src/a.py',
        '"Locate" src/a.py',
        "The word locate appears in src/a.py",
    ],
)
def test_quoted_action_word_or_linguistic_mention_cannot_create_scope(question):
    from careerforge.services.explicit_source_paths import requested_source_path

    assert requested_source_path(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "Locate src/Makefile",
        "Locate Target.run in src/Makefile",
        'Locate "src/处理.py"',
        'Locate "src/file name.py"',
        "Locate src/处理.py",
        "定位 源码/agent.py",
    ],
)
@pytest.mark.asyncio
async def test_unsupported_but_explicit_path_syntax_clarifies_instead_of_unscoped_lookup(
    env, question
):
    source = import_navigation_source(env)
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, question))
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["source_path_resolution"]["status"] == "clarification_required"


@pytest.mark.asyncio
async def test_explicit_file_marker_accepts_extensionless_relative_path(env):
    source = import_navigation_source(env, {"src/Makefile": "all:\n\techo synthetic\n"})
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, "Locate file src/Makefile")
    )
    assert result["source_path_resolution"] == {"status": "available", "path": "src/Makefile"}
    assert result["uncovered_questions"] == ["requested_source_symbol_unresolved"]


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.asyncio
async def test_funded_result_does_not_confuse_workflow_delivery_with_navigation_success(
    env, missing
):
    from test_source_navigation_v163 import submit_navigation

    app = env["app"]
    source = import_navigation_source(env)
    path = "src/missing.py" if missing else "src/agent.py"
    turn = submit_navigation(env, source, "Locate Target.run in " + path, funded=True)
    epoch = app.runtime.claim(turn["run_id"], "synthetic-path-worker")
    await app.conversations.process(
        turn["turn_id"], (turn["run_id"], epoch, "synthetic-path-worker")
    )
    result = app.runtime.stage(turn["run_id"], "result")
    assert result["source_path_contract"] == "explicit-source-path-1.6.5"
    if missing:
        assert result["task_satisfied"] is False
        assert result["navigation_outcome"] == "not_completed"
        assert result["uncovered_questions"] == ["requested_source_path_missing_from_snapshot"]
    else:
        assert result["task_satisfied"] is None
        assert result["navigation_outcome"] == "source_observations_returned"
        assert result["uncovered_questions"] == []
    assert not env["model"].calls


@pytest.mark.parametrize(
    "question",
    [
        "查看src/a.py，src/b.py",
        "Locate src/a.py; src/b.py",
        "Locate src/a.py\nsrc/b.py",
        "Locate src/a(1).py",
        "Locate src/foo bar.py",
        "Locate src%2Fmissing.py",
        "Show me src/a.py...",
        "Locate Target.run in src/a.py(backup)",
    ],
)
@pytest.mark.asyncio
async def test_independent_audit_unsupported_targets_never_drop_scope_or_repair_path(env, question):
    source = import_navigation_source(
        env,
        {
            "src/a.py": "class Target:\n    def run(self):\n        return 1\n",
            "src/b.py": "class Target:\n    def run(self):\n        return 2\n",
        },
    )
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, question))
    assert result.get("source_path_resolution", {}).get("status") == "clarification_required"
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["uncovered_questions"]


@pytest.mark.parametrize("entry", ["navigation", "commit"])
@pytest.mark.parametrize("scope_origin", ["focus", "literal", "unselected"])
@pytest.mark.asyncio
async def test_same_path_in_another_authorized_snapshot_cannot_replace_frozen_user_scope(
    env, entry, scope_origin
):
    from test_source_navigation_v163 import submit_navigation

    from careerforge.errors import DomainError
    from careerforge.models import Turn
    from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT

    app, actor = env["app"], env["actor"]
    selected = import_navigation_source(env, {"src/agent.py": "def run(): return 1\n"})
    other = import_navigation_source(
        env, {"src/agent.py": "def run(): return 999\n"}, key="fixtures/other"
    )
    question = "Locate run in src/agent.py"
    if scope_origin == "focus":
        turn = submit_navigation(env, selected, question)
    else:
        from careerforge.contracts import TurnInput

        if scope_origin == "literal":
            question = "For fixtures/navigation, " + question
        turn = app.conversations.submit(
            actor,
            app.conversations.create(actor),
            TurnInput(question=question, expected_revision=1, idempotency_key="bound-source"),
        )
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).focus["source_path_source_ids"] == (
            [] if scope_origin == "unselected" else [selected]
        )
    if entry == "navigation":
        spec = spec_for(other, question).model_copy(update={"turn_id": turn["turn_id"]})
        forged = await app.retrieval.retrieve(actor, spec)
        with pytest.raises(DomainError) as failure:
            app.conversations._navigate_source(
                actor, turn["turn_id"], forged, None, SOURCE_NAVIGATION_CONTRACT
            )
        assert failure.value.code == "SOURCE_PATH_CONFLICT"
    else:
        read = app.sources.read_span(actor, other, "src/agent.py", 1, 1, "tutor", turn["turn_id"])
        content = {
            "kind": "raw_source_observation",
            "text": read["content"],
            "source_id": other,
            "publication_ids": [],
            "fact_atom_ids": [],
            "citations": [read],
            "checked": "source_position",
            "navigation_contract": SOURCE_NAVIGATION_CONTRACT,
            "navigation": {
                "source_id": other,
                "path": "src/agent.py",
                "symbol": "run",
                "start_line": 1,
                "end_line": 1,
            },
        }
        with pytest.raises(DomainError) as failure:
            app.answers.commit_block(actor, turn["turn_id"], content, None, 0)
        assert failure.value.code == "ANSWER_SOURCE_PATH"
    assert answer_blocks(env, turn) == []
