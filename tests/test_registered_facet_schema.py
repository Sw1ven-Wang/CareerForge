import pytest
from jsonschema import Draft202012Validator
from careerforge.services.build import BuildService

TOPICS = [{"key": "agent.loop", "facets": {"lifecycle": {"value_type": "string"}}}, {"key": "agent.architecture", "facets": {"architecture_scope": {"value_type": "string"}}}]


def validator():
    schema = BuildService._analysis_schema(TOPICS, registered_pairs=True)
    return Draft202012Validator({"$ref": "#/$defs/FacetDraft", "$defs": schema["$defs"]})


@pytest.mark.parametrize("topic,facet,valid", [("agent.loop", "lifecycle", True), ("agent.architecture", "architecture_scope", True), ("agent.loop", "architecture_scope", False), ("runtime.error_handling", "failure_boundary", False), ("agent.conditional_routing", "failure_boundary", False)])
def test_registered_pairs(topic, facet, valid):
    value = {"topic_key": topic, "facet_key": facet, "value_status": "known", "value": "x", "claim_keys": ["c1"], "applicability_reason": "test"}
    assert validator().is_valid(value) == valid


def test_old_schema_unchanged_and_empty_taxonomy():
    assert "anyOf" not in BuildService._analysis_schema(TOPICS)["$defs"]["FacetDraft"]
    assert BuildService._analysis_schema([], registered_pairs=True)["$defs"]["UnitDraft"]["properties"]["facets"]["maxItems"] == 0


@pytest.mark.parametrize("status,value,claims,valid", [("unknown", "not established", [], False), ("unknown", None, [], True), ("known", "x", [], False), ("known", 123, ["c1"], False), ("conflicting", "x", ["c1", "c2"], False), ("conflicting", None, ["c1", "c2"], True)])
def test_status_value_and_support(status, value, claims, valid):
    assert validator().is_valid({"topic_key": "agent.loop", "facet_key": "lifecycle", "value_status": status, "value": value, "claim_keys": claims, "applicability_reason": "test"}) == valid

def test_analysis_reference_id_is_selected_from_actual_read_bundle():
    schema = BuildService._analysis_schema(TOPICS, read_ids=["actual-read0"])
    check = Draft202012Validator({"$ref": "#/$defs/ReadReference", "$defs": schema["$defs"]})
    assert check.is_valid({"read_id": "actual-read0", "quote": "source"})
    assert not check.is_valid({"read_id": "actual-read", "quote": "source"})
