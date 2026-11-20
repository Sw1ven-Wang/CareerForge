"""Frozen goal identity and conservative, claim-anchored task sufficiency.

This module never publishes knowledge or authorizes an external model call.
Callers must reauthorize goal snapshots and publication packs in their read/write
transaction. The pure evaluator consumes only anchors resolved from those packs,
not model-supplied publication IDs. A correct small fact is useful but does not
by itself establish coverage of the original research task.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import ExtractedRequirement
from ..contracts_jd_v166 import REQUIREMENT_VERSION, ProvenanceRequirement
from ..contracts_research_v163 import (
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_COVERAGE_VERSION,
    ProvenanceResearchRequirement,
    ResearchCoverageDraft,
    ResearchDimension,
    ResearchEvidenceAssessment,
    ResearchRequirement,
    ResearchRequirementAssessment,
    ResearchTaskContract,
)
from ..errors import DomainError
from ..models import AgentRun, CareerGoal, GoalVersion, ObjectRow, StageArtifact
from ..util import digest, fingerprint
from .dependencies import check_closure, depend, register, require_object
from .jd import JDService, goal_topic_dependencies, validate_requirement_set
from .jd_topics import JD_TOPIC_MAPPING
from .security import namespace_access, private_namespace

_KEY = re.compile(r"^[a-z0-9_.-]{1,100}$")
_REQUIREMENT_FIELDS = ("technology", "quote", "depth", "depth_basis", "keywords", "topic_key")
DEPTH_RANK = {"了解": 0, "熟悉": 1, "掌握": 2, "精通": 3}
# These are kinds of explanation, not counts of files, claims, or model calls.
# The independent reviewer must assess what the claims actually establish.
DEPTH_DIMENSIONS: dict[str, tuple[ResearchDimension, ...]] = {
    "了解": ("purpose",),
    "熟悉": ("purpose", "implementation", "boundaries"),
    "掌握": ("purpose", "implementation", "boundaries", "tradeoffs"),
    "精通": ("purpose", "implementation", "boundaries", "tradeoffs"),
}
PROJECT_DIMENSIONS = ("project_structure", "module_responsibilities", "call_flow")


def requirement_identity(requirement: dict[str, Any]) -> str:
    """Stable goal-local identity shared by research and learning presentation."""
    key = (
        requirement.get("requirement_id")
        if requirement.get("requirement_contract_version") == REQUIREMENT_VERSION
        else requirement.get("key")
    )
    if isinstance(key, str) and _KEY.fullmatch(key):
        return key
    fields = {key: requirement[key] for key in _REQUIREMENT_FIELDS if key in requirement}
    if isinstance(fields.get("keywords"), list) and all(
        isinstance(item, str) for item in fields["keywords"]
    ):
        fields["keywords"] = sorted(set(fields["keywords"]))
    return "requirement-" + fingerprint(fields)[:24]


def iter_requirement_items(payload: Any) -> list[dict[str, Any]]:
    """Read actual wrappers, old lists, and keyed legacy records without guessing.

    Partial legacy dictionaries remain quotable goal data for learning. Strict
    research normalization below marks them unresolved rather than inventing a
    target depth. Strings and wrapper metadata never become requirements.
    """
    if isinstance(payload, dict) and "requirements" in payload:
        payload = payload["requirements"]
    if isinstance(payload, list):
        return [
            dict(item)
            for item in payload
            if isinstance(item, dict) and ("technology" in item or "quote" in item)
        ]
    if isinstance(payload, dict):
        if "technology" in payload or "quote" in payload:
            return [dict(payload)]
        return [
            {**value, **({"key": key} if "key" not in value and _KEY.fullmatch(key) else {})}
            for key, value in payload.items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and ("technology" in value or "quote" in value)
        ]
    return []


def normalize_requirements(
    payload: Any, *, source_text: str | None = None
) -> tuple[list[ResearchRequirement | ProvenanceResearchRequirement], list[str]]:
    """Preserve IDs/quotes/depth, retaining invalid or ambiguous items as gaps."""
    unresolved: list[str] = []
    if isinstance(payload, dict):
        raw_unresolved = payload.get("unresolved", payload.get("unresolved_requirement_ids", []))
        if isinstance(raw_unresolved, list):
            unresolved.extend(str(item)[:180] for item in raw_unresolved)
        elif raw_unresolved:
            unresolved.append("unresolved_requirements_contract_invalid")
        if source_text is not None and payload.get("source_hash") not in (
            None,
            fingerprint(source_text),
        ):
            raise DomainError("GOAL_HASH", "目标要求的原文指纹不一致。")
    items = iter_requirement_items(payload)
    # Atomic JD goals may contain more independent capabilities than a single
    # research task. Preserve the whole goal; legacy/task contracts stay at 30.
    marker = payload.get("jd_granularity_contract") if isinstance(payload, dict) else None
    if marker is not None:
        from .jd_atomic import ATOMIC_JD_CONTRACT

        if marker != ATOMIC_JD_CONTRACT:
            raise DomainError("REQUIREMENT_CONTRACT", "未知 JD 粒度合同。", 422)
    limit = 80 if marker is not None else 30
    if len(items) > limit:
        raise DomainError("RESEARCH_REQUIREMENTS", "目标要求超过冻结合同上限。", 422)
    if payload and not items:
        unresolved.append("requirements_not_extracted")
    container = (
        payload.get("requirements")
        if isinstance(payload, dict) and "requirements" in payload
        else payload
    )
    if isinstance(container, list) and len(container) != len(items):
        unresolved.append("requirements_contract_invalid")
    normalized: dict[str, ResearchRequirement | ProvenanceResearchRequirement] = {}
    contents: dict[str, str] = {}
    ambiguous: set[str] = set()
    for item in items:
        identifier = requirement_identity(item)
        if item.get("requirement_contract_version") is not None:
            if item["requirement_contract_version"] != REQUIREMENT_VERSION:
                raise DomainError("REQUIREMENT_CONTRACT", "未知需求来源合同，不能降级丢失限定符。")
            raw_provenance = {
                key: value for key, value in item.items() if key != "required_dimensions"
            }
            try:
                source_requirement = ProvenanceRequirement.model_validate(raw_provenance)
                if source_text is not None and (
                    source_text[source_requirement.span_start : source_requirement.span_end]
                    != source_requirement.quote
                    or digest(source_text) != source_requirement.raw_sha256
                ):
                    raise ValueError("Versioned requirement lost its exact source")
                content_hash = fingerprint(raw_provenance)
                if identifier in contents and contents[identifier] != content_hash:
                    raise ValueError("Versioned requirement identity is ambiguous")
                contents[identifier] = content_hash
                normalized[identifier] = ProvenanceResearchRequirement(
                    **source_requirement.model_dump(mode="json"),
                    required_dimensions=list(
                        DEPTH_DIMENSIONS.get(source_requirement.depth or "", ())
                    ),
                )
            except (ValidationError, ValueError) as exc:
                raise DomainError("REQUIREMENT_CONTRACT", "固定需求位置或限定符无效。") from exc
            continue
        raw = {key: item[key] for key in _REQUIREMENT_FIELDS if key in item}
        raw["key"] = identifier
        semantic = dict(raw)
        if isinstance(semantic.get("keywords"), list) and all(
            isinstance(word, str) for word in semantic["keywords"]
        ):
            semantic["keywords"] = sorted(set(semantic["keywords"]))
        content_hash = fingerprint(semantic)
        if identifier in contents and contents[identifier] != content_hash:
            ambiguous.add(identifier)
            unresolved.append(identifier)
            normalized.pop(identifier, None)
            continue
        contents[identifier] = content_hash
        if identifier in ambiguous or identifier in normalized:
            continue
        try:
            requirement = ExtractedRequirement.model_validate(raw)
            if source_text is not None and (
                source_text.count(requirement.quote) != 1
                or (
                    requirement.depth_basis == "explicit"
                    and requirement.depth not in requirement.quote
                )
            ):
                raise ValueError("Requirement quote or explicit depth is not source-anchored")
            normalized[identifier] = ResearchRequirement(
                **requirement.model_dump(mode="json"),
                requirement_id=identifier,
                required_dimensions=list(DEPTH_DIMENSIONS[requirement.depth]),
            )
        except (ValidationError, ValueError):
            unresolved.append(identifier)
    return list(normalized.values()), list(dict.fromkeys(unresolved))


def freeze_research_task(
    question: str,
    *,
    scope: str = "project",
    required_dimensions: list[str] | None = None,
    goal_version_id: str | None = None,
    requirements: Any = None,
    unresolved_requirement_ids: list[str] | None = None,
) -> ResearchTaskContract:
    if isinstance(requirements, list):
        requirements = [
            item.model_dump(mode="json")
            if isinstance(item, (ResearchRequirement, ProvenanceResearchRequirement))
            else item
            for item in requirements
        ]
    normalized, unresolved = normalize_requirements(requirements)
    unresolved = list(dict.fromkeys([*unresolved, *(unresolved_requirement_ids or [])]))
    if goal_version_id and not normalized and not unresolved:
        unresolved = ["requirements_not_extracted"]
    defaults = (
        PROJECT_DIMENSIONS
        if scope == "project"
        else ("purpose", "boundaries")
        if scope == "concept"
        else ()
    )
    # Explicit extra dimensions cannot erase the normal project's structure,
    # responsibilities and execution flow. Choose focused scope when warranted.
    dimensions = list(dict.fromkeys([*defaults, *(required_dimensions or [])]))
    return ResearchTaskContract.model_validate(
        {
            "original_question": question,
            "scope": scope,
            "goal_version_id": goal_version_id,
            "requirements": [item.model_dump(mode="json") for item in normalized],
            "unresolved_requirement_ids": unresolved,
            "required_dimensions": dimensions,
        }
    )


def _goal_projection(db: Session, actor: str, identifier: str) -> tuple[GoalVersion, CareerGoal]:
    own_namespace = private_namespace(db, actor)
    version = db.get(GoalVersion, identifier)
    goal = db.get(CareerGoal, version.goal_id) if version else None
    if (
        version is None
        or goal is None
        or goal.principal_id != actor
        or goal.namespace_id != own_namespace.id
    ):
        raise DomainError("NOT_FOUND", "学习目标不存在。", 404)
    namespace_access(db, actor, goal.namespace_id)
    if version.origin_contract not in {"run-goal-v1", "command-goal-v1"}:
        raise DomainError("GOAL_AMENDMENT", "未知目标来源合同。", 409)
    if version.content_hash != fingerprint(version.source_text):
        raise DomainError("GOAL_HASH", "学习目标原文已损坏。")
    if (
        isinstance(version.requirements, dict)
        and version.requirements.get("contract_version") == REQUIREMENT_VERSION
    ):
        document = JDService.read_in(db, actor, version.requirements.get("jd_revision_id"))
        if document["raw_text"] != version.source_text:
            raise DomainError("GOAL_HASH", "目标版本不是指定岗位原文。")
        validate_requirement_set(version.requirements, document)
        check_closure(db, actor, goal_topic_dependencies(db, actor, version.requirements))
    normalize_requirements(version.requirements, source_text=version.source_text)
    return version, goal


def _goal_payload(version: GoalVersion) -> dict[str, Any]:
    return {
        "goal_id": version.goal_id,
        "version": version.version,
        "source_text": version.source_text,
        "source_hash": version.content_hash,
        "requirements": version.requirements,
    }


def ensure_goal_snapshot(db: Session, actor: str, goal_version_id: str) -> dict[str, Any]:
    """Snapshot an owned legacy goal only in an explicitly authorized new build.

    Never use this on a read path or to repair a mismatching existing object.
    The caller owns the new-build transaction and current-run lease as needed.
    """
    version, goal = _goal_projection(db, actor, goal_version_id)
    namespace_access(db, actor, goal.namespace_id, True)
    if version.origin_contract == "command-goal-v1":
        return read_goal_version(db, actor, goal_version_id)
    if db.get(ObjectRow, goal_version_id) is None:
        register(db, actor, goal.namespace_id, "goal_version", _goal_payload(version), version.id)
        if (
            isinstance(version.requirements, dict)
            and version.requirements.get("contract_version") == REQUIREMENT_VERSION
        ):
            depend(
                db,
                actor,
                goal_version_id,
                version.requirements["jd_revision_id"],
                "derived_from_jd",
            )
            for dependency in goal_topic_dependencies(db, actor, version.requirements):
                target = require_object(db, actor, dependency)
                relation = (
                    "maps_requirement_to_facet"
                    if target.kind == "facet_schema"
                    else "maps_requirement_to_topic"
                )
                depend(db, actor, goal_version_id, dependency, relation)
    return read_goal_version(db, actor, goal_version_id)


def read_goal_version(db: Session, actor: str, goal_version_id: str) -> dict[str, Any]:
    """Recheck owner, privacy generation, ACL, immutable object and typed row."""
    version, goal = _goal_projection(db, actor, goal_version_id)
    obj = require_object(db, actor, goal_version_id, "goal_version")
    check_closure(db, actor, [goal_version_id])
    extension = {}
    expected_payload = _goal_payload(version)
    if version.origin_contract == "command-goal-v1":
        from .goal_amendments import command_goal_payload, validate_goal_amendment_in

        extension = validate_goal_amendment_in(db, actor, goal_version_id)
        expected_payload = command_goal_payload(db, version)
    if obj.namespace_id != goal.namespace_id or obj.payload != expected_payload:
        raise DomainError("GOAL_HASH", "目标版本与固定要求不一致。")
    requirements, unresolved = normalize_requirements(
        version.requirements, source_text=version.source_text
    )
    if not requirements and not unresolved:
        unresolved = ["requirements_not_extracted"]
    links = (
        {"requirement_topic_links": version.requirements["requirement_topic_links"]}
        if isinstance(version.requirements, dict)
        and "requirement_topic_links" in version.requirements
        else {}
    )
    if (
        isinstance(version.requirements, dict)
        and version.requirements.get("topic_link_contract") == JD_TOPIC_MAPPING
    ):
        links["topic_link_contract"] = JD_TOPIC_MAPPING
    return {
        **links,
        **extension,
        "goal_version_id": version.id,
        "namespace_id": goal.namespace_id,
        "source_text": version.source_text,
        "content_hash": version.content_hash,
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "unresolved_requirement_ids": unresolved,
    }


def validate_goal_snapshot(db: Session, actor: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("goal_version_id"), str):
        raise DomainError("GOAL_HASH", "缺少固定目标版本。")
    current = read_goal_version(db, actor, snapshot["goal_version_id"])
    if current != snapshot:
        raise DomainError("GOAL_HASH", "学习目标已改变，不能沿用旧研究上下文。")
    return current


def validate_research_run(db: Session, actor: str, run: AgentRun) -> None:
    """Fence new research work after suspension, revocation or goal mutation.

    Called within the runtime's current-lease transaction, before budget spending
    or derived writes. It does not acquire a lease recursively and it never
    upgrades a historical run. A fresh JD run has no goal until extraction saves
    its research_context stage; descendants inherit that stage's exact contract.
    """
    from .research_query import query_contract_context

    manifest = run.input_manifest
    scope_root = db.get(AgentRun, run.parent_id) if run.parent_id else run
    scoped = manifest.get("goal_build_scope") is not None or bool(
        scope_root and scope_root.input_manifest.get("goal_build_scope") is not None
    )
    scope_goal_id = scope_root.input_manifest.get("goal_version_id") if scope_root else None
    scope_goal = db.get(GoalVersion, scope_goal_id) if scope_goal_id else None
    command_goal = bool(
        (scope_goal and scope_goal.origin_contract == "command-goal-v1")
        or (
            scope_root
            and (scope_root.input_manifest.get("goal_snapshot") or {}).get("origin_contract")
            == "command-goal-v1"
        )
    )
    if (scoped or command_goal) and manifest.get(
        "research_contract_version"
    ) != RESEARCH_CONTRACT_VERSION:
        raise DomainError("GOAL_BUILD_SCOPE", "用户目标范围不能丢失研究合同标记。", 409)
    query_contract_context(manifest)  # Unknown new query contracts never silently downgrade.
    declared = manifest.get("research_contract_version")
    task_value = manifest.get("research_task")
    if declared is None and isinstance(task_value, dict):
        declared = task_value.get("contract_version")
    if declared is None:
        return
    if declared != RESEARCH_CONTRACT_VERSION:
        raise DomainError("RESEARCH_CONTRACT", "研究合同版本不可恢复。")
    if run.principal_id != actor or fingerprint(manifest) != run.input_hash:
        raise DomainError("RESEARCH_CONTRACT", "研究任务身份或冻结输入已改变。")
    namespace = namespace_access(db, actor, run.namespace_id, True)
    snapshot = manifest.get("goal_snapshot")
    root = db.get(AgentRun, run.parent_id) if run.parent_id else run
    if root is None or root.principal_id != actor or root.namespace_id != run.namespace_id:
        raise DomainError("RESEARCH_CONTRACT", "研究父任务身份或范围不一致。")
    root_manifest = root.input_manifest
    research_batch = root_manifest.get("jd_research_batch")
    goal_scope = root_manifest.get("goal_build_scope")
    if scoped:
        from .goal_build_scope import validate_scope_manifest_in

        if (
            goal_scope is None
            or manifest.get("goal_build_scope") != goal_scope
            or fingerprint(root_manifest) != root.input_hash
        ):
            raise DomainError("GOAL_BUILD_SCOPE", "用户目标范围不能在子任务中丢失或改变。", 409)
        validate_scope_manifest_in(db, actor, goal_scope, root_manifest, root=True)
        if run.parent_id:
            validate_scope_manifest_in(db, actor, goal_scope, manifest)
    elif command_goal:
        raise DomainError("GOAL_BUILD_SCOPE", "新增目标必须保留显式构建范围。", 409)
    imported_jd = None
    if root_manifest.get("jd_revision_id"):
        imported_jd = JDService.read_in(db, actor, root_manifest["jd_revision_id"])
        if (
            imported_jd != root_manifest.get("jd_import_snapshot")
            or imported_jd["namespace_id"] != root.namespace_id
        ):
            raise DomainError("JD_CONTRACT", "任务的固定岗位原文或私人范围已变化。")
    context = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == root.id, StageArtifact.stage_key == "research_context"
        )
    )
    if context is not None:
        if context.payload.get("jd_research_batch") != research_batch:
            raise DomainError("JD_RESEARCH_BATCH", "研究分批血缘不一致。", 409)
        if (
            context.input_hash != root.input_hash
            or fingerprint(root.input_manifest) != root.input_hash
        ):
            raise DomainError("RESEARCH_CONTRACT", "研究上下文与父任务不一致。")
        if scoped and context.payload.get("goal_build_scope") != goal_scope:
            raise DomainError("GOAL_BUILD_SCOPE", "持久研究阶段遗漏或改变了目标范围。", 409)
        parent_task = context.payload.get("research_task")
        if task_value is not None and parent_task != task_value:
            raise DomainError("RESEARCH_CONTRACT", "研究子任务不能改变固定目标。")
        task_value = parent_task
        context_snapshot = context.payload.get("goal_snapshot")
        if snapshot is not None and context_snapshot != snapshot:
            raise DomainError("GOAL_HASH", "研究子任务不能替换目标快照。")
        snapshot = context_snapshot
    if manifest.get("goal_version_id") and snapshot is None:
        raise DomainError("GOAL_HASH", "研究目标缺少已授权的固定快照。")
    if snapshot is not None:
        if namespace.kind != "private" or namespace.owner_id != actor:
            raise DomainError("PRIVATE_TO_PUBLIC", "目标研究只能保存在本人私人域。", 403)
        current = validate_goal_snapshot(db, actor, snapshot)
        if manifest.get("goal_version_id") not in (None, current["goal_version_id"]):
            raise DomainError("GOAL_HASH", "研究任务与指定目标版本不一致。")
        if root_manifest.get("jd_text") or imported_jd:
            version = db.get(GoalVersion, current["goal_version_id"])
            if (
                current["source_text"]
                != (imported_jd["raw_text"] if imported_jd else root_manifest["jd_text"])
                or version is None
                or version.run_id != root.id
            ):
                raise DomainError("GOAL_HASH", "研究目标不是本次原文的固定提取结果。")
        elif root_manifest.get("goal_version_id") != current["goal_version_id"]:
            raise DomainError("GOAL_HASH", "研究上下文不能添加或替换私人目标。")
    if task_value is not None:
        try:
            task = ResearchTaskContract.model_validate(task_value)
        except ValidationError as exc:
            raise DomainError("RESEARCH_CONTRACT", "固定研究合同无效。") from exc
        if task.goal_version_id is not None:
            if snapshot is None or task.goal_version_id != snapshot["goal_version_id"]:
                raise DomainError("GOAL_HASH", "研究要求缺少匹配的目标快照。")
            if scoped:
                from .goal_build_scope import scope_requirements

                if goal_scope is None:
                    raise DomainError("GOAL_HASH", "研究要求缺少固定目标范围。")
                expected_requirements = scope_requirements(snapshot, goal_scope)
                expected_unresolved = goal_scope["unresolved_requirement_ids"]
            else:
                if research_batch is not None:
                    from .jd_research_batch import validate_batch

                    expected_requirements = validate_batch(snapshot, research_batch)
                else:
                    expected_requirements = snapshot["requirements"]
                expected_unresolved = snapshot["unresolved_requirement_ids"]
            if [
                item.model_dump(mode="json") for item in task.requirements
            ] != expected_requirements or task.unresolved_requirement_ids != expected_unresolved:
                raise DomainError("GOAL_HASH", "研究要求与固定目标不一致。")
        elif snapshot is not None:
            raise DomainError("GOAL_HASH", "研究合同遗漏了私人目标。")
        question = root_manifest.get("question")
        if not isinstance(question, str) or not question:
            raise DomainError("RESEARCH_CONTRACT", "研究任务缺少原始问题。")
        scope = root_manifest.get("research_scope") or "project"
        if not root_manifest.get("research_scope") and root_manifest.get("source_id"):
            from .sources import SourceService

            if SourceService.get(db, actor, root_manifest["source_id"]).origin_kind == "concept":
                scope = "concept"
        if scoped:
            from .goal_build_scope import scoped_research_task_in

            expected = scoped_research_task_in(db, actor, goal_scope, snapshot)
        else:
            expected = freeze_research_task(
                question,
                scope=scope,
                required_dimensions=root_manifest.get("required_dimensions"),
                goal_version_id=snapshot["goal_version_id"] if snapshot else None,
                requirements=expected_requirements if snapshot else None,
                unresolved_requirement_ids=snapshot["unresolved_requirement_ids"]
                if snapshot
                else None,
            )
        if task != expected:
            raise DomainError("RESEARCH_CONTRACT", "研究上下文改变了原始问题或冻结研究范围。")


def _assessment_map(coverage: ResearchCoverageDraft) -> dict[str, ResearchEvidenceAssessment]:
    return {
        "question": coverage.question,
        **{"dimension:" + item.dimension: item for item in coverage.dimensions},
        **{"requirement:" + item.requirement_id: item for item in coverage.requirements},
    }


def evaluate_task_coverage(
    task: ResearchTaskContract | dict[str, Any],
    analysis_coverages: list[dict[str, Any]],
    reviewer_coverages: list[dict[str, Any]],
    published_anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute sufficiency only from matched, independently reviewed publications.

    Coverage records contain source_id and coverage; reviewer records additionally
    contain their own unit_version_id. A separately budgeted root composition
    audit instead receives SERVER-added unit_version_ids equal to the source's
    exact published unit set. Published anchors are SERVER-resolved rows
    with source_id, subject_key, claim_key, claim_id, publication_id,
    unit_version_id, statement, conditions and kind. A model-generated row is
    never authority. Conditions and claim kind are kept verbatim in every result;
    dropping a qualifier would turn scoped knowledge into an unsupported fact.

    No number of shallow claims fills a missing dimension or target depth. One
    matched analysis/reviewer assessment must establish each requirement at its
    target depth. A project's not_applicable never deletes a global JD requirement.
    """
    task = ResearchTaskContract.model_validate(task)
    invalid_records: list[str] = []
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    anchor_fields = (
        "source_id",
        "subject_key",
        "claim_key",
        "claim_id",
        "publication_id",
        "unit_version_id",
        "statement",
    )
    for raw in published_anchors:
        if not isinstance(raw, dict) or any(
            not isinstance(raw.get(key), str) or not raw[key] for key in anchor_fields
        ):
            invalid_records.append("published_anchor_contract_invalid")
            continue
        if (
            not isinstance(raw.get("conditions"), list)
            or len(raw["conditions"]) > 8
            or any(not isinstance(condition, str) for condition in raw["conditions"])
            or not isinstance(raw.get("kind"), str)
            or raw.get("kind")
            not in {
                "source_fact",
                "documented_intent",
                "analytical_inference",
                "runtime_observation",
            }
        ):
            invalid_records.append("published_anchor_scope_invalid")
            continue
        row = {
            **{key: raw[key] for key in anchor_fields},
            "conditions": list(raw["conditions"]),
            "kind": raw["kind"],
        }
        key = (row["source_id"], row["subject_key"], row["claim_key"])
        if key in index and index[key] != row:
            ambiguous.add(key)
            invalid_records.append("ambiguous_candidate_claim")
        else:
            index[key] = row
    for key in ambiguous:
        index.pop(key, None)

    def records(values: list[dict[str, Any]], reviewer: bool) -> list[dict[str, Any]]:
        result = []
        for value in values:
            try:
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("source_id"), str)
                    or not value["source_id"]
                ):
                    raise ValueError("Missing source scope")
                if reviewer:
                    unit_id = value.get("unit_version_id")
                    unit_ids = value.get("unit_version_ids")
                    if unit_ids is not None:
                        expected_ids = {
                            row["unit_version_id"]
                            for row in index.values()
                            if row["source_id"] == value["source_id"]
                        }
                        if (
                            unit_id is not None
                            or not isinstance(unit_ids, list)
                            or not unit_ids
                            or any(not isinstance(item, str) or not item for item in unit_ids)
                            or len(unit_ids) != len(set(unit_ids))
                            or set(unit_ids) != expected_ids
                        ):
                            raise ValueError(
                                "Composition audit scope is not the exact published set"
                            )
                    elif not isinstance(unit_id, str) or not unit_id:
                        raise ValueError("Missing independent candidate scope")
                coverage = ResearchCoverageDraft.model_validate(
                    value.get("coverage", value.get("task_coverage"))
                )
                result.append(
                    {**value, "coverage": coverage, "assessments": _assessment_map(coverage)}
                )
            except (ValidationError, ValueError):
                invalid_records.append(
                    "review_coverage_invalid" if reviewer else "analysis_coverage_invalid"
                )
        return result

    analyses = records(analysis_coverages, False)
    reviews = records(reviewer_coverages, True)
    unresolved_questions = list(
        dict.fromkeys(
            question
            for record in [*analyses, *reviews]
            for question in [
                *record["coverage"].unresolved_questions,
                *(
                    question
                    for assessment in record["coverage"].assessments()
                    for question in assessment.unresolved_questions
                ),
            ]
        )
    )

    def resolve(
        record: dict[str, Any], assessment: ResearchEvidenceAssessment
    ) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[str]]:
        resolved: dict[tuple[str, str, str], dict[str, Any]] = {}
        reasons: list[str] = []
        if not assessment.anchors:
            reasons.append("missing_claim_anchors")
        for anchor in assessment.anchors:
            for claim_key in anchor.claim_keys:
                key = (record["source_id"], anchor.subject_key, claim_key)
                row = index.get(key)
                if row is None:
                    reasons.append("claim_not_published_or_ambiguous")
                elif (
                    record.get("unit_version_id")
                    and row["unit_version_id"] != record["unit_version_id"]
                ):
                    reasons.append("claim_outside_reviewed_candidate")
                elif (
                    record.get("unit_version_ids")
                    and row["unit_version_id"] not in record["unit_version_ids"]
                ):
                    reasons.append("claim_outside_reviewed_candidate")
                else:
                    resolved[key] = row
        return resolved, list(dict.fromkeys(reasons))

    def match(
        analysis: dict[str, Any],
        a: ResearchEvidenceAssessment,
        review: dict[str, Any],
        r: ResearchEvidenceAssessment,
    ) -> tuple[bool, list[dict[str, Any]], list[str]]:
        aa, ar = resolve(analysis, a)
        ra, rr = resolve(review, r)
        reasons = [*ar, *rr]
        if a.status != "known" or r.status != "known":
            reasons.append("assessment_not_known")
        if a.unresolved_questions or r.unresolved_questions:
            reasons.append("unresolved_assessment_questions")
        if not aa or not set(aa) <= set(ra):
            reasons.append("independent_assessment_does_not_cover_analysis_anchors")
        return not reasons, list(ra.values()), list(dict.fromkeys(reasons))

    def summarize(
        key: str, requirement: ResearchRequirement | ProvenanceResearchRequirement | None = None
    ) -> dict[str, Any]:
        reasons: list[str] = []
        scoped = []
        evidence: dict[str, dict[str, Any]] = {}
        completed_evidence: dict[str, dict[str, Any]] = {}
        complete = False
        demonstrated = None
        for review in reviews:
            r = review["assessments"].get(key)
            if r is None:
                continue
            rows, invalid = resolve(review, r)
            scoped.append(
                {
                    "source_id": review["source_id"],
                    **(
                        {"unit_version_ids": review["unit_version_ids"]}
                        if "unit_version_ids" in review
                        else {"unit_version_id": review["unit_version_id"]}
                    ),
                    "status": r.status if not invalid else "unknown",
                    "reported_status": r.status,
                    "reason_codes": invalid,
                    "unresolved_questions": r.unresolved_questions,
                }
            )
            if r.status in {"known", "partial"} and not invalid:
                evidence.update({row["claim_id"]: row for row in rows.values()})
            for analysis in analyses:
                if analysis["source_id"] != review["source_id"]:
                    continue
                a = analysis["assessments"].get(key)
                if a is None:
                    continue
                good, matched, why = match(analysis, a, review, r)
                if requirement is not None:
                    if not isinstance(a, ResearchRequirementAssessment) or not isinstance(
                        r, ResearchRequirementAssessment
                    ):
                        good = False
                        why.append("requirement_assessment_missing")
                    else:
                        target = DEPTH_RANK.get(requirement.depth or "", 99)
                        if requirement.depth is None:
                            good = False
                            why.append("unknown_target_depth")
                        if (
                            min(
                                DEPTH_RANK.get(a.demonstrated_depth or "", -1),
                                DEPTH_RANK.get(r.demonstrated_depth or "", -1),
                            )
                            < target
                        ):
                            good = False
                            why.append("insufficient_depth")
                        ad = {item.dimension: item for item in a.dimensions}
                        rd = {item.dimension: item for item in r.dimensions}
                        for dimension in requirement.required_dimensions:
                            if dimension not in ad or dimension not in rd:
                                good = False
                                why.append("missing_depth_dimension:" + dimension)
                            else:
                                established, extra, failures = match(
                                    analysis, ad[dimension], review, rd[dimension]
                                )
                                matched.extend(extra)
                                if not established:
                                    good = False
                                    why.extend(
                                        "depth_dimension:" + dimension + ":" + reason
                                        for reason in failures
                                    )
                if good:
                    complete = True
                    completed_evidence.update({row["claim_id"]: row for row in matched})
                    if requirement:
                        # The requested target is proven; do not extrapolate a
                        # higher proficiency level from a model's self-label.
                        demonstrated = requirement.depth
                else:
                    reasons.extend(why)
        if not complete and not reasons:
            reasons.append("missing_independent_task_assessment")
        if (
            requirement is not None
            and not complete
            and any(item["reported_status"] == "not_applicable" for item in scoped)
        ):
            reasons.append("project_not_applicable_does_not_satisfy_goal")
        return {
            "status": "complete" if complete else "partial" if evidence else "unknown",
            "complete": complete,
            "reason_codes": [] if complete else list(dict.fromkeys(reasons)),
            "evidence": list((completed_evidence if complete else evidence).values()),
            "scoped_assessments": scoped,
            **({"demonstrated_depth": demonstrated} if requirement else {}),
        }

    question = summarize("question")
    dimensions = [
        {"dimension": dimension, **summarize("dimension:" + dimension)}
        for dimension in task.required_dimensions
    ]
    requirements = [
        {
            "requirement_id": requirement.requirement_id,
            "quote": requirement.quote,
            "target_depth": requirement.depth,
            "required_dimensions": requirement.required_dimensions,
            **summarize("requirement:" + requirement.requirement_id, requirement),
            **(
                {
                    "modality": requirement.modality,
                    "group_operator": requirement.group_operator,
                    "group_id": requirement.group_id,
                    "group_path": [
                        group.model_dump(mode="json") for group in requirement.group_path
                    ],
                    "paragraph_id": requirement.paragraph_id,
                    "span_start": requirement.span_start,
                    "span_end": requirement.span_end,
                    "jd_revision_id": requirement.jd_revision_id,
                    "raw_sha256": requirement.raw_sha256,
                    "user_mastery_unchanged": True,
                }
                if isinstance(requirement, ProvenanceResearchRequirement)
                else {}
            ),
        }
        for requirement in task.requirements
    ]
    obligation_results, group_results = requirement_obligations(task.requirements, requirements)
    all_items: Iterable[dict[str, Any]] = [question, *dimensions, *obligation_results]
    complete = (
        all(item["complete"] for item in all_items)
        and not task.unresolved_requirement_ids
        and not unresolved_questions
        and not invalid_records
    )
    return {
        "contract_version": RESEARCH_COVERAGE_VERSION,
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "task_contract_hash": fingerprint(task.model_dump(mode="json")),
        "goal_version_id": task.goal_version_id,
        "status": "complete" if complete else "partial" if index else "unknown",
        "complete": complete,
        "question": question,
        "dimensions": dimensions,
        "requirements": requirements,
        "unresolved_requirement_ids": task.unresolved_requirement_ids,
        "unresolved_questions": unresolved_questions,
        "reason_codes": list(dict.fromkeys(invalid_records)),
        "publication_ids": sorted({row["publication_id"] for row in index.values()}),
        "fact_support_is_not_task_sufficiency": True,
        **(
            {"requirement_groups": group_results, "user_mastery_unchanged": True}
            if any(isinstance(row, ProvenanceResearchRequirement) for row in task.requirements)
            else {}
        ),
    }


def requirement_obligations(requirements, results):
    """Evaluate preserved logical groups, never treating alternatives as conjunctions.

    A not-required or optional item remains visible but does not become a hard
    completion gate. Exemption is not evidence of competence or implementation.
    """
    result_map = {row["requirement_id"]: row for row in results}
    leaves = {row.requirement_id: row for row in requirements}
    groups, parents = {}, {}
    for requirement in requirements:
        if not isinstance(requirement, ProvenanceResearchRequirement):
            continue
        for group in requirement.group_path:
            payload = group.model_dump(mode="json")
            if group.group_id in groups and groups[group.group_id] != payload:
                raise DomainError("REQUIREMENT_GROUP", "固定需求逻辑组存在冲突。")
            groups[group.group_id] = payload
    for group_id, group in groups.items():
        for child in group["member_ids"]:
            if child in parents and parents[child] != group_id:
                raise DomainError("REQUIREMENT_GROUP", "固定需求被多个逻辑组重复引用。")
            if child not in groups and child not in leaves:
                raise DomainError("REQUIREMENT_GROUP", "固定需求逻辑组缺少成员。")
            parents[child] = group_id
    group_results, visiting = {}, set()

    def evaluate(identifier):
        if identifier in visiting:
            raise DomainError("REQUIREMENT_GROUP", "固定需求逻辑组形成循环。")
        if identifier in leaves:
            req, result = leaves[identifier], result_map[identifier]
            if not isinstance(req, ProvenanceResearchRequirement):
                return {"complete": result["complete"], "required_for_completion": True}
            required = req.modality in {"required", "unspecified"}
            result["required_for_completion"] = required
            if req.modality == "not_required":
                result.update(
                    complete=False,
                    status="not_required",
                    demonstrated_depth=None,
                    reason_codes=["explicitly_not_required"],
                )
            return {
                "complete": bool(result["complete"] and req.modality == "required"),
                "required_for_completion": required,
            }
        visiting.add(identifier)
        group = groups[identifier]
        children = [evaluate(child) for child in group["member_ids"]]
        visiting.remove(identifier)
        active = [child for child in children if child["required_for_completion"]]
        if group["operator"] == "NOT" and active:
            raise DomainError("REQUIREMENT_GROUP", "NOT 不能把正向必需成员变成满足。")
        if group["operator"] == "OR" and active and len(active) != len(children):
            raise DomainError("REQUIREMENT_GROUP", "OR 不能以非必需选项代替硬性要求。")
        result = {
            **group,
            "required_for_completion": bool(active),
            "complete": (
                any(child["complete"] for child in active)
                if group["operator"] == "OR"
                else all(child["complete"] for child in active)
            )
            if active
            else False,
            "user_mastery_unchanged": True,
        }
        group_results[identifier] = result
        return result

    roots = [identifier for identifier in [*leaves, *groups] if identifier not in parents]
    obligations = [evaluate(identifier) for identifier in roots]
    if set(parents) - set(leaves) - set(groups) or len(group_results) != len(groups):
        raise DomainError("REQUIREMENT_GROUP", "固定需求逻辑组不能形成孤立循环。")
    return [row for row in obligations if row["required_for_completion"]], list(
        group_results.values()
    )
