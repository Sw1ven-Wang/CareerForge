"""Realistic large text imports stay bounded and exact; no model or network calls."""

from __future__ import annotations

import io
import zipfile

import pytest

from careerforge.adapters.github import GitHubProvider
from careerforge.contracts import ImportSourceInput
from careerforge.errors import DomainError
from careerforge.util import validate_files


def large_text(size=180_000):
    line = "# bounded source data\n"
    return line * (size // len(line))


def test_direct_import_supports_large_source_and_bounded_reads(env):
    body = large_text()
    source = env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="example/project",
            commit_sha="b" * 40,
            license_id="MIT",
            files={"src/large.py": body},
        ),
    )
    actual = env["app"].sources.read_span(
        env["actor"], source, "src/large.py", 1, 20, "analysis", "large-import"
    )
    assert actual["content"] == "".join(body.splitlines(keepends=True)[:20])
    assert actual["end_line"] == 20
    with pytest.raises(DomainError):
        env["app"].sources.read_span(
            env["actor"], source, "src/large.py", 1, 200, "analysis", "too-large-page"
        )


def test_archive_import_uses_same_large_file_bound():
    data = io.BytesIO()
    body = large_text()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("project-commit/src/large.py", body)
        archive.writestr("project-commit/README.md", "Small fixture.\n")
    files, skipped = GitHubProvider.unpack(data.getvalue())
    assert files["src/large.py"] == body
    assert not skipped


@pytest.mark.parametrize("body", [large_text(1_000_100), "\x00", "x" * 25001])
def test_large_file_bounds_do_not_disable_other_guards(body):
    with pytest.raises(DomainError):
        validate_files({"src/sample.py": body})


def test_aggregate_budget_remains_enforced():
    with pytest.raises(DomainError):
        validate_files({"a.py": large_text(90_000), "b.py": large_text(90_000)}, max_bytes=150_000)


def test_utf8_byte_limit_not_character_limit():
    with pytest.raises(DomainError):
        validate_files({"large.txt": ("汉字\n" * 150_000)})
