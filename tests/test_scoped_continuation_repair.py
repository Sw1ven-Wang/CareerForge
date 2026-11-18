import pytest

from careerforge.services.answer_relevance import scoped_continuation_question


def unit(*topics, relation="implements"):
    return {"topic_links": [{"topic_key": topic, "relation": relation} for topic in topics]}


@pytest.mark.parametrize(
    "question",
    [
        "继续讲解",
        "请继续",
        "Continue",
        "please continue explaining.",
        "go on",
        "tell me more",
        "more detail",
    ],
)
def test_continuation_uses_only_current_exact_reviewed_topics(question):
    focus = {
        "topic_keys": ["agent.tool_calling", "private.old_topic"],
        "topic_hints": ["agent.tool_calling", "NOT_A_REVIEWED_KEY"],
        "history": [{"question": "PRIVATE_OLD_QUESTION", "answer_excerpt": "PRIVATE_OLD_ANSWER"}],
    }
    result = scoped_continuation_question(question, focus, [unit("agent.tool_calling")])
    assert result.startswith(question)
    assert result.endswith("agent.tool_calling")
    assert "PRIVATE" not in result and "private.old_topic" not in result
    assert "NOT_A_REVIEWED_KEY" not in result
    assert result.count("agent.tool_calling") == 1


@pytest.mark.parametrize(
    "question",
    [
        "continue with deleting the old implementation",
        "continue and explain retry",
        '"continue"',
        "do not continue",
        "if I continue",
        "continue after I approve",
        "继续删除文件",
        "不要继续",
        "如果继续",
        "继续，如果我同意",
        "What does continue mean?",
        "Explain the current cache",
    ],
)
def test_non_pure_continuations_keep_exact_current_question(question):
    assert (
        scoped_continuation_question(
            question, {"topic_keys": ["agent.tool_calling"]}, [unit("agent.tool_calling")]
        )
        == question
    )


@pytest.mark.parametrize(
    "focus",
    [
        {},
        {"topic_keys": "agent.tool_calling"},
        {"topic_keys": {"agent.tool_calling": True}},
        {"topic_keys": [None, 7, {"topic_key": "agent.tool_calling"}]},
        {"topic_keys": ["Tool Calling"]},
        {"topic_hints": ["private.revoked"]},
    ],
)
def test_unknown_alias_or_malformed_focus_never_becomes_topic_authority(focus):
    assert (
        scoped_continuation_question("continue", focus, [unit("agent.tool_calling")]) == "continue"
    )


def test_mentions_only_or_omitted_pack_topics_are_not_resolved():
    focus = {"topic_keys": ["private.topic_a", "agent.tool_calling"]}
    a, b = unit("private.topic_a"), unit("agent.tool_calling")
    assert "private.topic_a" in scoped_continuation_question("continue", focus, [a, b])
    assert "private.topic_a" not in scoped_continuation_question("continue", focus, [b])
    assert "agent.tool_calling" in scoped_continuation_question("continue", focus, [b])
    assert (
        scoped_continuation_question(
            "continue", focus, [unit("private.topic_a", relation="mentions")]
        )
        == "continue"
    )
