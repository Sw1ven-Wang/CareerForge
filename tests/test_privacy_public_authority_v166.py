"""Public evidence survives personal erasure without retaining private read data."""

import json
from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FILES
from test_privacy_v16 import confirm, service

from careerforge.contracts import ImportSourceInput
from careerforge.models import (
    AgentRun,
    Evidence,
    FundingAgreement,
    Namespace,
    ObjectRow,
    SourceRead,
    SourceSet,
)
from careerforge.util import digest, fingerprint


def source_fixture(env, *, private=False, key="read-authority"):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"] if private else env["owner"]["public_namespace_id"],
            repository_key="fixtures/" + key,
            commit_sha="d" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )


@pytest.mark.asyncio
async def test_public_publication_retains_only_authority_spans_after_admin_erasure(env):
    app, actor = env["app"], env["actor"]
    source, publication, run_id = await publish_fixture(
        env, namespace=env["owner"]["public_namespace_id"]
    )
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        funding = db.get(FundingAgreement, run.funding_id)
        public_namespace = db.get(Namespace, run.namespace_id)
        assert public_namespace.kind == "shared_public" and funding.kind == "system"
        before_pack = deepcopy(app.knowledge.publication_pack(db, actor, publication))
        reads = list(db.scalars(select(SourceRead).where(SourceRead.source_id == source)))
        referenced = set(db.scalars(select(Evidence.read_id).where(Evidence.source_id == source)))
        assert reads and referenced == {read.id for read in reads}
        before = {
            read.id: (read.path, read.text, read.file_hash, read.quote_hash) for read in reads
        }
        for read in reads:
            read.session_key = "PERSONAL_SESSION_METADATA_ERASE"
        selected = service(env)._scope(db, actor)
        assert run_id not in {row["id"] for row in selected["agent_runs"]}
        assert referenced <= {row["id"] for row in selected["source_reads"]}
        print(
            "PUBLIC_WITNESS_PROVENANCE "
            + json.dumps(
                {
                    "namespace_kind": public_namespace.kind,
                    "funding_kind": funding.kind,
                    "run_is_personally_selected": False,
                    "public_source_id": source,
                    "publication_id": publication,
                    "public_witness_ids_selected_for_personal_erasure": sorted(referenced),
                    "source_namespace_id": db.get(ObjectRow, source).namespace_id,
                    "publication_namespace_id": db.get(ObjectRow, publication).namespace_id,
                },
                sort_keys=True,
            )
        )
    unused = app.sources.read_span(
        actor, source, "src/tools.py", 1, 3, "analysis", "PERSONAL_UNUSED_READ"
    )
    unused_id = unused["read_id"]
    assert unused_id not in referenced
    svc = service(env)
    proposal = svc.propose(actor)
    for _ in range(2):
        assert (await confirm(svc, actor, proposal))["state"] == "completed"
        with app.db.read() as db:
            assert app.knowledge.publication_pack(db, actor, publication) == before_pack
            for read_id, public_span in before.items():
                read = db.get(SourceRead, read_id)
                assert (read.path, read.text, read.file_hash, read.quote_hash) == public_span
                assert read.session_key == ""
            read = db.get(SourceRead, unused_id)
            assert read.path == read.text == read.session_key == ""
    # A new erasure operation, not only replay of the first confirmation, is safe.
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert app.knowledge.publication_pack(db, actor, publication) == before_pack


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "private_source",
        "private_evidence",
        "unreferenced",
        "revoked_source",
        "revoked_evidence",
        "mismatched_source",
        "tampered_source_files",
        "tampered_source_identity",
        "tampered_source_license",
        "tampered_source_rights",
        "tampered_source_synthetic",
        "tampered_source_hash",
        "tampered_read_text",
        "tampered_read_hash",
        "tampered_evidence_hash",
        "tampered_evidence_quote",
        "mismatched_evidence_projection",
        "wrong_line_binding",
        "ambiguous_quote",
        "mixed_no_active_public_evidence",
    ],
)
async def test_public_labels_do_not_exempt_private_or_invalid_read_witnesses(env, mutation):
    app, actor = env["app"], env["actor"]
    source = source_fixture(env, private=mutation == "private_source")
    other_source = (
        source_fixture(env, key="different-source") if mutation == "mismatched_source" else None
    )
    observed = app.sources.read_span(
        actor, source, "src/tools.py", 1, 3, "analysis", "PERSONAL_READ_ERASE"
    )
    read_id = observed["read_id"]
    with app.db.transaction() as db:
        read = db.get(SourceRead, read_id)
        if mutation != "unreferenced":
            evidence_id = app.sources.evidence_from_read(
                db,
                actor,
                env["ns"]
                if mutation in {"private_source", "private_evidence"}
                else env["owner"]["public_namespace_id"],
                source,
                read_id,
                "return value",
                "analysis",
                "PERSONAL_READ_ERASE",
            )
            db.flush()
            evidence = db.get(Evidence, evidence_id)
            obj = db.get(ObjectRow, evidence_id)
        source_row, source_obj = db.get(SourceSet, source), db.get(ObjectRow, source)
        if mutation == "revoked_source":
            source_obj.state = "revoked"
        elif mutation in {"revoked_evidence", "mixed_no_active_public_evidence"}:
            obj.state = "revoked"
            if mutation == "mixed_no_active_public_evidence":
                app.sources.evidence_from_read(
                    db,
                    actor,
                    env["ns"],
                    source,
                    read_id,
                    "return value",
                    "analysis",
                    "PERSONAL_READ_ERASE",
                )
        elif mutation == "mismatched_source":
            # A corrupted projection must not turn another source into authority.
            evidence.source_id = other_source
        elif mutation == "tampered_source_files":
            source_row.files = {**source_row.files, "src/tools.py": "PRIVATE_TAMPERED_SOURCE"}
        elif mutation == "tampered_source_identity":
            source_row.repository_key = "PRIVATE_TAMPERED_IDENTITY"
        elif mutation == "tampered_source_license":
            source_row.license_id = "PRIVATE_TAMPERED_LICENSE"
        elif mutation == "tampered_source_rights":
            source_row.metadata_json = {
                **source_row.metadata_json,
                "rights_confirmed": not source_row.metadata_json.get("rights_confirmed"),
            }
        elif mutation == "tampered_source_synthetic":
            source_row.metadata_json = {**source_row.metadata_json, "synthetic": False}
        elif mutation == "tampered_source_hash":
            source_obj.content_hash = "0" * 64
        elif mutation == "tampered_read_text":
            read.text = "PRIVATE_TAMPERED_READ"
        elif mutation == "tampered_read_hash":
            read.quote_hash = "0" * 64
        elif mutation == "tampered_evidence_hash":
            obj.content_hash = "0" * 64
        elif mutation == "tampered_evidence_quote":
            evidence.exact_quote = "PRIVATE_TAMPERED_QUOTE"
        elif mutation == "mismatched_evidence_projection":
            evidence.path = "private/incorrect.py"
        elif mutation == "wrong_line_binding":
            evidence.start_line += 1
            obj.payload = {**obj.payload, "start_line": evidence.start_line}
            obj.content_hash = fingerprint(obj.payload)
        elif mutation == "ambiguous_quote":
            evidence.exact_quote = "e"
            evidence.quote_hash = digest(evidence.exact_quote)
            obj.payload = {**obj.payload, "quote_hash": evidence.quote_hash}
            obj.content_hash = fingerprint(obj.payload)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        read = db.get(SourceRead, read_id)
        assert read.text == read.path == read.session_key == ""
    assert env["model"].calls == []


@pytest.mark.asyncio
async def test_mixed_public_and_private_evidence_retains_public_span_only(env):
    app, actor = env["app"], env["actor"]
    source = source_fixture(env)
    observed = app.sources.read_span(
        actor, source, "src/tools.py", 1, 3, "analysis", "PERSONAL_MIXED_SESSION"
    )
    read_id = observed["read_id"]
    with app.db.transaction() as db:
        ids = {}
        for label, namespace in (
            ("public", env["owner"]["public_namespace_id"]),
            ("private", env["ns"]),
        ):
            ids[label] = app.sources.evidence_from_read(
                db,
                actor,
                namespace,
                source,
                read_id,
                "return value",
                "analysis",
                "PERSONAL_MIXED_SESSION",
            )
        db.flush()
        before = deepcopy(app.knowledge.evidence_dict(db, actor, ids["public"]))
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert app.knowledge.evidence_dict(db, actor, ids["public"]) == before
        assert db.get(ObjectRow, ids["private"]).payload == {}
        read = db.get(SourceRead, read_id)
        assert read.text == FILES["src/tools.py"] and read.session_key == ""
    assert env["model"].calls == []
