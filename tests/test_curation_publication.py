from copy import deepcopy
from types import SimpleNamespace

import pytest

from careerforge.contracts import UnitDraft
from careerforge.errors import DomainError
from careerforge.services.curation_publication import (
    CONTRACT,
    BindingOutput,
    apply_claim_repairs,
    bind_fixed_references,
    bind_frozen_drafts,
    check_pair_topics,
    definition_targets,
    fixed_binding_model,
    frozen_drafts,
    python_symbol_at,
    read_windows,
    unchanged_drafts,
    validate_family,
)


def unit(key):
    return UnitDraft.model_validate(
        {
            "subject_key": key,
            "unit_type": "architecture",
            "title": key,
            "claims": [
                {
                    "key": "fact",
                    "statement": "明确的局部源码命题",
                    "support": [{"read_id": "fresh", "quote": "unique code quote"}],
                }
            ],
            "topics": [{"topic_key": "agent.orchestration", "relation": "illustrates"}],
        }
    )


def test_reordered_model_units_preserve_original_to_new_version_mapping():
    first, second = unit("first"), unit("second")
    expected = [first.model_dump(mode="json"), second.model_dump(mode="json")]
    ordered = unchanged_drafts(expected, [second, first])
    assert [u.subject_key for u in ordered] == ["first", "second"]


@pytest.mark.parametrize("field", ["statement", "conditions", "kind"])
def test_materialization_cannot_rewrite_approved_claims(field):
    original = unit("approved")
    changed = deepcopy(original)
    setattr(
        changed.claims[0],
        field,
        {
            "statement": "不同的命题",
            "conditions": ["新增条件"],
            "kind": "analytical_inference",
        }[field],
    )
    with pytest.raises(DomainError) as error:
        unchanged_drafts([original.model_dump(mode="json")], [changed])
    assert error.value.code == "CURATION_MATERIALIZATION_DRIFT"


def test_child_or_reviewer_cannot_act_as_publication_root():
    for workflow, parent_id in [("review", None), ("knowledge_build", "other-root")]:
        run = SimpleNamespace(
            workflow=workflow,
            parent_id=parent_id,
            namespace_id="ns",
            input_manifest={"phase": "root", "namespace_id": "ns"},
        )
        with pytest.raises(DomainError) as error:
            validate_family(None, run, "root")
        assert error.value.code == "CURATION_PUBLICATION_FAMILY"


def test_duplicate_candidate_selection_is_rejected_before_any_model_call():
    with pytest.raises(DomainError) as error:
        frozen_drafts(
            None,
            None,
            "actor",
            {
                "curation_publication_contract": CONTRACT,
                "original_unit_ids": ["same", "same"],
            },
        )
    assert error.value.code == "CURATION_PUBLICATION_SCOPE"


def test_curation_quote_correction_is_explicitly_bounded():
    from careerforge.services.build_execution import QUOTE_CORRECTION_CONTRACT
    from careerforge.services.quote_correction import quote_correction_enabled

    value = {
        "curation_publication_contract": CONTRACT,
        "quote_correction_contract": deepcopy(QUOTE_CORRECTION_CONTRACT),
    }
    assert quote_correction_enabled(value)
    value["quote_correction_contract"]["max_attempts"] = 2
    with pytest.raises(DomainError):
        quote_correction_enabled(value)


def test_standalone_object_context_comes_from_real_nested_python_scope():
    source = "class Scope:\n    def method(self):\n        def inner():\n            return 1\n        return inner()\n"
    assert python_symbol_at(source, 4) == "Scope.method.inner"
    assert python_symbol_at(source, 5) == "Scope.method"
    assert python_symbol_at(source, 1) == "Scope"
    assert python_symbol_at(source, 99) is None
    assert python_symbol_at("invalid python (", 1) is None


def test_compact_binding_preserves_frozen_semantics_and_rejects_missing_claims():
    draft = unit("fixed").model_dump(mode="json")
    draft["claims"][0]["support"] = []
    binding = BindingOutput.model_validate(
        {
            "units": [
                {
                    "subject_key": "fixed",
                    "claims": [
                        {
                            "claim_key": "fact",
                            "support": [
                                {"read_id": "new-analysis", "quote": "fresh unique source"}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    output = bind_frozen_drafts([draft], binding)
    actual = output.units[0].model_dump(mode="json")
    assert actual["claims"][0]["support"][0]["read_id"] == "new-analysis"
    actual["claims"][0]["support"] = []
    assert actual == draft
    binding.units[0].claims[0].claim_key = "unapproved"
    with pytest.raises(DomainError):
        bind_frozen_drafts([draft], binding)


def test_compact_binding_rejects_duplicate_unit_identity():
    row = {
        "subject_key": "fixed",
        "claims": [{"claim_key": "fact", "support": [{"read_id": "read", "quote": "code quote"}]}],
    }
    binding = BindingOutput.model_validate({"units": [row, row]})
    with pytest.raises(DomainError):
        bind_frozen_drafts([unit("fixed").model_dump(mode="json")], binding)


def test_fixed_binding_schema_requires_every_slot_and_preserves_mapping():
    from pydantic import ValidationError

    drafts = [unit("one").model_dump(mode="json"), unit("two").model_dump(mode="json")]
    model = fixed_binding_model(drafts)
    refs = model.model_validate(
        {
            "u1_c0": [{"read_id": "second", "quote": "second code"}],
            "u0_c0": [{"read_id": "first", "quote": "first code"}],
        }
    )
    bound = bind_fixed_references(drafts, refs)
    assert [u.claims[0].support[0].read_id for u in bound.units] == ["first", "second"]
    with pytest.raises(ValidationError):
        model.model_validate({"u0_c0": [{"read_id": "first", "quote": "first code"}]})
    with pytest.raises(ValidationError):
        model.model_validate({**refs.model_dump(), "unapproved": []})


def test_review_repair_preserves_conditions_and_frozen_unit_metadata():
    draft = unit("fixed").model_dump(mode="json")
    draft["claims"][0]["conditions"] = ["fixed source only"]
    row = {
        "statement": "函数固定对象的局部静态事实",
        "conditions": ["fixed source only"],
        "kind": "documented_intent",
        "support": [{"read_id": "fresh", "quote": "source quote"}],
    }
    repaired = apply_claim_repairs([draft], {"u0_c0": row})
    assert repaired.units[0].title == draft["title"]
    assert repaired.units[0].claims[0].kind == "documented_intent"
    row["conditions"] = []
    with pytest.raises(DomainError):
        apply_claim_repairs([draft], {"u0_c0": row})


def test_review_reads_include_both_class_and_method_declarations():
    text = "class Owner:\n    def method(self):\n        value = 1\n        return value\n"
    targets = definition_targets({"a.py": text}, [{"path": "a.py", "start_line": 4, "end_line": 4}])
    assert targets == [{"path": "a.py", "start_line": 1, "end_line": 4}]


def test_unrelated_or_duplicate_pairs_fail_before_publication():
    views = [
        {
            "original_unit_id": "arch",
            "draft": {"topics": [{"topic_key": "agent.memory", "relation": "illustrates"}]},
        },
        {
            "original_unit_id": "impl",
            "draft": {"topics": [{"topic_key": "code.python", "relation": "illustrates"}]},
        },
    ]
    pair = {"architecture_unit_version_id": "arch", "implementation_unit_version_id": "impl"}
    with pytest.raises(DomainError) as error:
        check_pair_topics(views, [pair])
    assert error.value.code == "CURATION_PAIR_TOPIC"
    views[1]["draft"]["topics"].append({"topic_key": "agent.memory", "relation": "illustrates"})
    check_pair_topics(views, [pair])
    with pytest.raises(DomainError):
        check_pair_topics(views, [pair, pair])


def test_reviewed_claim_body_cannot_be_rewritten_during_reference_rebinding():
    from pydantic import ValidationError

    from careerforge.services.curation_publication import repair_model

    drafts = [unit("fixed").model_dump(mode="json")]
    refs = {"u0_c0": [{"read_id": "new", "quote": "new unique code"}]}
    result = apply_claim_repairs(drafts, refs, editable=set())
    assert result.units[0].claims[0].statement == drafts[0]["claims"][0]["statement"]
    with pytest.raises(ValidationError):
        repair_model(drafts, set()).model_validate({"u0_c0": {"statement": "rewrite"}})


def test_frozen_read_range_follows_all_pages_without_reading_outside_scope():
    calls = []

    def read(actor, sid, path, start, end, role, session, run_id, **kwargs):
        calls.append((path, start, end, role))
        actual_end = min(start + 1, end)
        return {
            "read_id": str(start),
            "continuation": {"path": path, "start_line": actual_end + 1, "end_line": end}
            if actual_end < end
            else None,
        }

    app = SimpleNamespace(sources=SimpleNamespace(read_span=read))
    run = SimpleNamespace(
        id="run",
        principal_id="owner",
        input_manifest={"read_completion_contract": "bounded-frozen-pages-v1"},
    )
    result = read_windows(
        app,
        run,
        "source",
        "independent-session",
        "reviewer",
        [{"path": "a.py", "start_line": 1, "end_line": 5}],
    )
    assert [r["read_id"] for r in result["reads"]] == ["1", "3", "5"]
    assert calls == [
        ("a.py", 1, 5, "reviewer"),
        ("a.py", 3, 5, "reviewer"),
        ("a.py", 5, 5, "reviewer"),
    ]
