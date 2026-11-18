"""Candidate002 synthetic regressions; no native calls or imported-code execution."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from test_source_navigation_v163 import import_navigation_source, submit_navigation

from careerforge.contracts import ModelOutput
from careerforge.models import SourceRead, Turn

CASES = [
    (
        "inspect",
        "Inspect src/missing.py in the selected fixed snapshot. Do not substitute another file.",
    ),
    (
        "navigate",
        "Navigate to src/missing.py in the selected fixed snapshot. Do not substitute another file.",
    ),
    ("chinese_find", "请找出 src/missing.py 的实现，仅限当前已选固定快照，不要用其他文件替代。"),
    ("supported_control", "请查看 src/missing.py，仅限当前已选固定快照，不要用其他文件替代。"),
]


def fixture_source(env):
    return import_navigation_source(
        env,
        {
            "src/nearby.py": "class Missing:\n    def run(self):\n        return 'SYNTHETIC_NEARBY_ONLY'\n"
        },
    )


def coordinator(env, source):
    env["model"].overrides["coordinator"] = lambda request: ModelOutput(
        structured={
            "interpreted_intent": "locate_symbol",
            "confidence": 0.99,
            "reference_resolution": "resolved",
            "proposed_next_action": "direct_tool",
            "project_source_ids": [source],
            "code_focus_ref": "src/missing.py",
            "rationale_codes": ["synthetic_requested_navigation"],
        },
        synthetic=True,
    )


def blocks(env, turn):
    return [
        event["payload"]["content"]
        for event in env["app"].answers.events(env["actor"], turn["turn_id"])["events"]
        if event["type"] == "answer.block.validated"
    ]


@pytest.mark.parametrize("case,question", CASES, ids=[name for name, _ in CASES])
@pytest.mark.asyncio
async def test_original_four_normal_routes_never_read_substitute_files(env, case, question):
    app = env["app"]
    source = fixture_source(env)
    coordinator(env, source)
    turn = submit_navigation(env, source, question, funded=True)
    await app.drain()
    with app.db.read() as db:
        reads = list(
            db.scalars(select(SourceRead).where(SourceRead.session_key == turn["turn_id"]))
        )
    assert reads == []
    answer = blocks(env, turn)
    assert answer and all(item["kind"] == "evidence_gap" for item in answer)
    assert all(item["task_satisfied"] is False for item in answer)
    result = app.runtime.stage(turn["run_id"], "result")
    assert result["task_satisfied"] is False
    assert result["navigation_outcome"] == "not_completed"
    assert result["uncovered_questions"]
    assert len(env["model"].calls) == 1
    assert env["model"].calls[0]["role"] == "coordinator"
    if case == "supported_control":
        assert result["uncovered_questions"] == ["requested_source_path_missing_from_snapshot"]
    else:
        assert result["uncovered_questions"] == ["requested_source_path_attachment_unconfirmed"]


@pytest.mark.parametrize(
    "old_path_contract", [False, True], ids=["pre_path_contract", "candidate001"]
)
@pytest.mark.asyncio
async def test_unmarked_durable_turn_and_completed_receipt_keep_original_semantics(
    env, old_path_contract
):
    app = env["app"]
    source = fixture_source(env)
    coordinator(env, source)
    turn = submit_navigation(env, source, CASES[0][1], funded=True)
    with app.db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        row.focus = {
            key: value
            for key, value in row.focus.items()
            if key != "source_path_guard" and (old_path_contract or key != "source_path_contract")
        }
    await app.drain()
    old = deepcopy(blocks(env, turn))
    assert len(old) == 2 and all(b["kind"] == "raw_source_observation" for b in old)
    assert {b["navigation"]["path"] for b in old} == {"src/nearby.py"}
    result = app.runtime.stage(turn["run_id"], "result")
    assert "task_satisfied" not in result and "source_path_guard" not in result
    await app.conversations.process(turn["turn_id"])
    assert blocks(env, turn) == old
    assert app.runtime.stage(turn["run_id"], "result") == result
    assert len(env["model"].calls) == 1


@pytest.mark.parametrize(
    "question",
    [
        "Browse src/missing.py",  # Not one of the three reported verbs.
        "Navigate to `src/missing.py`",
        "Inspect src/missing(1).py",
        "Inspect src/missing file.py",
    ],
)
@pytest.mark.asyncio
async def test_locate_retrieval_checks_candidates_without_positive_verb_grammar(env, question):
    from test_explicit_source_paths_v165 import spec_for

    from careerforge.services.explicit_source_paths import LOCATE_PATH_GUARD, requested_source_path

    assert requested_source_path(question) is None  # Old grammar stays unchanged.
    source = fixture_source(env)
    result = await env["app"].retrieval.retrieve(env["actor"], spec_for(source, question))
    assert result["source_path_guard"] == LOCATE_PATH_GUARD
    assert result["symbols"] == result["knowledge_units"] == []
    assert result["uncovered_questions"] == ["requested_source_path_attachment_unconfirmed"]


@pytest.mark.parametrize(
    "question,locate",
    [
        ("Explain src/missing.py", False),
        ('Explain "Inspect src/missing.py"', True),
        ('Explain "src/missing.py should be compared with src/nearby.py"', True),
        ("Explain ```Navigate to src/missing.py```", True),
        ("> 请找出 src/missing.py\nExplain this example.", True),
        ("The docs mention src/missing.py. Locate symbol Missing.run.", True),
    ],
)
@pytest.mark.asyncio
async def test_non_navigation_and_quoted_or_contextual_data_do_not_grant_path_scope(
    env, question, locate
):
    from test_explicit_source_paths_v165 import spec_for

    from careerforge.contracts import TaskType
    from careerforge.services.explicit_source_paths import (
        LOCATE_PATH_GUARD,
        SOURCE_PATH_CONTRACT,
        navigation_path_request,
    )

    assert (
        navigation_path_request(
            question, contract=SOURCE_PATH_CONTRACT, guard=LOCATE_PATH_GUARD, locate=locate
        )
        is None
    )
    source = fixture_source(env)
    task = TaskType.LOCATE if locate else TaskType.IMPLEMENTATION
    result = await env["app"].retrieval.retrieve(
        env["actor"], spec_for(source, question, task=task)
    )
    assert "source_path_resolution" not in result


@pytest.mark.asyncio
async def test_legacy_selection_cannot_bypass_new_guard_at_navigation_boundary(env):
    from test_explicit_source_paths_v165 import spec_for

    from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT

    app, actor = env["app"], env["actor"]
    source = fixture_source(env)
    question = CASES[0][1]
    turn = submit_navigation(env, source, question)
    spec = spec_for(source, question).model_copy(update={"turn_id": turn["turn_id"]})
    evidence = await app.retrieval.retrieve(actor, spec, source_path_guard=None)
    assert evidence["symbols"]  # Real legacy candidates; no mocked retrieval.
    result = app.conversations._navigate_source(
        actor, turn["turn_id"], evidence, None, SOURCE_NAVIGATION_CONTRACT
    )
    assert len(result) == 1 and result[0]["task_satisfied"] is False
    with app.db.read() as db:
        assert not list(
            db.scalars(select(SourceRead).where(SourceRead.session_key == turn["turn_id"]))
        )


@pytest.mark.parametrize("receipt_contract", [False, True])
@pytest.mark.asyncio
async def test_real_read_cannot_commit_under_new_unconfirmed_target_guard(env, receipt_contract):
    from careerforge.errors import DomainError
    from careerforge.services.retrieval import SOURCE_NAVIGATION_CONTRACT

    app, actor = env["app"], env["actor"]
    source = fixture_source(env)
    turn = submit_navigation(env, source, CASES[0][1])
    read = app.sources.read_span(actor, source, "src/nearby.py", 1, 3, "tutor", turn["turn_id"])
    block = {
        "kind": "raw_source_observation",
        "text": read["content"],
        "source_id": source,
        "publication_ids": [],
        "fact_atom_ids": [],
        "citations": [read],
        "checked": "source_position",
        "navigation": {
            "source_id": source,
            "path": "src/nearby.py",
            "start_line": 1,
            "end_line": 3,
        },
        **({"navigation_contract": SOURCE_NAVIGATION_CONTRACT} if receipt_contract else {}),
    }
    with pytest.raises(DomainError) as rejected:
        app.answers.commit_block(actor, turn["turn_id"], block, None, 0)
    assert rejected.value.code == "ANSWER_SOURCE_PATH"
    assert blocks(env, turn) == []
    # Exercise the same immutable validator used by every replay surface.
    with app.db.read() as db, pytest.raises(DomainError) as replay_rejected:
        app.answers._validate_navigation_read(db, actor, db.get(Turn, turn["turn_id"]), block)
    assert replay_rejected.value.code == "ANSWER_SOURCE_PATH"


@pytest.mark.asyncio
async def test_guard_version_is_internal_and_cache_is_separate_from_candidate001(env):
    from pydantic import ValidationError
    from test_explicit_source_paths_v165 import spec_for

    from careerforge.contracts import TurnInput
    from careerforge.models import Conversation
    from careerforge.services.explicit_source_paths import LOCATE_PATH_GUARD

    app, actor = env["app"], env["actor"]
    source = fixture_source(env)
    question = CASES[0][1]
    with pytest.raises(ValidationError):
        TurnInput.model_validate(
            {
                "question": question,
                "expected_revision": 1,
                "idempotency_key": "client-cannot-set-guard",
                "source_path_guard": None,
            }
        )
    cid = app.conversations.create(actor)
    with app.db.transaction() as db:
        db.get(Conversation, cid).focus = {
            "source_path_guard": "forged",
            "project_source_ids": [source],
        }
    turn = app.conversations.submit(
        actor,
        cid,
        TurnInput(question=question, expected_revision=1, idempotency_key="server-freezes-guard"),
    )
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).focus["source_path_guard"] == LOCATE_PATH_GUARD
    spec = spec_for(source, question)
    legacy = await app.retrieval.retrieve(actor, spec, source_path_guard=None)
    old_keys = set(app.cache.values)
    current = await app.retrieval.retrieve(actor, spec)
    assert legacy["symbols"] and current["symbols"] == []
    assert "source_path_guard" not in legacy
    assert set(app.cache.values) > old_keys


@pytest.mark.parametrize(
    "question,reason",
    [
        ("Inspect missing.py", "requested_source_target_ambiguous"),
        ("Locate missing.py. Do not substitute another file.", "requested_source_target_ambiguous"),
        ("Inspect src/missing", "requested_source_path_attachment_unconfirmed"),
    ],
)
@pytest.mark.asyncio
async def test_unconfirmed_bare_or_extensionless_target_cannot_fall_back(env, question, reason):
    app = env["app"]
    source = fixture_source(env)
    coordinator(env, source)
    turn = submit_navigation(env, source, question, funded=True)
    await app.drain()
    with app.db.read() as db:
        assert not list(
            db.scalars(select(SourceRead).where(SourceRead.session_key == turn["turn_id"]))
        )
    result = app.runtime.stage(turn["run_id"], "result")
    assert result["task_satisfied"] is False
    assert result["uncovered_questions"] == [reason]
    assert blocks(env, turn)[0]["source_path_resolution"] == {
        "status": "clarification_required",
        "reason": reason,
    }
    assert len(env["model"].calls) == 1


@pytest.mark.parametrize(
    "question,reason",
    [
        ("Locate AgentRun.next", "requested_source_target_ambiguous"),
        ("Inspect symbol AgentRun.next", None),
        ("Inspect AgentRun.next", "requested_source_target_ambiguous"),
        ("Inspect read.py", "requested_source_target_ambiguous"),
    ],
)
def test_unknown_dotted_target_is_not_guessed_to_be_a_file_or_symbol(question, reason):
    from careerforge.services.explicit_source_paths import (
        LOCATE_PATH_GUARD,
        SOURCE_PATH_CONTRACT,
        navigation_path_request,
    )

    result = navigation_path_request(
        question, contract=SOURCE_PATH_CONTRACT, guard=LOCATE_PATH_GUARD, locate=True
    )
    assert result == ({"status": "ambiguous", "reason": reason} if reason else None)
    assert not result or "path" not in result


@pytest.mark.parametrize("legacy", [False, True], ids=["new_guard", "candidate001_no_guard"])
@pytest.mark.parametrize(
    "question", ["Locate Target.run", "定位 fixtures/navigation 的 Target.run"]
)
@pytest.mark.asyncio
async def test_original_ambiguous_inputs_clarify_new_but_replay_legacy_unchanged(
    env, legacy, question
):
    app = env["app"]
    source = import_navigation_source(env)
    turn = submit_navigation(env, source, question)
    if legacy:
        with app.db.transaction() as db:
            row = db.get(Turn, turn["turn_id"])
            row.focus = {
                key: value for key, value in row.focus.items() if key != "source_path_guard"
            }
    await app.conversations.process(turn["turn_id"])
    saved = deepcopy(blocks(env, turn))
    assert saved
    with app.db.read() as db:
        reads = list(
            db.scalars(select(SourceRead).where(SourceRead.session_key == turn["turn_id"]))
        )
    if legacy:
        assert reads and all(block["kind"] == "raw_source_observation" for block in saved)
        assert any(block["navigation"]["symbol"] == "Target.run" for block in saved)
        assert all("source_path_guard" not in block for block in saved)
    else:
        assert reads == []
        assert all(
            block["kind"] == "evidence_gap" and block["task_satisfied"] is False for block in saved
        )
        assert all(
            block["source_path_resolution"]["status"] == "clarification_required" for block in saved
        )
    await app.conversations.process(turn["turn_id"])
    assert blocks(env, turn) == saved
    assert not env["model"].calls


@pytest.mark.parametrize(
    "question", ["Locate symbol Target.run", "定位 仓库 fixtures/navigation 的符号 Target.run"]
)
@pytest.mark.asyncio
async def test_explicit_symbol_and_repository_context_remain_real_navigation(env, question):
    app = env["app"]
    source = import_navigation_source(env)
    turn = submit_navigation(env, source, question)
    await app.conversations.process(turn["turn_id"])
    observed = blocks(env, turn)
    assert observed and all(block["kind"] == "raw_source_observation" for block in observed)
    assert any(block["navigation"]["symbol"] == "Target.run" for block in observed)
    assert not env["model"].calls
