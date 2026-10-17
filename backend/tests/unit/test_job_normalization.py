import json

import pytest
from careerforge.domain.job_normalization import adapt, canonical, sections
from careerforge.domain.job_requirements import parse_concepts
from careerforge.workflows.contract_alignment import choose


@pytest.mark.parametrize(
    "provider,payload",
    [
        (
            "greenhouse",
            {
                "id": 123,
                "internal_job_id": 999,
                "title": "Agent Intern",
                "content": "<h2>Requirements</h2><p>Python or Go</p><script>alert(1)</script>",
                "updated_at": "2026-10-12T00:00:00Z",
            },
        ),
        (
            "lever",
            {
                "id": "lever-1",
                "text": "Agent Intern",
                "descriptionPlain": "Requirements",
                "lists": [{"text": "Qualifications", "content": "<li>Python or Go</li>"}],
            },
        ),
        (
            "ashby",
            {
                "title": "Agent Intern",
                "descriptionPlain": "Requirements: Python or Go",
                "publishedAt": "2026-10-12T00:00:00Z",
            },
        ),
        (
            "json_ld",
            {
                "@type": "JobPosting",
                "title": "Agent Intern",
                "description": "<p>Requirements: Python or Go</p>",
                "datePosted": "2026-10-12T00:00:00Z",
            },
        ),
    ],
)
def test_saved_provider_payloads_preserve_semantics(provider, payload):
    job, body, external, _, meta = adapt(provider, json.dumps(payload), None, None)
    assert "Python or Go" in body and "alert(1)" not in body
    assert job["employment_type"] == "internship"
    if provider == "greenhouse":
        assert external == "123" and meta["internal_job_id"] == 999
        assert job["published_at"] is None
        assert job["provider_updated_at"] is not None
    if provider == "ashby":
        assert external is None and job["published_at_semantics"] == "last_published"
    if provider == "json_ld":
        assert job["published_at_semantics"] == "date_posted"


@pytest.mark.parametrize(
    "text,root",
    [
        ("Python 或 Go，并具备 RAG 项目经验", "all_of"),
        ("(Python 和 Go) 或 Java", "any_of"),
        ("Python 或 (Go 且 RAG)", "any_of"),
    ],
)
def test_nested_logic_preserves_choices(text, root):
    tree = parse_concepts(text)
    assert tree["type"] == root
    decisions = []
    selected = choose(tree, {"go"}, decisions)
    if "RAG" in text and text.startswith("Python 或 Go"):
        assert set(selected) == {"technology.go", "capability.rag"}
    assert decisions


def test_unicode_and_section_priority():
    body = canonical("任职要求：\r\n熟悉 Python 😀\nPyTorch 优先\n具备 RAG 经验")
    parts = sections(body)
    assert all(body[p["start"] : p["end"]] == p["text"] for p in parts)
    assert parts[-1]["kind"] == "qualification"
