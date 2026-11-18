"""Bounded, owner-specific migration exports from the inspected fixed baseline."""

from __future__ import annotations

import json
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contracts import Contract
from .util import canonical, fingerprint

LEGACY_BASELINE = "90e32f187ba44f9273cd3e2c7c12af2aeaba5e93"
LEGACY_TABLES = {
    "sources",
    "source_snapshots",
    "citations",
    "repositories",
    "repository_snapshots",
    "repository_files",
    "code_citations",
    "career_goals",
    "career_goal_versions",
    "learning_plans",
    "learning_plan_versions",
    "learning_workspaces",
    "study_tracks",
    "study_artifacts",
    "learning_progress",
    "personal_evidence",
    "personal_evidence_versions",
    "submissions",
    "artifact_feedback",
}


class LegacyRecord(Contract):
    table_name: str
    legacy_id: str
    data: dict[str, JsonValue]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("table_name")
    @classmethod
    def supported_table(cls, value: str) -> str:
        if value not in LEGACY_TABLES:
            raise ValueError(
                "Unsupported table; credentials, sessions and billing are never imported"
            )
        return value

    @field_validator("legacy_id")
    @classmethod
    def canonical_identifier(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Legacy identifiers must be canonical UUID strings")
        return value

    @model_validator(mode="after")
    def unchanged(self) -> Self:
        try:
            encoded = json.dumps(self.data, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("Legacy data must be bounded, finite JSON") from exc
        if len(encoded.encode("utf-8")) > 8_000_000:
            raise ValueError("Legacy record exceeds the bounded transfer contract")
        if fingerprint(self.data) != self.content_hash:
            raise ValueError("Legacy record content hash does not match its bytes")
        # Never import old authentication state even if a caller disguises its table.
        if {"password_hash", "token_hash", "csrf_token", "api_key"} & set(self.data):
            raise ValueError("Credential columns cannot be imported")
        if self.data.get("id", self.data.get("unit_id")) != self.legacy_id:
            raise ValueError("Record identifier differs from its payload")
        return self


class LegacyBundle(Contract):
    format: Literal["careerforge_legacy_export_v1"] = "careerforge_legacy_export_v1"
    baseline_sha: Literal["90e32f187ba44f9273cd3e2c7c12af2aeaba5e93"] = (
        "90e32f187ba44f9273cd3e2c7c12af2aeaba5e93"
    )
    owner_id: str
    records: list[LegacyRecord] = Field(max_length=10000)
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("owner_id")
    @classmethod
    def owner_identifier(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Owner must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def fixed_owner_and_manifest(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for record in self.records:
            if record.data.get("owner_id") != self.owner_id:
                raise ValueError("Mixed-owner exports cannot be imported")
            identity = (record.table_name, record.legacy_id)
            if identity in seen:
                raise ValueError("Duplicate historical record")
            seen.add(identity)
        payload = self.model_dump(mode="json", exclude={"bundle_hash"})
        if len(canonical(payload).encode("utf-8")) > 8_000_000:
            raise ValueError("Legacy export exceeds the bounded 8 MB transfer contract")
        if fingerprint(payload) != self.bundle_hash:
            raise ValueError("Legacy export manifest hash mismatch")
        return self


class LegacyImportInput(Contract):
    namespace_id: str
    bundle: LegacyBundle
    confirm: Literal["IMPORT_UNREVIEWED"]


class LegacySourceInput(Contract):
    snapshot_record_id: str
    license_id: str = Field(min_length=1, max_length=100)
    rights_confirmed: bool
    confirm: Literal["IMPORT_SOURCE_NOT_KNOWLEDGE"]
