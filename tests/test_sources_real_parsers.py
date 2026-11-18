"""Real installed Tree-sitter grammars, not parser mocks or execution claims."""

import pytest

from careerforge.contracts import ImportSourceInput
from careerforge.services.sources import SourceService

SAMPLES = [
    (
        ".ts",
        "tree_sitter_typescript",
        "// 示例\ninterface Input { value: string }\n"
        "export const dispatch = (value: string): string => value;\n"
        "export function echo(value: string): string { return value; }\n",
        {"Input": (2, 2), "dispatch": (3, 3), "echo": (4, 4)},
    ),
    (
        ".tsx",
        "tree_sitter_typescript",
        "// 示例\nexport function App() {\n  return <div>你好</div>;\n}\n",
        {"App": (2, 4)},
    ),
    (
        ".js",
        "tree_sitter_javascript",
        "// 示例\nconst dispatch = (value) => {\n  return value;\n};\n"
        "function echo(value) { return value; }\n",
        {"dispatch": (2, 4), "echo": (5, 5)},
    ),
    (
        ".jsx",
        "tree_sitter_javascript",
        "export const App = () => <div>你好</div>;\n",
        {"App": (1, 1)},
    ),
    (
        ".go",
        "tree_sitter_go",
        "package main\ntype Input struct { Value string }\n"
        "func (i Input) Echo() string { return i.Value }\n",
        {"Input": (2, 2), "Echo": (3, 3)},
    ),
]


@pytest.mark.parametrize("suffix,grammar,body,expected", SAMPLES)
def test_real_grammar_symbol_spans_and_read_only_import(env, suffix, grammar, body, expected):
    pytest.importorskip("tree_sitter")
    pytest.importorskip(grammar)
    symbols, parser, status = SourceService._tree_sitter(suffix, body)
    assert status == "parsed" and parser.startswith("tree-sitter:" + grammar + ":")
    assert {s["name"]: (s["start"], s["end"]) for s in symbols} == expected
    source_id = env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="parser/fixture",
            commit_sha="a" * 40,
            license_id="MIT",
            files={"src/sample" + suffix: body},
            source_metadata={"synthetic": True},
        ),
    )
    description = env["app"].sources.describe(env["actor"], source_id)
    assert description["executed"] is False
    assert description["files"][0]["parse_status"] == "parsed"
    for name, span in expected.items():
        found = env["app"].sources.search(env["actor"], source_id, name, symbols_only=True)
        assert any((row["start_line"], row["end_line"]) == span for row in found)
        assert all(row["code_read"] is False for row in found)


@pytest.mark.parametrize("suffix,grammar", [(row[0], row[1]) for row in SAMPLES])
def test_real_grammar_marks_invalid_syntax_partial(suffix, grammar):
    pytest.importorskip("tree_sitter")
    pytest.importorskip(grammar)
    _, parser, status = SourceService._tree_sitter(suffix, "function broken( {")
    assert status == "partial" and parser.startswith("tree-sitter:")


def test_missing_grammar_is_explicit_text_only_fallback(monkeypatch):
    pytest.importorskip("tree_sitter")

    def unavailable(name):
        raise ImportError("deliberately unavailable fixture grammar")

    monkeypatch.setattr("careerforge.services.sources.importlib.import_module", unavailable)
    assert SourceService._tree_sitter(".ts", "function echo() {}") == (
        [],
        "tree-sitter-unavailable",
        "text_only",
    )
