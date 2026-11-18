"""Authenticated read-only C05 projection of existing evidence, with no inference.

Static observations never become semantic facts or model-read receipts. Semantic
sections resolve only the independent composition review's actual dimension
anchors into this build's published, version-bound, exact approved claims.
"""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts_project_analysis_v166 import (
    PROJECT_ANALYSIS_VERSION,
    MechanismAnalysisOutput,
    ProjectAnalysisProjection,
    ReviewedStructureSection,
)
from ..contracts_research_v163 import ResearchSufficiencyOutput, ResearchTaskContract
from ..contracts_scoped_review_v166 import MECHANISM_PUBLICATION_CONTRACT
from ..errors import DomainError
from ..models import AgentRun, Claim, ModelReceipt, Publication, ReviewDecision, StageArtifact
from ..util import digest, fingerprint
from .analysis_coordination import NAVIGATION_CONTRACT, NAVIGATION_FIELD
from .build_execution import EVIDENCE_RETRY_CONTRACT
from .dependencies import check_closure, require_object
from .mechanism_discovery import DISCOVERY_STAGE, MECHANISM_PHASES
from .research_coverage import validate_research_run
from .security import namespace_access
from .sources import SourceService
from .taxonomy import schema_view


def _invalid() -> DomainError:
    return DomainError(
        "PROJECT_ANALYSIS_BINDING", "项目结构投影与固定任务、审核或来源不一致。", 409
    )


def _stage(db: Session, run: AgentRun, key: str) -> dict[str, Any] | None:
    row = db.scalar(
        select(StageArtifact).where(StageArtifact.run_id == run.id, StageArtifact.stage_key == key)
    )
    if row is None:
        return None
    if row.input_hash != run.input_hash or not isinstance(row.payload, dict):
        raise _invalid()
    return row.payload


def _ids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise _invalid()
    return value


def _unread(total: int, spans: list[tuple[int, int]]) -> list[dict[str, int]]:
    """Complement of observed line windows, not a semantic coverage estimate."""
    cursor, gaps = 1, []
    for start, end in sorted(spans):
        if start > cursor:
            gaps.append({"start_line": cursor, "end_line": start - 1})
        cursor = max(cursor, end + 1)
    if cursor <= total:
        gaps.append({"start_line": cursor, "end_line": total})
    return gaps


def project_section(
    dimension: str,
    records: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    source_ids: list[str],
    requirement_id: str | None = None,
) -> dict[str, Any]:
    """Pure resolver. Callers supply validated publications and reviewer records.

    Deliberately no keyword classification, statement rewriting, module naming,
    endpoint extraction or transitive graph completion occurs here.
    """
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    for fact in facts:
        key = (fact["source_id"], fact["subject_key"], fact["claim_key"])
        if key in index and index[key] != fact:
            ambiguous.add(key)
        index[key] = fact
    for key in ambiguous:
        index.pop(key)
    items: dict[str, dict[str, Any]] = {}
    scopes: list[dict[str, Any]] = []
    reasons: list[str] = []
    for record in records:
        coverage = record["coverage"]
        if requirement_id is None:
            candidates = coverage.get("dimensions", [])
        else:
            requirement = next(
                (
                    item
                    for item in coverage.get("requirements", [])
                    if item["requirement_id"] == requirement_id
                ),
                None,
            )
            candidates = (
                [requirement]
                if dimension == "requirement" and requirement
                else requirement.get("dimensions", [])
                if requirement
                else []
            )
        assessment = next(
            (
                item
                for item in candidates
                if dimension == "requirement" or item["dimension"] == dimension
            ),
            None,
        )
        if assessment is None:
            continue
        resolved: dict[str, dict[str, Any]] = {}
        invalid = []
        for anchor in assessment.get("anchors", []):
            for key in anchor["claim_keys"]:
                matched = index.get((record["source_id"], anchor["subject_key"], key))
                if matched is None:
                    invalid.append("claim_not_published_or_ambiguous")
                elif matched["unit_version_id"] not in record["unit_version_ids"]:
                    invalid.append("claim_outside_reviewed_version")
                else:
                    resolved[matched["claim_id"]] = matched
        if not assessment.get("anchors"):
            invalid.append("missing_independent_dimension_anchors")
        supported = assessment["status"] in {"known", "partial"}
        if not supported:
            invalid.append("independent_assessment_not_supported")
            resolved = {}
        effective = (
            "known"
            if resolved
            and not invalid
            and assessment["status"] == "known"
            and not assessment.get("unresolved_questions")
            else "partial"
            if resolved
            else "unknown"
        )
        if effective != "known":
            invalid.append("independent_dimension_incomplete")
        items.update(resolved)
        scopes.append(
            {
                "source_id": record["source_id"],
                "unit_version_ids": record["unit_version_ids"],
                "reported_status": assessment["status"],
                "status": effective,
                "reviewer_explanation": assessment["explanation"],
                "unresolved_questions": assessment.get("unresolved_questions", []),
                "claim_ids": list(resolved),
                "reason_codes": list(dict.fromkeys(invalid)),
                "assessment_receipt": record["assessment_receipt"],
            }
        )
        reasons.extend(invalid)
    absent = set(source_ids) - {item["source_id"] for item in scopes}
    if absent or not scopes:
        reasons.append("missing_independent_dimension_assessment")
    known = bool(scopes) and not absent and all(item["status"] == "known" for item in scopes)
    return ReviewedStructureSection(
        dimension=dimension,
        status="known" if known else "partial" if items else "unknown",
        items=list(items.values()),
        scoped_assessments=scopes,
        reason_codes=list(dict.fromkeys(reasons)),
    ).model_dump(mode="json")


class ProjectAnalysisService:
    def __init__(self, knowledge, sources):
        self.knowledge, self.sources = knowledge, sources

    def project_in(
        self, db: Session, actor: str, run: AgentRun, result: dict[str, Any]
    ) -> dict[str, Any]:
        # This view is owner-only even when its fact publications are public.
        # In particular a grant to a source/knowledge namespace is not JD access.
        if run.principal_id != actor or run.workflow != "knowledge_build":
            raise DomainError("NOT_FOUND", "知识构建不存在。", 404)
        namespace_access(db, actor, run.namespace_id)
        if fingerprint(run.input_manifest) != run.input_hash:
            raise _invalid()
        marker = run.input_manifest.get("project_analysis_contract_version")
        if marker not in (None, PROJECT_ANALYSIS_VERSION):
            raise DomainError("PROJECT_ANALYSIS_VERSION", "项目结构合同版本不可恢复。", 409)
        validate_research_run(db, actor, run)
        context = _stage(db, run, "research_context") or {}
        raw_task = context.get("research_task", run.input_manifest.get("research_task"))
        task = ResearchTaskContract.model_validate(raw_task) if raw_task else None
        if result.get("research_task") is not None and result["research_task"] != raw_task:
            raise _invalid()
        sources_stage = _stage(db, run, "sources") or {}
        source_ids = _ids(
            sources_stage.get(
                "source_ids",
                [run.input_manifest["source_id"]] if run.input_manifest.get("source_id") else [],
            )
        )
        if run.input_manifest.get("source_id") and source_ids != [run.input_manifest["source_id"]]:
            raise _invalid()
        sources = {identifier: self.sources.get(db, actor, identifier) for identifier in source_ids}
        children = self._children(db, actor, run, source_ids)
        publication_ids = _ids(result.get("publication_ids", []))
        check_closure(db, actor, [*source_ids, *publication_ids])
        facts, units = self._facts(db, actor, run, publication_ids, source_ids, children)
        reads = self._reads(db, actor, children, source_ids)
        records, review_gaps, receipt = self._coverage(db, run, publication_ids, units)
        goal_id = task.goal_version_id if task else None
        if goal_id and namespace_access(db, actor, run.namespace_id).kind != "private":
            raise DomainError("PRIVATE_TO_PUBLIC", "私人目标不能进入公共投影。", 403)
        if result.get("goal_version_id") not in (None, goal_id):
            raise _invalid()
        structure = project_section("project_structure", records, facts, source_ids=source_ids)
        modules = project_section("module_responsibilities", records, facts, source_ids=source_ids)
        calls = project_section("call_flow", records, facts, source_ids=source_ids)
        boundaries = project_section("boundaries", records, facts, source_ids=source_ids)
        source_views, symbols, unread = [], [], []
        index_count = 0
        for source_id, source in sources.items():
            inventory, indexed = self.sources.static_index(source)
            index_count += len(indexed)
            own_reads = [row for row in reads if row["source_id"] == source_id]
            paths = []
            for file in inventory["files"]:
                spans = [row for row in own_reads if row["path"] == file["path"]]
                missing = {
                    role: _unread(
                        file["lines"],
                        [
                            (row["start_line"], row["end_line"])
                            for row in spans
                            if row["role"] == role
                        ],
                    )
                    for role in ("analysis", "reviewer")
                }
                paths.append(
                    {
                        **file,
                        "verified_read_ids": [row["read_id"] for row in spans],
                        "unread_line_ranges": missing,
                    }
                )
                if any(missing.values()):
                    unread.append(
                        {
                            "source_id": source_id,
                            "commit": source.commit_sha,
                            "path": file["path"],
                            "unread_line_ranges": missing,
                        }
                    )
            for symbol in indexed:
                overlap = [
                    row["read_id"]
                    for row in own_reads
                    if row["path"] == symbol["path"]
                    and row["start_line"] <= symbol["end_line"]
                    and row["end_line"] >= symbol["start_line"]
                ]
                if overlap:
                    symbols.append(
                        {
                            **symbol,
                            "repository": source.repository_key,
                            "commit": source.commit_sha,
                            "file_hash": digest(source.files[symbol["path"]]),
                            "overlapping_read_ids": overlap,
                            "semantic_importance_assessed": False,
                        }
                    )
            source_views.append(
                {
                    "source_id": source_id,
                    "repository": source.repository_key,
                    "commit": source.commit_sha,
                    "scope_hash": source.scope_hash,
                    "origin_kind": source.origin_kind,
                    "license_id": source.license_id,
                    "observation_kind": "static_index_from_imported_snapshot",
                    "files": paths,
                    "file_count": inventory["file_count"],
                    "entry_candidates": inventory["entry_candidates"],
                    "entry_candidates_are_verified_entrypoints": False,
                    "verified_reads": own_reads,
                    "outside_imported_snapshot": "unknown_not_enumerated",
                    "full_repository_read": False,
                    "executed": False,
                }
            )
        requirement_links = []
        for requirement in task.requirements if task else []:
            row = project_section(
                "requirement",
                records,
                facts,
                source_ids=source_ids,
                requirement_id=requirement.requirement_id,
            )
            requirement_links.append(
                {
                    "requirement": requirement.model_dump(mode="json"),
                    "evidence_link_status": row["status"],
                    "evidence": row["items"],
                    "scoped_assessments": row["scoped_assessments"],
                    "reason_codes": row["reason_codes"],
                    "dimensions": [
                        project_section(
                            dimension,
                            records,
                            facts,
                            source_ids=source_ids,
                            requirement_id=requirement.requirement_id,
                        )
                        for dimension in requirement.required_dimensions
                    ],
                    "target_depth_satisfaction_claimed": False,
                    "evidence_link_is_not_depth_sufficiency": True,
                    "user_mastery_unchanged": True,
                }
            )
        questions = list(result.get("unanswered_questions", []))
        for record in records:
            coverage = record["coverage"]
            questions.extend(coverage.get("unresolved_questions", []))
            assessments = [
                coverage["question"],
                *coverage.get("dimensions", []),
                *coverage.get("requirements", []),
                *(
                    dim
                    for req in coverage.get("requirements", [])
                    for dim in req.get("dimensions", [])
                ),
            ]
            questions.extend(
                question
                for item in assessments
                for question in item.get("unresolved_questions", [])
            )
        value = ProjectAnalysisProjection(
            projection={
                "run_id": run.id,
                "run_input_hash": run.input_hash,
                "admission_contract": marker,
                "read_only": True,
                "model_fact_transformation": "exact_approved_text_and_conditions",
                "additional_model_calls": 0,
                "scope": "imported_snapshot_only",
                "full_repository_understanding": False,
                "runtime_execution_observed": False,
                "goal_version_id": goal_id,
                "other_goals_status": "not_evaluated_requires_separate_goal_build",
                "publication_ids": publication_ids,
                "independent_coverage_receipt": receipt,
                "reason_codes": review_gaps,
            },
            repo_map={
                "observation_kind": "static_index_not_semantic_coverage",
                "sources": source_views,
                "reviewed_structure": structure,
                "unread_scopes": unread,
                "cross_source_composition": False,
            },
            modules=ReviewedStructureSection.model_validate(modules),
            call_chain_edges=ReviewedStructureSection.model_validate(calls),
            exception_boundaries=ReviewedStructureSection.model_validate(boundaries),
            key_symbols={
                "observation_kind": "static_index_not_semantic_coverage",
                "selection": "indexed_symbols_overlapping_verified_read_windows",
                "index_symbol_count": index_count,
                "selected_symbol_count": len(symbols),
                "items": symbols,
                "read_overlap_is_not_full_symbol_read": True,
                "symbol_presence_is_not_implementation_coverage": True,
            },
            knowledge_units=units,
            jd_mechanism_evidence_links={
                "goal_version_id": goal_id,
                "coverage_scope": "single_frozen_goal_only",
                "other_goals_status": "not_evaluated_requires_separate_goal_build",
                "requirements": requirement_links,
                "unresolved_requirement_ids": task.unresolved_requirement_ids if task else [],
                "user_mastery_unchanged": True,
            },
            open_questions=list(dict.fromkeys(questions)),
        ).model_dump(mode="json")
        discovery = (
            _stage(db, run, DISCOVERY_STAGE)
            if run.input_manifest.get("mechanism_discovery_contract")
            else None
        )
        directed = (
            discovery is not None
            and run.input_manifest.get(NAVIGATION_FIELD) == NAVIGATION_CONTRACT
        )
        if discovery is not None or (
            run.input_manifest.get("mechanism_publication_contract")
            == MECHANISM_PUBLICATION_CONTRACT
        ):
            targets = (
                discovery["targets"]
                if discovery is not None
                else run.input_manifest["mechanism_edge_targets"]
            )
            reviewed = [edge for unit in units for edge in unit.get("mechanism_edges", [])]
            value["mechanism_cards"] = [
                {
                    "source_id": source_id,
                    "commit": source.commit_sha,
                    "edges": [edge for edge in reviewed if edge["source_id"] == source_id],
                    "required_edges": [
                        {
                            **target,
                            "status": "reviewed"
                            if any(
                                edge["edge_key"] == target["key"] and edge["source_id"] == source_id
                                for edge in reviewed
                            )
                            else "unknown",
                        }
                        for target in (() if directed else targets)
                    ],
                    "unread_file_ranges": [row for row in unread if row["source_id"] == source_id],
                    "observed_execution": False,
                    "complete_mechanism": False,
                }
                for source_id, source in sources.items()
            ]
            value["learning_coverage_manifest"] = self._learning_coverage(
                db, actor, run, task, units, value["mechanism_cards"], modules, boundaries
            )
            if discovery is not None:
                connected = "connection_contract" in discovery
                facets_by_phase = {
                    **({"entry": "registration"} if connected else {}),
                    "validation": "argument_validation",
                    "dispatch": "dispatch",
                    "result": "result_feedback",
                    "boundary": "failure_boundary",
                }
                for card in value["mechanism_cards"]:
                    card["phase_evidence"] = {}
                    for phase in discovery.get("phase_order", MECHANISM_PHASES):
                        edge_ids = [
                            edge["edge_id"]
                            for edge in card["edges"]
                            if discovery["phases"].get(edge["edge_key"]) == phase
                        ]
                        facet_ids = [
                            facet["id"]
                            for unit in units
                            if unit["source_id"] == card["source_id"]
                            for facet in unit["facets"]
                            if facet["value_status"] == "known"
                            and facet["facet_key"] == facets_by_phase.get(phase)
                            and (not connected or facet["topic_key"] == "agent.tool_calling")
                        ]
                        card["phase_evidence"][phase] = {
                            "status": "reviewed" if edge_ids or facet_ids else "unknown",
                            "edge_ids": edge_ids,
                            "facet_ids": facet_ids,
                        }
                    card["scope"] = "bounded_static_mechanism"
                    if "phase_order" in discovery:
                        edges = card["edges"]
                        card["reviewed_edge_connections"] = [
                            [first["edge_id"], second["edge_id"]]
                            for first in edges
                            for second in edges
                            if first["edge_id"] != second["edge_id"]
                            and all(
                                first["target"]["callee"][key] == second["target"]["caller"][key]
                                for key in ("path", "symbol")
                            )
                        ]
                        if connected:
                            self._project_phase_connections(card, discovery, units)
                        card["unverified_phases"] = [
                            phase
                            for phase, state in card["phase_evidence"].items()
                            if state["status"] == "unknown"
                        ]
            if directed:
                retry = _stage(db, run, "evidence_retry_plan") or {}
                mechanism_path = result.get("task_coverage", {}).get("mechanism_path", {})
                for card in value["mechanism_cards"]:
                    candidate_rows = [
                        *targets,
                        *retry.get("alternate_edge_targets", []),
                    ]
                    card["candidate_edges"] = self._navigation_evidence(
                        db, actor, children, card, reads, candidate_rows
                    )
                    card["navigation_needs"] = retry.get("navigation_needs", [])
                    card["published_path"] = mechanism_path
                    card["complete_mechanism"] = bool(mechanism_path.get("complete"))
        elif run.input_manifest.get("evidence_retry_contract") == EVIDENCE_RETRY_CONTRACT:
            value["mechanism_cards"] = self._mechanism_cards(
                db, run, sources, children, facts, modules, boundaries
            )
        value["projection"]["content_hash"] = fingerprint(value)
        return value

    def _navigation_evidence(self, db, actor, children, card, reads, candidates):
        proposed, verified = set(), set()
        for suffix in ("", "_retry"):
            reviewer = children.get("review" + suffix)
            if reviewer is None:
                continue
            review_result = _stage(db, reviewer, "result") or {}
            for review_id in review_result.get("review_ids", []):
                row = db.get(ReviewDecision, review_id)
                if row is None or row.reviewer_run_id != reviewer.id:
                    raise _invalid()
                version = self.knowledge._validated_version(db, actor, row.unit_version_id)
                if version.source_id != card["source_id"]:
                    continue
                review = self.knowledge.validated_review(db, actor, review_id, version)
                proposed.update(
                    row["edge_key"]
                    for row in self.knowledge.mechanism_candidates(db, actor, reviewer, version)
                )
                verified.update(
                    row["edge_key"]
                    for row in review.report.get("scope_derivation", {}).get(
                        "retained_mechanism_edges", []
                    )
                )
        published = {row["edge_key"]: row["edge_id"] for row in card["edges"]}
        output = []
        for target in candidates:
            endpoint_reads = {
                side: [
                    row["read_id"]
                    for row in reads
                    if row["source_id"] == card["source_id"]
                    and row["path"] == target[side]["path"]
                    and row["start_line"] <= target[side]["start_line"]
                    and row["end_line"] >= target[side]["end_line"]
                ]
                for side in ("caller", "callee")
            }
            key = target["key"]
            output.append(
                {
                    **target,
                    "read": all(endpoint_reads.values()),
                    "read_ids": endpoint_reads,
                    "proposed": key in proposed or key in published,
                    "independently_verified": key in verified or key in published,
                    "published": key in published,
                    "published_edge_id": published.get(key),
                }
            )
        return output

    @staticmethod
    def _project_phase_connections(card, discovery, units):
        phases = discovery["phase_order"]
        edges = {edge["edge_id"]: edge for edge in card["edges"]}
        phase_by_edge = {
            identifier: discovery["phases"].get(edge["edge_key"])
            for identifier, edge in edges.items()
        }
        card["phase_connections"] = []
        for before, after in zip(phases, phases[1:], strict=False):
            pairs = [
                pair
                for pair in card["reviewed_edge_connections"]
                if [phase_by_edge[identifier] for identifier in pair] == [before, after]
            ]
            card["phase_connections"].append(
                {
                    "from_phase": before,
                    "to_phase": after,
                    "status": "reviewed_static_adjacency" if pairs else "unknown",
                    "edge_pairs": pairs,
                }
            )
        for phase in card["phase_evidence"].values():
            phase["source_navigation"] = []
            for unit in units:
                if unit["source_id"] != card["source_id"]:
                    continue
                claims = {
                    claim_id
                    for facet in unit["facets"]
                    if facet["id"] in phase["facet_ids"]
                    for claim_id in facet["claim_ids"]
                }
                claims.update(edges[identifier]["claim_id"] for identifier in phase["edge_ids"])
                anchors = [
                    {
                        "claim_id": fact["claim_id"],
                        "fact_atom_id": fact["fact_atom_id"],
                        "citations": [
                            {
                                key: citation[key]
                                for key in ("citation_id", "path", "start_line", "end_line")
                            }
                            for citation in fact["independent_citations"]
                        ],
                    }
                    for fact in unit["reviewed_claims"]
                    if fact["claim_id"] in claims
                ]
                if anchors:
                    phase["source_navigation"].append(
                        {
                            "publication_id": unit["publication_id"],
                            "topic_key": "agent.tool_calling",
                            "anchors": anchors,
                        }
                    )

    @staticmethod
    def _learning_coverage(db, actor, run, task, units, cards, modules, boundaries):
        topics = run.input_manifest.get("topic_keys", [])
        versions = run.input_manifest["taxonomy_versions"]
        scopes = []
        for card in cards:
            selected = [unit for unit in units if unit["source_id"] == card["source_id"]]
            dimensions = []
            for topic in topics:
                schema = schema_view(db, actor, topic, versions[topic])
                linked = any(
                    link["topic_key"] == topic and link["relation"] != "mentions"
                    for unit in selected
                    for link in unit["topic_links"]
                )
                for facet in schema["facets"]:
                    witnesses = [
                        value["id"]
                        for unit in selected
                        for value in unit["facets"]
                        if value["topic_key"] == topic
                        and value["facet_key"] == facet
                        and value["value_status"] == "known"
                    ]
                    dimensions.append(
                        {
                            "topic_key": topic,
                            "facet_key": facet,
                            "facet_ids": witnesses,
                            "status": "reviewed" if witnesses else "unknown",
                            "gap_reason": None
                            if witnesses
                            else "missing_facet"
                            if linked
                            else "missing_topic_link",
                        }
                    )
            scopes.append(
                {
                    "source_id": card["source_id"],
                    "commit": card["commit"],
                    "publication_ids": [unit["publication_id"] for unit in selected],
                    "reviewed_fact_count": sum(len(unit["facts"]) for unit in selected),
                    "required_edges": card["required_edges"],
                    "facet_coverage": dimensions,
                    "unread_file_ranges": card["unread_file_ranges"],
                    "next_read_targets": [
                        {
                            "edge_key": edge["key"],
                            "caller": edge["caller"],
                            "callee": edge["callee"],
                            "reason": "edge_not_independently_published",
                        }
                        for edge in card["required_edges"]
                        if edge["status"] == "unknown"
                    ],
                }
            )
        return {
            "contract": "scoped-learning-coverage-v1",
            "goal_version_id": task.goal_version_id if task else None,
            "required_dimensions": task.required_dimensions if task else [],
            "module_responsibilities": {
                "status": modules["status"],
                "reason_codes": modules["reason_codes"],
            },
            "exception_boundaries": {
                "status": boundaries["status"],
                "reason_codes": boundaries["reason_codes"],
            },
            "sources": scopes,
            "scope": "fixed_snapshot_and_reviewed_publications_only",
        }

    @staticmethod
    def _mechanism_cards(db, run, sources, children, facts, modules, boundaries):
        cards = []
        for source_id, source in sources.items():
            edges = {}
            for child in children.values():
                if child.workflow != "analysis" or child.status != "succeeded":
                    continue
                stage = _stage(db, child, "task_analysis:" + source_id)
                if not stage:
                    continue
                receipt = db.scalar(
                    select(ModelReceipt).where(
                        ModelReceipt.run_id == child.id,
                        ModelReceipt.stage_key == "analysis.final:" + source_id,
                    )
                )
                if (
                    receipt is None
                    or receipt.status != "completed"
                    or not receipt.response
                    or receipt.role != "analysis"
                    or receipt.payer_id != run.principal_id
                    or receipt.profile_version != child.input_manifest.get("model_profile")
                ):
                    raise _invalid()
                output = MechanismAnalysisOutput.model_validate(receipt.response["structured"])
                index = {
                    (fact["subject_key"], fact["claim_key"]): fact
                    for fact in facts
                    if fact["unit_version_id"] in stage["unit_version_ids"]
                }
                for edge in output.mechanism_edges:
                    fact = index.get((edge.subject_key, edge.claim_key))
                    if fact is None or edge.from_path == edge.to_path:
                        continue
                    endpoints = ((edge.from_path, edge.from_symbol), (edge.to_path, edge.to_symbol))
                    if not all(
                        path in source.files
                        and re.search(
                            r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])", fact["statement"]
                        )
                        and all(
                            any(ref["path"] == path and symbol in ref["quote"] for ref in fact[key])
                            for key in ("citations", "independent_citations")
                        )
                        for path, symbol in endpoints
                    ):
                        continue
                    edges[fact["fact_atom_id"]] = {**edge.model_dump(mode="json"), **fact}
            cards.append(
                {
                    "source_id": source_id,
                    "commit": source.commit_sha,
                    "coverage": "reviewed_cross_file_edges"
                    if edges
                    else "no_reviewed_cross_file_edges",
                    "edges": list(edges.values()),
                    "module_claim_ids": [
                        item["claim_id"]
                        for item in modules["items"]
                        if item["source_id"] == source_id
                    ],
                    "exception_claim_ids": [
                        item["claim_id"]
                        for item in boundaries["items"]
                        if item["source_id"] == source_id
                    ],
                    "limitations": {
                        "observed_execution": False,
                        "complete_mechanism": False,
                        "scope": "only_cross_file_edges_whose_original_claim_and_both_file_quotes_passed_review",
                    },
                }
            )
        return cards

    @staticmethod
    def _children(db, actor, run, source_ids):
        children = {}
        for name, workflow, key in (
            ("analysis", "analysis", "analysis_child"),
            ("review", "review", "review_child"),
            ("analysis_retry", "analysis", "analysis_retry_child"),
            ("review_retry", "review", "review_retry_child"),
        ):
            stage = _stage(db, run, key)
            if stage is None:
                continue
            child = db.get(AgentRun, stage.get("run_id"))
            if (
                child is None
                or child.parent_id != run.id
                or child.principal_id != actor
                or child.namespace_id != run.namespace_id
                or child.funding_id != run.funding_id
                or child.workflow != workflow
                or fingerprint(child.input_manifest) != child.input_hash
            ):
                raise _invalid()
            validate_research_run(db, actor, child)
            if workflow == "analysis" and child.input_manifest.get("source_ids") != source_ids:
                raise _invalid()
            if name.endswith("_retry"):
                retry = _stage(db, run, "evidence_retry_plan")
                if not retry or child.input_manifest.get("retry_lineage") != retry:
                    raise _invalid()
            children[name] = child
        return children

    def _facts(self, db, actor, run, publication_ids, source_ids, children):
        facts, units = [], []
        for identifier in publication_ids:
            pack = self.knowledge.publication_pack(db, actor, identifier)
            publication = db.get(Publication, identifier)
            version = self.knowledge._validated_version(db, actor, pack["unit_version_id"])
            review = self.knowledge.validated_review(db, actor, publication.review_id, version)
            child_pair = next(
                (
                    (children["analysis" + suffix], children["review" + suffix])
                    for suffix in ("", "_retry")
                    if "analysis" + suffix in children
                    and "review" + suffix in children
                    and version.analysis_run_id == children["analysis" + suffix].id
                    and review.reviewer_run_id == children["review" + suffix].id
                ),
                None,
            )
            if (
                version.source_id not in source_ids
                or child_pair is None
                or version.id not in child_pair[1].input_manifest.get("unit_version_ids", [])
                or pack["namespace_id"] != run.namespace_id
            ):
                raise _invalid()
            draft = require_object(db, actor, version.id, "knowledge_unit_version").payload["draft"]
            rendered = {row["fact_atom_id"]: row for row in pack["facts"]}
            decisions = {row["claim_id"]: row for row in review.report["claim_decisions"]}
            unit_facts = []
            for original, element in zip(draft["claims"], version.render_ast, strict=True):
                if (
                    element["fact_atom_id"] not in rendered
                    and pack.get("publication_scope", {}).get("status") == "partial"
                ):
                    continue
                fact = rendered[element["fact_atom_id"]]
                claim = db.get(Claim, fact["claim_id"])
                cp = require_object(db, actor, claim.id, "claim").payload
                decision = decisions[claim.id]
                if (
                    claim.statement != original["statement"]
                    or claim.conditions != original["conditions"]
                    or claim.kind != original["kind"]
                    or decision["outcome"] != "supported_within_scope"
                    or not decision.get("independent_evidence_ids")
                    or decision.get("counterevidence_ids")
                ):
                    raise _invalid()
                citations = [
                    self.knowledge.evidence_dict(db, actor, key) for key in cp["support_refs"]
                ]
                independent = [
                    self.knowledge.evidence_dict(db, actor, key)
                    for key in decision["independent_evidence_ids"]
                ]
                for role, child, refs in (
                    ("analysis", child_pair[0], citations),
                    ("reviewer", child_pair[1], independent),
                ):
                    for citation in refs:
                        read, source = SourceService.validated_read(db, actor, citation["read_id"])
                        if (
                            source.id != version.source_id
                            or read.role != role
                            or read.run_id != child.id
                            or read.principal_id != actor
                            or citation["commit"] != pack["commit"]
                        ):
                            raise _invalid()
                value = {
                    "source_id": version.source_id,
                    "repository": pack["repository"],
                    "commit": pack["commit"],
                    "unit_version_id": version.id,
                    "unit_content_hash": version.content_hash,
                    "unit_render_hash": version.render_hash,
                    "publication_id": identifier,
                    "review_id": review.id,
                    "subject_key": draft["subject_key"],
                    "claim_key": original["key"],
                    "claim_id": claim.id,
                    "claim_hash": fingerprint(cp),
                    "fact_atom_id": fact["fact_atom_id"],
                    "statement": fact["text"],
                    "conditions": deepcopy(fact["conditions"]),
                    "kind": claim.kind,
                    "citations": citations,
                    "independent_citations": independent,
                }
                facts.append(value)
                unit_facts.append(value)
            # Database scan order is not a wire contract (especially on
            # PostgreSQL). Preserve the frozen render order for facts and use
            # stable IDs for unordered related metadata, without changing text.
            units.append(
                {
                    **pack,
                    "facts": [
                        rendered[element["fact_atom_id"]]
                        for element in version.render_ast
                        if element["fact_atom_id"] in rendered
                    ],
                    "citations": sorted(pack["citations"], key=lambda item: item["citation_id"]),
                    "topic_links": sorted(pack["topic_links"], key=lambda item: item["id"]),
                    "facets": sorted(pack["facets"], key=lambda item: item["id"]),
                    "reviewed_claims": unit_facts,
                }
            )
        return facts, units

    @staticmethod
    def _reads(db, actor, children, source_ids):
        output = {}
        for child in children.values():
            read_role = "reviewer" if child.workflow == "review" else "analysis"
            prefix = "independent:" if child.workflow == "review" else "reading:"
            stages = db.scalars(
                select(StageArtifact)
                .where(StageArtifact.run_id == child.id, StageArtifact.stage_key.startswith(prefix))
                .order_by(StageArtifact.stage_key)
            ).all()
            for stage in stages:
                if stage.input_hash != child.input_hash:
                    raise _invalid()
                bundle = stage.payload
                if bundle.get("role") != read_role:
                    raise _invalid()
                for row in bundle.get("reads", []):
                    read, source = SourceService.validated_read(db, actor, row["read_id"])
                    expected = SourceService._read_response(read, source, read.last_line, False)
                    if (
                        read.source_id not in source_ids
                        or read.principal_id != actor
                        or read.run_id != child.id
                        or read.role != read_role
                        or read.session_key != bundle.get("session_key")
                        or any(row.get(key) != val for key, val in expected.items())
                    ):
                        raise _invalid()
                    output[read.id] = {
                        **{key: val for key, val in expected.items() if key != "content"},
                        "role": read_role,
                        "run_id": child.id,
                        "bundle_stage_key": stage.stage_key,
                        "bundle_hash": fingerprint(bundle),
                    }
        return sorted(
            output.values(),
            key=lambda item: (
                item["source_id"],
                item["path"],
                item["role"],
                item["start_line"],
                item["end_line"],
                item["read_id"],
            ),
        )

    @staticmethod
    def _coverage(db, run, publication_ids, units):
        stage = _stage(db, run, "task_sufficiency")
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id, ModelReceipt.stage_key == "task.sufficiency"
            )
        )
        if stage is None:
            return [], ["independent_coverage_not_committed"], None
        if receipt is None or receipt.status != "completed" or receipt.response is None:
            return [], ["independent_coverage_receipt_unavailable"], None
        if receipt.role != "coverage" or receipt.payer_id != run.principal_id:
            raise _invalid()
        binding = {
            "receipt_id": receipt.id,
            "run_id": run.id,
            "stage_key": receipt.stage_key,
            "request_hash": receipt.request_hash,
            "response_hash": fingerprint(receipt.response),
            "stage_payload_hash": fingerprint(stage),
        }
        try:
            report = ResearchSufficiencyOutput.model_validate(receipt.response.get("structured"))
        except ValidationError:
            if stage.get("task_coverages"):
                raise _invalid() from None
            return [], ["independent_coverage_schema_invalid"], binding
        source_units: dict[str, list[str]] = defaultdict(list)
        for unit in units:
            source_units[unit["source_id"]].append(unit["unit_version_id"])
        records = []
        for item in report.source_coverages:
            if item.source_id not in source_units:
                # The existing workflow records this failed output with no
                # coverage. It is not a projection failure or usable evidence.
                if not stage.get("task_coverages") and stage.get("sufficiency_errors"):
                    return [], ["independent_coverage_scope_invalid"], binding
                raise _invalid()
            records.append(
                {
                    "source_id": item.source_id,
                    "unit_version_ids": sorted(source_units[item.source_id]),
                    "coverage": item.coverage.model_dump(mode="json"),
                }
            )
        if (
            stage.get("publication_ids") != publication_ids
            or stage.get("task_coverages") != records
            or stage.get("sufficiency_errors")
        ):
            raise _invalid()
        return [{**row, "assessment_receipt": binding} for row in records], [], binding
