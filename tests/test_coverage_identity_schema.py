from copy import deepcopy
from types import SimpleNamespace

import jsonschema
import pytest

from careerforge.contracts_scoped_review_v166 import ScopedCoverageOutput
from careerforge.services.scope_binding import coverage_identity_schema


def test_reviewer_references_are_limited_to_its_own_reads():
    from careerforge.contracts_scope_binding_v166 import RetryableScopedResearchReviewOutput
    from careerforge.services.scope_binding import reviewer_identity_schema
    schema = reviewer_identity_schema(RetryableScopedResearchReviewOutput,
        {"scope_identity": {}}, [{"read_id": "independent"}])
    reference = schema["$defs"]["ReadReference"]
    jsonschema.validate({"read_id": "independent", "quote": "actual source"}, reference)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"read_id": "analysis-read", "quote": "actual source"}, reference)


def test_relation_schema_excludes_claim_issue_indices_and_blocked_annotations():
    from careerforge.contracts_scoped_review_v166 import RelationScopedCoverageOutput
    subject = {"scope_identity": {"unit_version_id": "u", "content_hash": "c", "render_hash": "r"},
               "scope_elements": [{"element_id": "e", "claim_id": "claim", "fact_atom_id": "f"}]}
    review = SimpleNamespace(scope_review=SimpleNamespace(issue_scopes=[
        SimpleNamespace(issue_index=0, scope="claim", target_ids=["claim"]),
        SimpleNamespace(issue_index=1, scope="topic_link", target_ids=["blocked"])]),
        accepted_topic_link_ids=["blocked"], accepted_facet_ids=[], claim_decisions=[])
    schema = coverage_identity_schema(RelationScopedCoverageOutput, subject, "h", review, [])
    fields = schema["$defs"]["RelationAudit"]["properties"]
    jsonschema.validate([1], fields["confirmed_local_issue_indices"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([0], fields["confirmed_local_issue_indices"])
    assert fields["topic_links"]["maxItems"] == 0


def test_candidate_mapping_rejects_unknown_and_crossed_ids_without_forcing_verdict():
    subject = {
        "scope_identity": {"unit_version_id": "unit", "content_hash": "content", "render_hash": "render"},
        "scope_elements": [
            {"element_id": "e1", "claim_id": "c1", "fact_atom_id": "f1"},
            {"element_id": "e2", "claim_id": "c2", "fact_atom_id": "f2"},
        ],
    }
    original = deepcopy(ScopedCoverageOutput.model_json_schema())
    schema = coverage_identity_schema(ScopedCoverageOutput, subject, "review")
    item_schema = schema["$defs"]["StandaloneCoverageAudit"]["properties"]["elements"]["items"]
    for verdict in (True, False):
        item = {**subject["scope_elements"][0], "fact_bound": verdict,
                "conditions_preserved": verdict, "standalone": verdict, "reason": "Independent judgment"}
        jsonschema.validate(item, item_schema)
        for field, wrong in (("element_id", "invented"), ("claim_id", "c2"), ("fact_atom_id", "f2")):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({**item, field: wrong}, item_schema)
    assert ScopedCoverageOutput.model_json_schema() == original


@pytest.mark.parametrize("declared, fact_bound, valid", [(False, True, False), (False, False, True), (True, True, True)])
def test_local_issue_cannot_become_global_while_facts_remain_bound(declared, fact_bound, valid):
    from careerforge.services.coverage_issues import validate_global_scope
    from careerforge.errors import DomainError
    subject = {"scope_identity": {"unit_version_id":"u","content_hash":"c","render_hash":"r"},
               "scope_elements":[{"element_id":"e","claim_id":"claim","fact_atom_id":"f"}]}
    reason = "Independent global contradiction" if declared else "Topic link annotation unsupported"
    review = SimpleNamespace(scope_review=SimpleNamespace(issue_scopes=[], global_blocking_issues=[reason] if declared else []),blocking_issues=[])
    schema = coverage_identity_schema(ScopedCoverageOutput, subject, "h", review, bounded_global_scope=True)
    audit = {**subject["scope_identity"],"contract_version":"claim-scoped-review-1.6.6","reviewer_output_hash":"h", "global_blocking_issues":[reason],
             "elements":[{**subject["scope_elements"][0],"fact_bound":fact_bound,"standalone":True,"conditions_preserved":True,"reason":"test"}]}
    validator = jsonschema.Draft202012Validator({"$ref":"#/$defs/StandaloneCoverageAudit","$defs":schema["$defs"]})
    assert validator.is_valid(audit) == valid
    coverage = SimpleNamespace(scope_audit=SimpleNamespace(global_blocking_issues=[reason],elements=[SimpleNamespace(fact_bound=fact_bound)]))
    if valid:
        validate_global_scope(review, coverage)
    else:
        with pytest.raises(DomainError, match="全局"):
            validate_global_scope(review, coverage)

def test_reviewer_object_selection_rejects_truncated_and_wrong_kind_ids():
    from careerforge.contracts_scope_binding_v166 import RangeRetryableScopedResearchReviewOutput
    from careerforge.services.scope_binding import reviewer_identity_schema
    subject = {"claims": [{"claim_id": "claim-id"}],
               "topic_links": [{"id": "topic-id0"}], "facets": [{"id": "facet-id"}]}
    schema = reviewer_identity_schema(RangeRetryableScopedResearchReviewOutput, subject)
    issue = next(d for d in schema["$defs"].values() if "anyOf" in d and
                 any(v.get("properties", {}).get("scope", {}).get("enum") == ["topic_link"] for v in d["anyOf"]))
    row = {"issue_index": 0, "scope": "topic_link", "target_ids": ["topic-id0"]}
    jsonschema.validate(row, issue)
    for invalid in ("topic-id", "claim-id", "unknown"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**row, "target_ids": [invalid]}, issue)
