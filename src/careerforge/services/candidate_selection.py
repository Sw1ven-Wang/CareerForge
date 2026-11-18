"""Versioned, observation-bound candidate choice; relevance remains a model judgment.

Only server-observed candidate IDs are selectable. This selects material for
later independent analysis/review; it cannot publish facts or satisfy a JD.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final, Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from ..adapters.github import GitHubProvider
from ..contracts import Contract, ReadReference
from ..errors import DomainError
from ..models import ResearchNode, SearchObservation, SourceRead
from ..util import fingerprint
from .dependencies import check_closure
from .runtime import leased
from .security import namespace_access
from .sources import SourceService

CANDIDATE_SELECTION_VERSION: Final = "candidate-selection-1.6.6"

SELECTION_SYSTEM = """为下一步独立源码研究选择候选，而不是宣称仓库实现或 JD 已满足。
只选择 candidate_catalog 给出的 candidate_id，不生成仓库名、URL、来源或需求 ID。
逐个候选给出 select/defer/reject 和 reason_code，selected_candidate_ids 按希望研究的顺序排列。
每个 select 必须引用该候选自己的实际 observation_id。只依据元数据、README 或路径线索时，
相关需求映射必须标 metadata_hypothesis，不能说已实现；不确定性应保留。
source_observed 只可引用该候选 source_reads 的 read_id 和精确连续 quote，仍不是独立审核结论。
需求映射只能引用 frozen_requirements 中的 requirement_id，保留 OR、可选、否定与深度。
明确 not_required 的项不能成为正向选择目标。无法判断关联可以 defer/reject 或弃选全部，
不要为了凑满 max_selected 数量选择。不能把岗位目标变成用户能力，requirements_satisfied 必须 false。
target_topics 只是检索目标，不是候选已经具备的机制或强制分类。输出严格 JSON。"""


class CandidateRequirementLink(Contract):
    requirement_id: str = Field(min_length=1, max_length=180)
    basis: Literal["metadata_hypothesis", "source_observed"]
    observation_ids: list[str] = Field(default_factory=list, max_length=20)
    source_quotes: list[ReadReference] = Field(default_factory=list, max_length=8)


class CandidateDecision(Contract):
    candidate_id: str = Field(min_length=1, max_length=180)
    decision: Literal["select", "defer", "reject"]
    reason_code: Literal[
        "possible_requirement_match",
        "question_relevance_hypothesis",
        "source_review_needed",
        "insufficient_observation",
        "outside_goal",
        "duplicate_candidate",
        "budget_deferred",
    ]
    observation_ids: list[str] = Field(default_factory=list, max_length=20)
    possible_related_requirements: list[CandidateRequirementLink] = Field(
        default_factory=list, max_length=30
    )
    uncertainties: list[
        Literal[
            "implementation_not_verified",
            "requirement_relevance_unverified",
            "source_scope_partial",
            "runtime_not_observed",
            "metadata_only",
            "version_not_read",
        ]
    ] = Field(default_factory=list, max_length=6)


class CandidateDecisionOutput(Contract):
    contract_version: Literal["candidate-selection-1.6.6"] = CANDIDATE_SELECTION_VERSION
    selected_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    decisions: list[CandidateDecision] = Field(default_factory=list, max_length=100)
    requirements_satisfied: Literal[False] = False

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [row.candidate_id for row in self.decisions]
        if len(ids) != len(set(ids)) or len(self.selected_candidate_ids) != len(
            set(self.selected_candidate_ids)
        ):
            raise ValueError("Candidate IDs cannot repeat")
        return self


def validate_decision(
    output: CandidateDecisionOutput, catalog: dict[str, Any], limit: int
) -> dict[str, Any]:
    candidates = {row["candidate_id"]: row for row in catalog["candidates"]}
    requirements = {row["requirement_id"]: row for row in catalog["requirements"]}
    decisions = {row.candidate_id: row for row in output.decisions}
    selected = set(output.selected_candidate_ids)
    if (
        len(selected) > limit
        or set(decisions) != set(candidates)
        or selected != {row.candidate_id for row in output.decisions if row.decision == "select"}
        or not selected <= set(candidates)
    ):
        raise DomainError(
            "CANDIDATE_SELECTION_SCOPE", "候选选择必须完整对应实际观察，并遵守数量与决策范围。", 422
        )
    for decision in output.decisions:
        candidate = candidates[decision.candidate_id]
        observations = {row["observation_id"] for row in candidate["observations"]}
        reads = {row["read_id"]: row for row in candidate["source_reads"]}
        if not set(decision.observation_ids) <= observations:
            raise DomainError("CANDIDATE_OBSERVATION", "候选引用了其他项目或不存在的观察。", 422)
        if decision.decision == "select" and (
            not decision.observation_ids
            or decision.reason_code
            not in {
                "possible_requirement_match",
                "question_relevance_hypothesis",
                "source_review_needed",
            }
        ):
            raise DomainError("CANDIDATE_OBSERVATION", "缺少候选自身观察时不能选择。", 422)
        if (
            decision.reason_code == "possible_requirement_match"
            and not decision.possible_related_requirements
        ):
            raise DomainError("CANDIDATE_REQUIREMENT", "需求关联理由必须引用固定需求。", 422)
        seen_requirements = set()
        for link in decision.possible_related_requirements:
            requirement = requirements.get(link.requirement_id)
            if (
                requirement is None
                or requirement.get("modality") == "not_required"
                or link.requirement_id in seen_requirements
            ):
                raise DomainError(
                    "CANDIDATE_REQUIREMENT", "候选不能新增、重复或正向使用已排除的需求。", 422
                )
            seen_requirements.add(link.requirement_id)
            if not link.observation_ids or not set(link.observation_ids) <= set(
                decision.observation_ids
            ):
                raise DomainError(
                    "CANDIDATE_OBSERVATION", "每条需求映射必须引用该候选已声明的观察。", 422
                )
            if (link.basis == "source_observed") != bool(link.source_quotes):
                raise DomainError(
                    "CANDIDATE_SOURCE", "静态源码关联必须有真实读取，元数据不能冒充源码。", 422
                )
            for quote in link.source_quotes:
                read = reads.get(quote.read_id)
                if read is None or read["content"].count(quote.quote) != 1:
                    raise DomainError(
                        "CANDIDATE_SOURCE", "候选源码引用必须来自本次该候选的实际读取。", 422
                    )
    return {
        **output.model_dump(mode="json"),
        "decisions": [
            {
                **row.model_dump(mode="json"),
                "verified_implementation": False,
                "relevance_is_model_judgment": True,
                "selection_rank": output.selected_candidate_ids.index(row.candidate_id) + 1
                if row.candidate_id in selected
                else None,
            }
            for row in output.decisions
        ],
        "semantic_quality_verified": False,
        "requires_independent_source_review": True,
        "user_mastery_unchanged": True,
    }


def _identity(candidate: dict[str, Any]) -> str:
    value = candidate.get("repository_id", candidate.get("name"))
    if value is None:
        raise DomainError("CANDIDATE_IDENTITY", "研究观察缺少候选身份。", 409)
    return str(value)


def make_catalog(runtime, run, research_result, requirements):
    """Use durable observations and current ACLs, never IDs supplied by a model."""
    candidates = research_result.get("candidates", [])
    if len(candidates) > 100:
        raise DomainError("CANDIDATE_CATALOG_LIMIT", "候选目录超过一次有界选择的上限。", 413)
    dependencies = set(research_result.get("access_dependencies", []))
    with runtime.db.read() as db:
        observations = list(
            db.scalars(
                select(SearchObservation)
                .join(ResearchNode)
                .where(ResearchNode.run_id == run.id)
                .order_by(SearchObservation.id)
            )
        )
        source_reads = list(
            db.scalars(
                select(SourceRead)
                .where(
                    SourceRead.run_id == run.id,
                    SourceRead.principal_id == run.principal_id,
                    SourceRead.role == "research",
                )
                .order_by(SourceRead.id)
            )
        )
        entries, originals = [], {}
        for candidate in candidates:
            repository = GitHubProvider.name(candidate["url"])
            if candidate.get("name") and candidate["name"].lower() != repository.lower():
                raise DomainError("CANDIDATE_IDENTITY", "候选名称与实际观察的地址不一致。", 409)
            identifier = (
                "candidate-"
                + fingerprint(
                    {
                        "identity": _identity(candidate),
                        "url": candidate["url"],
                        "source_id": candidate.get("source_id"),
                        "commit": candidate.get("commit"),
                    }
                )[:24]
            )
            matches = []
            for observation in observations:
                actual = [
                    item
                    for item in observation.payload.get("candidates", [])
                    if _identity(item) == _identity(candidate)
                    and item.get("url") == candidate["url"]
                    and item.get("source_id") == candidate.get("source_id")
                    and item.get("commit") == candidate.get("commit")
                ]
                if actual:
                    dependencies.update(observation.payload.get("access_dependencies", []))
                    matches.append(
                        {
                            "observation_id": observation.id,
                            "node_id": observation.node_id,
                            "query_hash": observation.query_hash,
                            "candidate_observations": deepcopy(actual),
                            "source_mode": observation.payload.get(
                                "source_mode",
                                "offline_fixture"
                                if observation.payload.get("synthetic")
                                else "local_snapshot"
                                if observation.payload.get("local_only")
                                else "provider_observation",
                            ),
                            "source_leads": [
                                lead
                                for lead in observation.payload.get("leads", [])
                                if (
                                    lead.get("source_id") == candidate.get("source_id")
                                    if run.input_manifest.get("jd_research_batch") is None
                                    else (
                                        bool(candidate.get("source_id"))
                                        and lead.get("source_id") == candidate["source_id"]
                                    )
                                    or str(lead.get("repository", "")).casefold()
                                    == repository.casefold()
                                )
                            ],
                        }
                    )
            reads = []
            if candidate.get("source_id"):
                dependencies.add(candidate["source_id"])
                source = SourceService.get(db, run.principal_id, candidate["source_id"])
                if (
                    source.repository_key.lower() != repository.lower()
                    or source.commit_sha != candidate.get("commit")
                ):
                    raise DomainError("CANDIDATE_SOURCE", "候选固定来源与仓库或版本不一致。", 409)
                for row in source_reads:
                    if row.source_id == source.id:
                        SourceService.validated_read(db, run.principal_id, row.id)
                        reads.append(
                            {
                                "read_id": row.id,
                                "source_id": source.id,
                                "commit": source.commit_sha,
                                "path": row.path,
                                "start_line": row.first_line,
                                "end_line": row.last_line,
                                "content": row.text,
                                "quote_hash": row.quote_hash,
                            }
                        )
            entries.append(
                {
                    "candidate_id": identifier,
                    "repository": repository,
                    "url": candidate["url"],
                    "observations": matches,
                    "source_reads": reads,
                    "verified_implementation": False,
                }
            )
            if identifier in originals:
                raise DomainError("CANDIDATE_IDENTITY", "候选目录含重复身份。", 409)
            originals[identifier] = deepcopy(candidate)
        closure = check_closure(db, run.principal_id, sorted(dependencies))
        if (
            namespace_access(db, run.principal_id, run.namespace_id).kind == "shared_public"
            and not closure.public_only
        ):
            raise DomainError("PRIVATE_TO_PUBLIC", "公共候选选择不能读取私人观察来源。", 403)
    return (
        {"candidates": entries, "requirements": deepcopy(requirements)},
        originals,
        sorted(dependencies),
    )


def bounded_catalog_view(entries):
    """Metadata excerpts for selection; full observations and source reads remain intact."""
    result = deepcopy(entries)
    for candidate in result:
        for observation in candidate["observations"]:
            original_hash = fingerprint(observation)
            observation["candidate_observations"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "repository_id",
                        "name",
                        "url",
                        "description",
                        "license",
                        "language",
                        "stars",
                        "fork",
                        "archived",
                        "verified_implementation",
                        "source_id",
                        "commit",
                    }
                }
                for item in observation["candidate_observations"]
            ]
            for item in observation["candidate_observations"]:
                if isinstance(item.get("description"), str) and len(item["description"]) > 800:
                    item["description"] = item["description"][:800]
                    item["description_is_excerpt"] = True
            for lead in observation["source_leads"]:
                if isinstance(lead.get("excerpt"), str) and len(lead["excerpt"]) > 800:
                    lead["excerpt"] = lead["excerpt"][:800]
                    lead["excerpt_truncated"] = True
            observation["full_observation_hash"] = original_hash
            observation["metadata_only"] = True
    return result


async def select_candidates(
    runtime,
    gateway,
    run,
    epoch,
    worker,
    research_result,
    requirements,
    limit,
    dependencies,
    data_classes,
):
    catalog, originals, catalog_dependencies = make_catalog(
        runtime, run, research_result, requirements
    )
    dependency_ids = sorted(set(dependencies) | set(catalog_dependencies))
    classes, dependency_ids = gateway.policy_egress_inputs(
        run.id,
        "planner",
        data_classes=data_classes,
        access_dependencies=dependency_ids,
    )
    old = runtime.stage(run.id, "candidate_selection")
    if old is None:
        if not catalog["candidates"]:
            output = CandidateDecisionOutput()
        else:
            result = await gateway.call(
                run.id,
                epoch,
                worker,
                "candidate.selection",
                "planner",
                SELECTION_SYSTEM,
                {
                    "original_question": run.input_manifest["question"],
                    "candidate_catalog": bounded_catalog_view(catalog["candidates"])
                    if run.input_manifest.get("jd_research_batch") is not None
                    else catalog["candidates"],
                    "frozen_requirements": catalog["requirements"],
                    "max_selected": limit,
                },
                CandidateDecisionOutput,
                data_classes=classes,
                access_dependencies=dependency_ids,
            )
            output = CandidateDecisionOutput.model_validate(result.structured)
        validated = validate_decision(output, catalog, limit)
        old = {
            **validated,
            "catalog_hash": fingerprint(catalog),
            "proposal": output.model_dump(mode="json"),
            "access_dependencies": dependency_ids,
            "selected_candidates": [
                originals[identifier] for identifier in output.selected_candidate_ids
            ],
        }
        with runtime.db.transaction() as db:
            active = leased(db, run.id, epoch, worker)
            check_closure(db, run.principal_id, dependency_ids)
            runtime.save_stage_in(db, active, "candidate_selection", old)
    output = CandidateDecisionOutput.model_validate(old.get("proposal"))
    validated = validate_decision(output, catalog, limit)
    expected = {
        **validated,
        "catalog_hash": fingerprint(catalog),
        "proposal": output.model_dump(mode="json"),
        "access_dependencies": dependency_ids,
        "selected_candidates": [
            originals[identifier] for identifier in output.selected_candidate_ids
        ],
    }
    if old != expected:
        raise DomainError("CANDIDATE_SELECTION_HASH", "固定候选选择与原始观察或需求不一致。", 409)
    return deepcopy(old)
