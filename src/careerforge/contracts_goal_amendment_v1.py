"""User objectives are not employer requirements or evidence of achieved ability."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .util import digest, fingerprint

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=80)]
Depth = Literal["掌握", "熟悉", "了解", "精通"]
Priority = Literal["required", "preferred", "unspecified"]


class SourceSpan(Contract):
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=1, strict=True)


class DialogueCommandSource(Contract):
    kind: Literal["dialogue_text"] = "dialogue_text"
    text: str = Field(min_length=1, max_length=50_000)
    raw_sha256: Hash
    byte_count: int = Field(ge=1, strict=True)
    target_span: SourceSpan
    depth_span: SourceSpan | None = None
    priority_span: SourceSpan | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if (
            digest(self.text) != self.raw_sha256
            or len(self.text.encode("utf-8")) != self.byte_count
        ):
            raise ValueError("Command source bytes changed")
        for span in (self.target_span, self.depth_span, self.priority_span):
            if span and not (0 <= span.start < span.end <= len(self.text)):
                raise ValueError("Invalid Unicode source span")
        return self


class StructuredCommandSource(Contract):
    kind: Literal["goal_revision_fields"] = "goal_revision_fields"
    fields: dict[str, Any]
    fields_hash: Hash
    target_path: Literal["topic", "technology"]
    depth_path: Literal["target_depth"] | None = None
    priority_path: Literal["learning_priority"] | None = None

    @model_validator(mode="after")
    def validate_source(self):
        allowed = {
            "topic",
            "technology",
            "target_depth",
            "learning_priority",
            "source_goal_version_id",
            "expected_goal_revision",
            "expected_revision",
            "idempotency_key",
            "authorization_ref",
        }
        if set(self.fields) - allowed or fingerprint(self.fields) != self.fields_hash:
            raise ValueError("Invalid submitted field source")
        for path in (self.target_path, self.depth_path, self.priority_path):
            if path and path not in self.fields:
                raise ValueError("Missing submitted source field")
        return self


CommandSource = Annotated[
    DialogueCommandSource | StructuredCommandSource, Field(discriminator="kind")
]


class ObjectiveMapping(Contract):
    status: Literal["mapped", "pending_mapping"]
    topic_key: str | None = Field(default=None, max_length=120)
    topic_version: int | None = Field(default=None, ge=1, strict=True)
    topic_object_id: str | None = Field(default=None, max_length=80)
    topic_object_hash: Hash | None = None

    @model_validator(mode="after")
    def consistent(self):
        values = (self.topic_key, self.topic_version, self.topic_object_id, self.topic_object_hash)
        if (self.status == "mapped" and any(v is None for v in values)) or (
            self.status == "pending_mapping" and any(v is not None for v in values)
        ):
            raise ValueError("Incomplete or invented taxonomy mapping")
        return self


class UserLearningObjective(Contract):
    origin: Literal["user_learning_addition"] = "user_learning_addition"
    objective_id: str = Field(pattern=r"^uobj-[0-9a-f]{24}$")
    technology: str = Field(min_length=1, max_length=120)
    target_depth: Depth | None = None
    depth_basis: Literal["explicit", "unspecified"] = "unspecified"
    learning_priority: Priority = "unspecified"
    mapping: ObjectiveMapping
    user_mastery_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def qualifiers(self):
        if (self.target_depth is None) != (self.depth_basis == "unspecified"):
            raise ValueError("Depth must remain explicitly sourced or unknown")
        if not self.technology.strip():
            raise ValueError("Empty technology")
        return self


class LegacyAddendumReference(Contract):
    addendum_id: Identifier
    manifest_hash: Hash


class GoalAmendmentManifestV2(Contract):
    contract_version: Literal["goal-addendum-v2"] = "goal-addendum-v2"
    origin: Literal["user_learning_addition"] = "user_learning_addition"
    source_goal_version_id: Identifier
    source_goal_object_hash: Hash
    source_jd_revision_id: Identifier | None
    parent_addendum_id: Identifier | None
    command_receipt_id: Identifier
    command_action_index: int = Field(ge=0, strict=True)
    command_request_hash: Hash
    command_source: CommandSource
    operation: Literal["add_learning_objective"] = "add_learning_objective"
    objective: UserLearningObjective
    inherited_legacy_addenda: list[LegacyAddendumReference] = Field(
        default_factory=list, max_length=30
    )
    user_mastery_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def source_matches(self):
        source, objective = self.command_source, self.objective
        if isinstance(source, DialogueCommandSource):
            text = source.text

            def quote(span):
                return text[span.start : span.end] if span else None

            target, depth, priority = (
                quote(source.target_span),
                quote(source.depth_span),
                quote(source.priority_span),
            )
            priorities = {
                "required": {"要求", "必须", "需要"},
                "preferred": {"优先", "最好", "希望"},
                "unspecified": {None},
            }
            if priority not in priorities[objective.learning_priority]:
                raise ValueError("Priority has no exact user source")
        else:
            target = source.fields[source.target_path]
            depth = source.fields[source.depth_path] if source.depth_path else None
            priority = (
                source.fields[source.priority_path] if source.priority_path else "unspecified"
            )
            if priority != objective.learning_priority:
                raise ValueError("Submitted priority changed")
        if target != objective.technology or depth != objective.target_depth:
            raise ValueError("Objective differs from exact user source")
        return self
