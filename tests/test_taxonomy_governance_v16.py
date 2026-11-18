"""Synthetic inputs exercise real G02 authorization, releases and publication paths."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import KnowledgeBuildInput
from careerforge.contracts_governance import (
    GovernedFacet,
    GovernedTopic,
    TaxonomyDecisionInput,
    TaxonomyProposalInput,
    TermAlias,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FacetValue, Publication, RunEvent, Topic, TopicLink
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal, set_grant
from careerforge.services.taxonomy import (
    expand_query,
    require_topic,
    resolve_topics,
    schema_view,
    taxonomy_revision,
    visible_topics,
)
from careerforge.services.taxonomy_governance import TaxonomyGovernanceService


async def evidence_fixture(env, *, public=False):
    source, publication, _ = await publish_fixture(
        env,
        key="public-evidence" if public else "private-evidence",
        namespace=env["owner"]["public_namespace_id"] if public else None,
    )
    with env["app"].db.read() as db:
        pack = env["app"].knowledge.publication_pack(db, env["actor"], publication)
    return source, pack["citations"][0]["citation_id"]


def definition(key, *, facets=None, relations=None, alias="SecretAlias739"):
    return GovernedTopic(
        key=key,
        title="Synthetic governed concept " + key,
        definition="A narrowly scoped synthetic concept for deterministic tests.",
        topic_kind="concept",
        aliases=[TermAlias(term=alias, language="en", context="synthetic testing")],
        facets=facets or {},
        relations=relations or [],
    )


def propose(
    env, evidence, definitions, *, operation="create", sources=None, ns=None, key="proposal"
):
    service = TaxonomyGovernanceService(env["app"])
    result = service.propose(
        env["actor"],
        TaxonomyProposalInput(
            namespace_id=ns or env["ns"],
            operation=operation,
            definitions=definitions,
            source_topic_versions=sources or {},
            evidence_ids=[evidence],
            rationale="Human-proposed synthetic governance test; no model approval.",
            idempotency_key=key,
        ),
    )
    return service, result


def approve(env, service, proposal, *, equivalence=False):
    return service.decide(
        env["actor"],
        proposal["proposal_id"],
        TaxonomyDecisionInput(
            expected_revision=proposal["revision"],
            expected_release_revision=service.status(env["actor"], proposal["namespace_id"])[
                "revision"
            ],
            approve=True,
            reason="Explicit namespace administrator review of frozen source evidence.",
            equivalence_confirmed=equivalence,
        ),
    )


def customize_analysis(
    env, topic_key, *, value=2, status="known", statement=None, facet_key="count"
):
    original = env["model"].complete

    async def complete(request):
        response = await original(request)
        if request["role"] == "analysis" and request.get("schema") and response.structured:
            payload = deepcopy(response.structured)
            unit = payload["units"][0]
            unit["topics"] = [{"topic_key": topic_key, "relation": "illustrates"}]
            unit["facets"] = (
                []
                if facet_key is None
                else [
                    {
                        "topic_key": topic_key,
                        "facet_key": facet_key,
                        "value_status": status,
                        "value": value,
                        "claim_keys": ["return"] if status != "unknown" else [],
                        "applicability_reason": "Explicit synthetic review boundary",
                    }
                ]
            )
            if statement:
                unit["claims"][0]["statement"] = statement
            return response.model_copy(update={"structured": payload})
        return response

    env["model"].complete = complete


def build(env, source, topic_key, key="governed-build"):
    return env["app"].build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source,
            question="Synthetic governed facet fixture",
            topic_keys=[topic_key],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key=key,
        ),
    )


@pytest.mark.asyncio
async def test_private_proposal_is_not_canonical_until_admin_release_and_never_globally_visible(
    env,
):
    _, evidence = await evidence_fixture(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "taxonomy-outsider")
        outsider = other.id
        before = taxonomy_revision(db, actor)
    service, proposal = propose(env, evidence, [definition("private.concept")])
    key = proposal["payload"]["definitions"][0]["key"]
    assert key.startswith("ns.") and proposal["provisional"]
    assert resolve_topics(app.db, "SecretAlias739", actor) == []
    release = approve(env, service, proposal)
    assert release["status"] == "approved"
    assert resolve_topics(app.db, "SecretAlias739", actor) == [key]
    assert resolve_topics(app.db, "SecretAlias739", outsider) == []
    assert resolve_topics(app.db, "SecretAlias739") == []
    assert expand_query(app.db, "plain", [key], outsider) == ["plain"]
    with app.db.read() as db:
        assert taxonomy_revision(db, actor) != before
        assert require_topic(db, actor, key).version == 1
        assert schema_view(db, actor, key)["namespace_id"] == env["ns"]
    with pytest.raises(DomainError):
        service.detail(outsider, proposal["proposal_id"])


@pytest.mark.asyncio
async def test_proposal_idempotency_and_release_cas_are_explicit(env):
    _, evidence = await evidence_fixture(env)
    service, first = propose(env, evidence, [definition("cas.first")])
    _, replay = propose(env, evidence, [definition("cas.first")])
    assert replay["proposal_id"] == first["proposal_id"]
    with pytest.raises(DomainError, match="同一工作单"):
        propose(env, evidence, [definition("cas.changed")])
    _, second = propose(env, evidence, [definition("cas.second")], key="second")
    approved = approve(env, service, first)
    with pytest.raises(DomainError) as exc:
        service.decide(
            env["actor"],
            second["proposal_id"],
            TaxonomyDecisionInput(
                expected_revision=1,
                expected_release_revision=0,
                approve=True,
                reason="Stale release CAS",
            ),
        )
    assert exc.value.code == "TAXONOMY_CONFLICT"
    with pytest.raises(DomainError):
        approve(env, service, first)
    historical = service.release(env["actor"], approved["release_id"])
    approve(env, service, second)
    assert service.release(env["actor"], approved["release_id"]) == historical


@pytest.mark.asyncio
async def test_public_namespace_requires_admin_and_public_provenance(env):
    _, private_evidence = await evidence_fixture(env)
    public = env["owner"]["public_namespace_id"]
    with pytest.raises(DomainError) as exc:
        propose(env, private_evidence, [definition("public.denied")], ns=public)
    assert exc.value.code == "PRIVATE_TO_PUBLIC"
    # Use separately imported and independently reviewed public evidence.
    _, public_evidence = await evidence_fixture(env, public=True)
    service, proposal = propose(
        env, public_evidence, [definition("public.allowed")], ns=public, key="public"
    )
    with env["app"].db.transaction() as db:
        writer, _, _ = create_principal(db, "taxonomy-writer")
        writer_id = writer.id
        set_grant(db, env["actor"], public, writer_id, "writer", True)
    with pytest.raises(DomainError) as exc:
        service.decide(
            writer_id,
            proposal["proposal_id"],
            TaxonomyDecisionInput(
                expected_revision=1,
                expected_release_revision=0,
                approve=True,
                reason="Not an administrator",
            ),
        )
    assert exc.value.code == "FORBIDDEN"
    approve(env, service, proposal)
    assert "public.allowed" in resolve_topics(env["app"].db, "SecretAlias739")


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["prerequisite", "broader"])
async def test_cycles_are_rejected_atomically_including_new_private_local_keys(env, relation):
    _, evidence = await evidence_fixture(env)
    service, proposal = propose(
        env,
        evidence,
        [
            definition(
                "cycle.first", relations=[{"target_key": "cycle.second", "relation": relation}]
            ),
            definition(
                "cycle.second", relations=[{"target_key": "cycle.first", "relation": relation}]
            ),
        ],
    )
    with pytest.raises(DomainError) as exc:
        approve(env, service, proposal)
    assert exc.value.code == "TOPIC_CYCLE"
    assert service.detail(env["actor"], proposal["proposal_id"])["status"] == "proposed"
    with env["app"].db.read() as db:
        assert not db.scalar(select(Topic).where(Topic.key.like("%cycle.first")))
    assert service.status(env["actor"], env["ns"])["revision"] == 0


@pytest.mark.asyncio
async def test_revoked_provenance_blocks_approval_and_alias_use_before_async_propagation(env):
    _, evidence = await evidence_fixture(env)
    service, proposal = propose(env, evidence, [definition("revoked.before")])
    _, approved_proposal = propose(env, evidence, [definition("revoked.after")], key="after")
    approve(env, service, approved_proposal)
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], evidence, "revoked", "test provenance revoked")
    with pytest.raises(DomainError):
        approve(env, service, proposal)
    assert resolve_topics(env["app"].db, "SecretAlias739", env["actor"]) == []


@pytest.mark.asyncio
async def test_frozen_schema_survives_new_release_and_legacy_mapping_never_invents_value(env):
    source, evidence = await evidence_fixture(env)
    facets = {
        "count": GovernedFacet(
            question="Synthetic count", value_type="number", unit="items", required=True
        )
    }
    service, proposal = propose(env, evidence, [definition("frozen.schema", facets=facets)])
    approve(env, service, proposal)
    key = proposal["payload"]["definitions"][0]["key"]
    customize_analysis(env, key)
    run = build(env, source, key)
    # New schema is approved after queueing, before analysis has read or emitted.
    facets2 = {**facets, "new_cell": GovernedFacet(question="New observation", required=True)}
    _, revision = propose(
        env,
        evidence,
        [definition(key, facets=facets2)],
        operation="schema",
        sources={key: 1},
        key="schema-2",
    )
    approve(env, service, revision)
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    assert result and result["publication_ids"], result
    pub = result["publication_ids"][0]
    with env["app"].db.read() as db:
        pack = env["app"].knowledge.publication_pack(db, env["actor"], pub)
        assert pack["facets"][0]["schema_version"] == 1
        assert pack["facets"][0]["value"] == 2
    mapping = service.map_facets(env["actor"], pub, key)
    cells = {c["facet_key"]: c for c in mapping["cells"]}
    assert cells["count"]["mapping_status"] == "compatible_definition"
    assert cells["new_cell"]["value_status"] == "unmapped_legacy"
    assert cells["new_cell"]["value"] is None
    assert mapping["model_calls"] == 0 and not mapping["knowledge_rewritten"]
    assert service.map_facets(env["actor"], pub, key, 1)["cells"][0]["mapping_status"] == "exact"
    # Unit/type changes do not silently convert old observations either.
    _, next_revision = propose(
        env,
        evidence,
        [
            definition(
                key,
                facets={
                    "count": GovernedFacet(
                        question="Synthetic count",
                        value_type="number",
                        unit="kilograms",
                        required=True,
                    )
                },
            )
        ],
        operation="schema",
        sources={key: 2},
        key="schema-3",
    )
    approve(env, service, next_revision)
    assert (
        service.map_facets(env["actor"], pub, key)["cells"][0]["value_status"] == "unmapped_legacy"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["merge", "split"])
async def test_merge_split_keeps_old_ids_and_reviewed_links_without_copying(env, operation):
    source, evidence = await evidence_fixture(env)
    service, proposal = propose(env, evidence, [definition("old.first"), definition("old.second")])
    approve(env, service, proposal)
    first, second = [d["key"] for d in proposal["payload"]["definitions"]]
    customize_analysis(env, first, facet_key=None)
    run = build(env, source, first)
    await env["app"].drain()
    pub = env["app"].runtime.stage(run, "result")["publication_ids"][0]
    with env["app"].db.read() as db:
        original = env["app"].knowledge.publication_pack(db, env["actor"], pub)
        old_links = [(link.id, link.topic_id) for link in db.scalars(select(TopicLink))]
    targets = (
        [definition("new.merged")]
        if operation == "merge"
        else [definition("new.split_a"), definition("new.split_b")]
    )
    sources = {first: 1, second: 1} if operation == "merge" else {first: 1}
    _, change = propose(env, evidence, targets, operation=operation, sources=sources, key=operation)
    if operation == "merge":
        with pytest.raises(DomainError) as exc:
            approve(env, service, change)
        assert exc.value.code == "TOPIC_EQUIVALENCE"
    approve(env, service, change, equivalence=operation == "merge")
    with env["app"].db.read() as db:
        assert env["app"].knowledge.publication_pack(db, env["actor"], pub) == original
        assert [(link.id, link.topic_id) for link in db.scalars(select(TopicLink))] == old_links
        assert first not in {topic.key for topic in visible_topics(db, env["actor"])}
        assert require_topic(db, env["actor"], first, 1).key == first
        with pytest.raises(DomainError) as exc:
            require_topic(db, env["actor"], first)
        assert exc.value.code == "TOPIC_RETIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,value,fields,enum,expected",
    [
        ("number", True, {}, [], False),
        ("number", 2, {}, [], True),
        ("boolean", 1, {}, [], False),
        ("boolean", False, {}, [], True),
        ("enum", "unknown-choice", {}, ["yes", "no"], False),
        ("enum", "yes", {}, ["yes", "no"], True),
        ("object", {"count": True}, {"count": "number"}, [], False),
        ("object", {"count": 2, "extra": 1}, {"count": "number"}, [], False),
        ("object", {"count": 2}, {"count": "number"}, [], True),
    ],
)
async def test_facet_values_use_exact_frozen_json_types(env, kind, value, fields, enum, expected):
    source, evidence = await evidence_fixture(env)
    service, proposal = propose(
        env,
        evidence,
        [
            definition(
                "typed.facet",
                facets={
                    "count": GovernedFacet(
                        question="Typed fixture",
                        value_type=kind,
                        object_fields=fields,
                        enum_values=enum,
                    )
                },
            )
        ],
    )
    approve(env, service, proposal)
    key = proposal["payload"]["definitions"][0]["key"]
    customize_analysis(env, key, value=value)
    run = build(env, source, key)
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    publications = result["publication_ids"] if result else []
    assert bool(publications) is expected
    if expected:
        with env["app"].db.read() as db:
            pack = env["app"].knowledge.publication_pack(db, env["actor"], publications[0])
        assert pack["facets"][0]["value"] == value
    else:
        with env["app"].db.read() as db:
            child = db.scalar(
                select(AgentRun).where(AgentRun.parent_id == run, AgentRun.workflow == "analysis")
            )
            assert child.status == "failed"
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == child.id)))
            assert any(event.payload.get("code") == "FACET_VALUE_TYPE" for event in events)
            assert not db.scalar(
                select(FacetValue).join(
                    Publication, Publication.unit_version_id == FacetValue.unit_version_id
                )
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_required_facet_must_be_explicit_but_unknown_is_not_fabricated(env, missing):
    source, evidence = await evidence_fixture(env)
    service, proposal = propose(
        env,
        evidence,
        [
            definition(
                "required.facet",
                facets={"count": GovernedFacet(question="Required observation", required=True)},
            )
        ],
    )
    approve(env, service, proposal)
    key = proposal["payload"]["definitions"][0]["key"]
    customize_analysis(
        env, key, value=None, status="unknown", facet_key=None if missing else "count"
    )
    run = build(env, source, key)
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    assert bool(result and result["publication_ids"]) is not missing
    if not missing:
        with env["app"].db.read() as db:
            pack = env["app"].knowledge.publication_pack(
                db, env["actor"], result["publication_ids"][0]
            )
        assert pack["facets"][0]["value_status"] == "unknown"
        assert pack["facets"][0]["value"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "predicate,operand,status,value,succeeds",
    [
        ("path_present", "not-in-scope.py", "not_applicable", None, False),
        ("path_present", "not-in-scope.py", "known", "exists", False),
        ("path_present", "not-in-scope.py", "unknown", None, True),
        ("path_present", "src/tools.py", "known", "exists", True),
        ("unit_type_is", "concept", "not_applicable", None, True),
    ],
)
async def test_typed_applicability_rules_do_not_treat_absence_as_nonapplicable(
    env, predicate, operand, status, value, succeeds
):
    source, evidence = await evidence_fixture(env)
    service, proposal = propose(
        env,
        evidence,
        [
            definition(
                "applicable.facet",
                facets={
                    "count": GovernedFacet(
                        question="Applicable observation",
                        applicability="requires_claim",
                        preconditions=[{"predicate": predicate, "operand": operand}],
                    )
                },
            )
        ],
    )
    approve(env, service, proposal)
    key = proposal["payload"]["definitions"][0]["key"]
    customize_analysis(env, key, value=value, status=status)
    run = build(env, source, key)
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    assert bool(result and result["publication_ids"]) is succeeds
    if succeeds:
        reviewer = next(
            call
            for call in reversed(env["model"].calls)
            if call["role"] == "reviewer" and call.get("schema")
        )
        facet = reviewer["context"]["subject"]["facets"][0]
        assert facet["schema_rule"]["preconditions"] == [
            {"predicate": predicate, "operand": operand}
        ]
        assert facet["applicability_reason"] and facet["schema_version"] == 1


@pytest.mark.asyncio
async def test_new_topic_created_after_queue_is_not_part_of_frozen_analysis_authority(env):
    source, evidence = await evidence_fixture(env)
    run = build(env, source, "agent.tool_calling")
    service, proposal = propose(env, evidence, [definition("late.topic")])
    approve(env, service, proposal)
    key = proposal["payload"]["definitions"][0]["key"]
    customize_analysis(env, key, facet_key=None)
    await env["app"].drain()
    result = env["app"].runtime.stage(run, "result")
    assert not result or not result["publication_ids"]
    with env["app"].db.read() as db:
        child = db.scalar(
            select(AgentRun).where(AgentRun.parent_id == run, AgentRun.workflow == "analysis")
        )
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == child.id)))
        assert any(event.payload.get("code") == "TOPIC_UNREGISTERED" for event in events)


@pytest.mark.asyncio
async def test_authenticated_governance_http_proposal_decision_and_legacy_mapping(env):
    import httpx

    from careerforge.api import create_app

    _, evidence = await evidence_fixture(env)
    body = TaxonomyProposalInput(
        namespace_id=env["ns"],
        operation="create",
        definitions=[definition("http.topic")],
        evidence_ids=[evidence],
        rationale="Explicit human HTTP governance request",
        idempotency_key="http-proposal",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])), base_url="http://test"
    ) as client:
        assert (
            await client.post("/api/v1/taxonomy/proposals", json=body.model_dump(mode="json"))
        ).status_code == 401
        client.headers["Authorization"] = "Bearer " + env["owner"]["token"]
        proposal = await client.post(
            "/api/v1/taxonomy/proposals", json=body.model_dump(mode="json")
        )
        assert proposal.status_code == 200, proposal.text
        result = await client.post(
            "/api/v1/taxonomy/proposals/" + proposal.json()["proposal_id"] + "/decisions",
            json={
                "expected_revision": 1,
                "expected_release_revision": 0,
                "approve": True,
                "reason": "Explicit authenticated namespace admin approval",
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "approved"
        release = await client.get("/api/v1/taxonomy/releases/" + result.json()["release_id"])
        assert release.status_code == 200 and release.json()["revision"] == 1
