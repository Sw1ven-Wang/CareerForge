from copy import deepcopy
import pytest
from jsonschema import Draft202012Validator
from careerforge.services.quote_correction import frozen_quote_schema, _without_ambiguous_references
from careerforge.contracts import ReviewOutput


def test_correction_ids_are_bounded_and_semantics_still_frozen():
    original = {"decision":"accept_scoped","claim_decisions":[{"claim_id":"real-id","boundary":"fixed", "independent_reads":[{"read_id":"r1","quote":"wrong"}]}]}
    pointers=["/claim_decisions/0/independent_reads/0"]
    schema=frozen_quote_schema(original,pointers,["r1","r2"],ReviewOutput.model_json_schema())
    identities=[]
    def find(v):
        if isinstance(v,dict):
            for k,p in v.get("properties",{}).items():
                if k in {"claim_id","read_id"}:identities.append((k,p))
            for x in v.values():find(x)
        elif isinstance(v,list):
            for x in v:find(x)
    find(schema)
    assert identities
    for key,field in identities:
        assert field["enum"] == (["real-id"] if key=="claim_id" else ["r1","r2"])
    patched=deepcopy(original);patched["claim_decisions"][0]["independent_reads"][0]={"read_id":"r2","quote":"exact"}
    assert _without_ambiguous_references(original,pointers)==_without_ambiguous_references(patched,pointers)
    patched["claim_decisions"][0]["boundary"]="changed"
    assert _without_ambiguous_references(original,pointers)!=_without_ambiguous_references(patched,pointers)


def test_patch_cannot_change_other_fields_or_write_outside_diagnosed_reference():
    from careerforge.services.quote_correction import QuotePatches, apply_quote_patches
    from careerforge.errors import DomainError
    original={"claim_id":"immutable","refs":[{"read_id":"r1","quote":"old"}]}
    patch=QuotePatches.model_validate({"patches":[{"pointer":"/refs/0","reference":{"read_id":"r2","quote":"new quote"}}]})
    merged=apply_quote_patches(original,patch,["/refs/0"])
    assert merged["claim_id"]=="immutable" and original["refs"][0]["quote"]=="old"
    with pytest.raises(DomainError):apply_quote_patches(original,patch,[])
    with pytest.raises(DomainError):apply_quote_patches(original,QuotePatches(patches=[]),["/refs/0"])
