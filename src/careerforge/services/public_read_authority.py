"""Minimal public review provenance across an authorized personal erasure.

This is not new evidence or approval. Before the privacy fence closes, the
ordinary publication/receipt guards must validate a public system publication.
Only already-retained evidence reads get a hash-only session witness. Later
reads still verify the complete original bundle, receipts, source and access.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts_scoped_review_v166 import SCOPED_REVIEW_VERSION
from ..errors import DomainError
from ..models import (
    AgentRun,
    ModelReceipt,
    Publication,
    ReviewDecision,
    SourceRead,
    StageArtifact,
    UnitVersion,
)
from ..models_privacy_ext import PrivacyDeletion, PrivacyState
from ..util import digest, fingerprint
from .consumer_context import CONSUMER_CONTEXT_CONTRACT
from .dependencies import check_closure, require_object
from .runtime import is_public_system_run
from .scoped_review import scoped_review_enabled

PUBLIC_READ_ERASURE_CONTRACT = "public-reviewed-read-erasure-1.6.6-v1"
PUBLIC_READ_ERASURE_STAGE = "public-read-erasure:"


def _receipt_hash(db: Session, run: AgentRun, version: UnitVersion) -> str:
    final = "review.final:" + version.id
    keys = [final, final + ":quote-correction:1", "coverage:" + version.id]
    rows = []
    for receipt in db.scalars(
        select(ModelReceipt)
        .where(ModelReceipt.run_id == run.id, ModelReceipt.stage_key.in_(keys))
        .order_by(ModelReceipt.stage_key)
    ):
        access = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key == "model-access:" + receipt.id,
            )
        )
        rows.append(
            {
                "receipt_id": receipt.id,
                "stage_key": receipt.stage_key,
                "request_hash": receipt.request_hash,
                "response_hash": fingerprint(receipt.response),
                "role": receipt.role,
                "status": receipt.status,
                "payer_id": receipt.payer_id,
                "profile_version": receipt.profile_version,
                "access_input_hash": access.input_hash if access else None,
                "access_payload_hash": fingerprint(access.payload) if access else None,
            }
        )
    return fingerprint(rows)


def _binding(
    db: Session,
    publication: Publication,
    review: ReviewDecision,
    run: AgentRun,
    version: UnitVersion,
    bundle: dict,
    read: SourceRead,
) -> str:
    return fingerprint(
        {
            "contract": PUBLIC_READ_ERASURE_CONTRACT,
            "consumer_context_contract": CONSUMER_CONTEXT_CONTRACT,
            "publication_id": publication.id,
            "publication_namespace_id": publication.namespace_id,
            "review_id": review.id,
            "review_hash": fingerprint(review.report),
            "review_contract": SCOPED_REVIEW_VERSION,
            "run_id": run.id,
            "run_input_hash": run.input_hash,
            "unit_version_id": version.id,
            "content_hash": version.content_hash,
            "render_hash": version.render_hash,
            "bundle_hash": fingerprint(bundle),
            "receipt_authority_hash": _receipt_hash(db, run, version),
            "read_id": read.id,
            "read_hash": fingerprint(
                {
                    "source_id": read.source_id,
                    "principal_id": read.principal_id,
                    "run_id": read.run_id,
                    "role": read.role,
                    "path": read.path,
                    "first_line": read.first_line,
                    "last_line": read.last_line,
                    "text_hash": digest(read.text),
                    "file_hash": read.file_hash,
                    "quote_hash": read.quote_hash,
                }
            ),
            "session_hash": digest("reviewer:" + version.id),
        }
    )


def _public_system_run(db: Session, run: AgentRun) -> bool:
    return is_public_system_run(db, run) and run.workflow == "review" and run.status == "succeeded"


def retained_public_review_session(
    db: Session, actor: str, run: AgentRun, version: UnitVersion, bundle: dict, read: SourceRead
) -> bool:
    """A blank session alone is never an erasure or independent-read proof."""
    if read.session_key != "" or not _public_system_run(db, run):
        return False
    witness = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run.id,
            StageArtifact.stage_key == PUBLIC_READ_ERASURE_STAGE + read.id,
        )
    )
    if witness is None or witness.input_hash != run.input_hash:
        return False
    payload = witness.payload
    if not isinstance(payload, dict) or set(payload) != {
        "contract",
        "publication_id",
        "deletion_id",
        "binding_hash",
    }:
        return False
    if not all(isinstance(value, str) for value in payload.values()):
        return False
    publication = db.get(Publication, payload.get("publication_id"))
    review = db.get(ReviewDecision, publication.review_id) if publication else None
    if (
        publication is None
        or publication.namespace_id != run.namespace_id
        or publication.unit_version_id != version.id
        or review is None
        or review.unit_version_id != version.id
        or review.reviewer_run_id != run.id
        or review.report.get("knowledge_review_contract_version") != SCOPED_REVIEW_VERSION
        or not scoped_review_enabled(run.input_manifest)
        or fingerprint(run.input_manifest) != run.input_hash
    ):
        return False
    check_closure(db, actor, [publication.id])
    publication_obj = require_object(db, actor, publication.id, "publication")
    review_obj = require_object(db, actor, review.id, "review_decision")
    if (
        publication_obj.payload.get("unit_version_id") != version.id
        or publication_obj.payload.get("review_id") != review.id
        or review_obj.payload != review.report
    ):
        return False
    deletion = db.get(PrivacyDeletion, payload.get("deletion_id"))
    state = db.get(PrivacyState, read.principal_id)
    return bool(
        deletion
        and deletion.principal_id == read.principal_id == run.principal_id
        and deletion.scope == "all_personal_content"
        and deletion.state in {"purging_cache", "completed"}
        and state
        and state.generation > deletion.generation
        and payload["contract"] == PUBLIC_READ_ERASURE_CONTRACT
        and payload["binding_hash"] == _binding(db, publication, review, run, version, bundle, read)
    )


def freeze_public_review_sessions(
    db: Session,
    actor: str,
    deletion_id: str,
    retained_read_ids: set[str],
    knowledge: Any,
) -> None:
    """Run only before the confirmed erasure closes the actor's privacy fence.

    No raw body, personal session string, prompt, model response or standalone
    verdict is copied. Invalid or historical unknown authority is not repaired.
    """
    rows = db.execute(
        select(Publication, ReviewDecision, UnitVersion, AgentRun)
        .join(ReviewDecision, ReviewDecision.id == Publication.review_id)
        .join(UnitVersion, UnitVersion.id == Publication.unit_version_id)
        .join(AgentRun, AgentRun.id == ReviewDecision.reviewer_run_id)
        .where(AgentRun.principal_id == actor)
    )
    seen = set()
    for publication, review, version, run in rows:
        if version.id in seen or not _public_system_run(db, run):
            continue
        try:
            pack = knowledge.publication_pack(
                db, actor, publication.id, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
            )
            if not any(row["standalone"] for row in pack["consumer_context"]["facts"].values()):
                continue
            bundle = knowledge._scoped_bundle_in(db, actor, run, version)
        except DomainError:
            # Erasure must still proceed; missing proof never becomes approval.
            continue
        seen.add(version.id)
        for item in bundle["reads"]:
            read = db.get(SourceRead, item["read_id"])
            if (
                read is None
                or read.id not in retained_read_ids
                or read.session_key != "reviewer:" + version.id
            ):
                continue
            key = PUBLIC_READ_ERASURE_STAGE + read.id
            old = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == key
                )
            )
            if old is None:
                db.add(
                    StageArtifact(
                        run_id=run.id,
                        stage_key=key,
                        input_hash=run.input_hash,
                        payload={
                            "contract": PUBLIC_READ_ERASURE_CONTRACT,
                            "publication_id": publication.id,
                            "deletion_id": deletion_id,
                            "binding_hash": _binding(
                                db, publication, review, run, version, bundle, read
                            ),
                        },
                    )
                )
