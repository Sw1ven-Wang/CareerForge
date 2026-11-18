from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from sqlalchemy import select

from ..db import locked
from ..errors import DomainError
from ..models import (
    CareerGoal,
    GoalVersion,
    LearningPlan,
    PlanVersion,
    Principal,
    TeachingObservation,
    Topic,
    TopicLink,
    UnitVersion,
)
from ..models_learning_ext import (
    LearningAdjustment,
    LearningObservationRecord,
    LearningProfile,
    PlanProposalManifest,
)
from ..util import canonical, fingerprint
from .bounded_evidence import refresh_context_pack, selection_context_contract
from .consumer_context import CONSUMER_CONTEXT_CONTRACT, standalone_fact_ids
from .dependencies import check_closure, depend, register, require_object
from .learning_ext import LearningLoop, _no_independent_assistance, _profile
from .learning_projection import MODULE_SCHEMA
from .privacy import assert_personal_access
from .runtime import leased
from .security import accessible_namespaces, private_namespace
from .sources import SourceService
from .taxonomy import schema_view

INTENTS = {"build_project", "learn_project", "technical_design", "function"}
COMPARISON_SCOPE_CONTRACT = "evidence-paired-with-conditions-v2"


class LearningService(LearningLoop):
    def __init__(self, app):
        self.app, self.db = app, app.db

    def save_goal(
        self, actor, namespace_id, run_id, text, requirements, *, epoch: int, worker: str
    ):
        """Compatibility entry point; even goal writes require a current worker lease."""
        with self.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            ns = private_namespace(db, actor)
            if (
                run.principal_id != actor
                or run.namespace_id != namespace_id
                or ns.id != namespace_id
            ):
                raise DomainError("GOAL_SCOPE", "目标只能写入本人当前私人域的有效任务。", 403)
            old = db.scalar(select(GoalVersion).where(GoalVersion.run_id == run_id))
            if old:
                return old.id
            goal = CareerGoal(principal_id=actor, namespace_id=namespace_id, title="职业学习目标")
            db.add(goal)
            db.flush()
            version = GoalVersion(
                goal_id=goal.id,
                version=1,
                source_text=text,
                content_hash=fingerprint(text),
                requirements=requirements,
                run_id=run_id,
            )
            db.add(version)
            db.flush()
            goal.active_version_id = version.id
            return version.id

    async def compare(
        self,
        actor: str,
        pack: dict[str, Any],
        topic_keys: list[str],
        *,
        expected_sources: list[str] | None = None,
        scoped_conditions: bool = False,
        diagnostics: bool = False,
        distinguish_multiple: bool = False,
        show_side_coverage: bool = False,
    ) -> dict[str, Any]:
        units = pack["knowledge_units"]
        if scoped_conditions and expected_sources:
            units = [u for u in units if u["source_id"] in expected_sources]

        def insufficient(per_source):
            return {
                **self.app.answers.action("gap", ["comparison_insufficient_evidence"]),
                "text": "当前缺少双方可对齐的已审比较证据。请按项目查看已有资料和待补维度。",
                "comparison_status": "comparison_insufficient_evidence",
                "comparison_sources": per_source,
            }

        if not units:
            if expected_sources is not None:
                availability = [
                    {"source_id": source, "status": "no_reviewed_material", "known_dimensions": []}
                    for source in dict.fromkeys(expected_sources)
                ]
                if diagnostics:
                    with self.db.read() as db:
                        self._comparison_gaps(
                            db,
                            actor,
                            pack,
                            topic_keys,
                            availability,
                            show_side_coverage=show_side_coverage,
                        )
                else:
                    for item in availability:
                        item.pop("known_dimensions")
                return insufficient(availability)
            return self.app.answers.action("gap")
        keys = list(
            dict.fromkeys(
                topic_keys or [link["topic_key"] for u in units for link in u["topic_links"]]
            )
        )

        def fresh_unit(db, publication_id):
            marker = selection_context_contract(db, actor, pack["retrieval_manifest_id"])
            if marker:
                return refresh_context_pack(
                    self.app.knowledge, db, actor, publication_id, pack["retrieval_manifest_id"]
                )
            return self.app.knowledge.publication_pack(db, actor, publication_id)

        with self.db.read() as db:
            privacy_generation = assert_personal_access(db, actor)
            owners = {
                actor,
                *(
                    ns.owner_id
                    for ns in accessible_namespaces(db, actor)
                    if ns.kind != "shared_public"
                ),
            }
            privacy_generations = {
                owner: assert_personal_access(db, owner) for owner in sorted(owners)
            }
            check_closure(db, actor, [pack["retrieval_manifest_id"]])
            units = [fresh_unit(db, u["publication_id"]) for u in units]
            schema = {key: schema_view(db, actor, key) for key in keys}
            old_schemas = {
                (f["topic_key"], f["schema_version"]): schema_view(
                    db, actor, f["topic_key"], f["schema_version"]
                )
                for u in units
                for f in u["facets"]
                if f["topic_key"] in keys
            }
        source_ids = sorted({u["source_id"] for u in units if u["origin_kind"] == "repository"})
        input_ids = [u["publication_id"] for u in units]
        cache_key = fingerprint(
            {
                "actor": actor,
                "privacy_generation": privacy_generation,
                "privacy_generations": privacy_generations,
                "publications": input_ids,
                "schemas": schema,
                "source_ids": source_ids,
                "total_project_groups": pack["total_project_groups"],
            }
        )

        async def materialize():
            rows = []
            for source_id in source_ids:
                selected = [u for u in units if u["source_id"] == source_id]
                cells = []
                for topic, definition in schema.items():
                    for facet in definition["facets"]:
                        values = [
                            f
                            for u in selected
                            for f in u["facets"]
                            if f.get("topic_key") == topic and f["facet_key"] == facet
                        ]
                        compatible = [
                            f
                            for f in values
                            if old_schemas[(topic, f["schema_version"])]["facets"].get(facet)
                            == definition["facets"][facet]
                        ]
                        known = [f for f in compatible if f["value_status"] == "known"]
                        distinct = {canonical(f["value"]) for f in known}
                        status = (
                            ("multiple_reviewed_values" if distinguish_multiple else "conflicting")
                            if len(distinct) > 1
                            else "known"
                            if distinct
                            else "unmapped_legacy"
                            if values and not compatible
                            else "unknown"
                        )
                        cells.append(
                            {
                                "topic_key": topic,
                                "facet_key": facet,
                                "value_status": status,
                                "value": known[0]["value"] if status == "known" else None,
                                **(
                                    {"synthesis_status": "requires_independent_review"}
                                    if status == "multiple_reviewed_values"
                                    else {}
                                ),
                                **(
                                    {
                                        "reviewed_values": [
                                            {
                                                "facet_id": f["id"],
                                                "value": f["value"],
                                                "claim_ids": f["claim_ids"],
                                            }
                                            for f in known
                                        ]
                                    }
                                    if diagnostics
                                    else {}
                                ),
                                "facet_ids": [f["id"] for f in known],
                                "claim_ids": [c for f in known for c in f["claim_ids"]],
                                "target_schema_version": definition["version"],
                                "original_schema_versions": sorted(
                                    {f["schema_version"] for f in known}
                                ),
                                "mapping_status": "compatible_definition"
                                if any(f["schema_version"] != definition["version"] for f in known)
                                else "exact"
                                if known
                                else "fresh_analysis_review_required",
                            }
                        )
                rows.append(
                    {
                        "source_id": source_id,
                        "repository": selected[0]["repository"],
                        "commit": selected[0]["commit"],
                        "cells": cells,
                    }
                )
            return {
                "privacy_owner_id": actor,
                "privacy_generation": privacy_generation,
                "privacy_owner_ids": sorted(privacy_generations),
                "privacy_generations": privacy_generations,
                "rows": rows,
                "topic_keys": keys,
                "schema_versions": {k: v["version"] for k, v in schema.items()},
                "sampled_project_count": len(rows),
                "total_project_groups": pack["total_project_groups"],
                "note": "仅比较本次选定版本与材料；未知不等于不支持。",
            }

        @contextmanager
        def publish_guard() -> Iterator[None]:
            with self.db.transaction() as db:
                for owner, generation in privacy_generations.items():
                    assert_personal_access(db, owner, expected_generation=generation)
                yield

        content = await self.app.cache.singleflight(
            "derived",
            cache_key,
            materialize,
            ttl=60,
            publish_guard=publish_guard,
        )
        with self.db.transaction() as db:
            for owner, generation in privacy_generations.items():
                assert_personal_access(db, owner, expected_generation=generation)
            check_closure(db, actor, [pack["retrieval_manifest_id"]])
            units = [fresh_unit(db, pid) for pid in input_ids]
            for topic, frozen in schema.items():
                if schema_view(db, actor, topic, frozen["version"]) != frozen:
                    raise DomainError("COMPARISON_SCHEMA_CHANGED", "比较所固定的主题维度已变化。")
            old_schemas = {
                (f["topic_key"], f["schema_version"]): schema_view(
                    db, actor, f["topic_key"], f["schema_version"]
                )
                for u in units
                for f in u["facets"]
                if f["topic_key"] in keys
            }
            # Cached cells never grant factual authority. Rebuild the small table from
            # current reviewed facets, including optional-facet revocations and topic scope.
            authoritative = await materialize()
            if content != authoritative:
                content = authoritative
            if expected_sources is not None:
                requested = list(dict.fromkeys(expected_sources or source_ids))
                by_source = {row["source_id"]: row for row in content["rows"]}
                available = [
                    {
                        "source_id": sid,
                        "status": "reviewed_material_available"
                        if sid in by_source
                        else "no_reviewed_material",
                        "known_dimensions": sorted(
                            f"{cell['topic_key']}:{cell['facet_key']}"
                            for cell in by_source.get(sid, {}).get("cells", [])
                            if cell["value_status"] == "known"
                        ),
                    }
                    for sid in requested
                ]
                common = (
                    set.intersection(*(set(row["known_dimensions"]) for row in available))
                    if available
                    else set()
                )
                paired = common
                if diagnostics:
                    for item in available:
                        item["reviewed_dimensions"] = sorted(
                            f"{cell['topic_key']}:{cell['facet_key']}"
                            for cell in by_source.get(item["source_id"], {}).get("cells", [])
                            if cell["reviewed_values"]
                        )
                    if available:
                        paired = set.intersection(
                            *(set(row["reviewed_dimensions"]) for row in available)
                        )
                    self._comparison_gaps(
                        db,
                        actor,
                        pack,
                        keys,
                        available,
                        paired,
                        show_side_coverage=show_side_coverage,
                    )
                if len(requested) < 2 or not paired:
                    return insufficient(available)
                if diagnostics:
                    content = {
                        **content,
                        "source_diagnostics": available,
                        "paired_known_dimensions": sorted(common),
                        "paired_reviewed_dimensions": sorted(paired),
                        **(
                            {
                                "coverage_summary": {
                                    "defined_dimensions": sum(
                                        len(definition["facets"]) for definition in schema.values()
                                    ),
                                    "paired_reviewed_count": len(paired),
                                    "unpaired_dimensions": sorted(
                                        f"{topic}:{facet}"
                                        for topic, definition in schema.items()
                                        for facet in definition["facets"]
                                        if f"{topic}:{facet}" not in paired
                                    ),
                                }
                            }
                            if distinguish_multiple
                            else {}
                        ),
                    }
            if scoped_conditions:
                conditions, citations = self._comparison_conditions(db, actor, units, content)
                content = {**content, "condition_scope": conditions}
            else:
                citations = [c for u in units for c in u["citations"]]
            ns = private_namespace(db, actor)
            comparison = register(db, actor, ns.id, "comparison", content)
            for frozen in schema.values():
                depend(db, actor, comparison.id, frozen["facet_schema_id"], "conforms_to")
            depend(
                db,
                actor,
                comparison.id,
                pack["retrieval_manifest_id"],
                "generated_under",
                "provenance_only",
            )
            for u in units:
                depend(db, actor, comparison.id, u["publication_id"], "compares_with")
                for link in u["topic_links"]:
                    if link["topic_key"] in keys:
                        depend(db, actor, comparison.id, link["id"], "requires_topic_link")
            for row in content["rows"]:
                for cell in row["cells"]:
                    for f in cell["facet_ids"]:
                        depend(db, actor, comparison.id, f, "uses_facet")
            text = (
                "\n".join(
                    [
                        f"{row['repository']} @ {row['commit'][:12]}"
                        + "\n"
                        + "\n".join(
                            f"  {c['facet_key']}: {self._comparison_value_text(c)}"
                            for c in row["cells"]
                        )
                        for row in content["rows"]
                    ]
                )
                or "当前没有可对齐的项目实现。"
            )
            if scoped_conditions:
                text += "\n适用条件以各条证据为准。相同维度可并列阅读，不能据此认定触发条件、稳定性或架构等同。"
            return {
                "kind": "comparison",
                "text": text,
                "comparison_id": comparison.id,
                "comparison": content,
                "publication_ids": input_ids,
                "fact_atom_ids": [],
                "conditions": [],
                "citations": citations,
                "checked": "facet_exact",
            }

    def _comparison_gaps(
        self, db, actor, pack, topic_keys, rows, common=None, *, show_side_coverage=False
    ):
        """Diagnose only authorized default versions in the actual selection scope."""
        manifest = require_object(db, actor, pack["retrieval_manifest_id"], "selection_manifest")
        task = manifest.payload.get("task_spec", {})
        hard_scope = task.get("project_source_ids") or []
        pins = task.get("pinned_publication_ids") or []
        for row in rows:
            source_id = row["source_id"]
            try:
                SourceService.get(db, actor, source_id)
            except DomainError:
                row["gap_reasons"] = ["scope_excluded"]
                continue
            if hard_scope and source_id not in hard_scope:
                row["gap_reasons"] = ["scope_excluded"]
                continue
            if pins:
                eligible = [
                    pub
                    for pub in self.app.knowledge.selected_publications(db, actor, pinned_ids=pins)
                    if db.get(UnitVersion, pub.unit_version_id).source_id == source_id
                ]
            else:
                eligible = self.app.knowledge.selected_publications(db, actor, [source_id])
            linked = False
            proposed = False
            published_dimensions = set()
            for pub in eligible:
                knowledge = self.app.knowledge.publication_pack(db, actor, pub.id)
                linked |= any(
                    link["topic_key"] in topic_keys and link["relation"] != "mentions"
                    for link in knowledge["topic_links"]
                )
                if show_side_coverage:
                    approved_topics = {
                        link["topic_key"]
                        for link in knowledge["topic_links"]
                        if link["relation"] != "mentions"
                    }
                    published_dimensions.update(
                        f"{facet['topic_key']}:{facet['facet_key']}"
                        for facet in knowledge["facets"]
                        if facet["value_status"] == "known"
                        and facet["topic_key"] in topic_keys
                        and facet["topic_key"] in approved_topics
                    )
                proposed |= (
                    db.scalar(
                        select(TopicLink.id)
                        .join(Topic, Topic.id == TopicLink.topic_id)
                        .where(
                            TopicLink.unit_version_id == pub.unit_version_id,
                            Topic.key.in_(topic_keys),
                            TopicLink.relation != "mentions",
                        )
                        .limit(1)
                    )
                    is not None
                )
            if not eligible:
                reasons = ["scope_excluded"] if pins else ["no_reviewed_material"]
            elif topic_keys and not linked:
                reasons = ["unreviewed_relation" if proposed else "missing_topic_link"]
            elif row["status"] == "no_reviewed_material":
                reasons = ["scope_excluded"]
            else:
                reasons = (
                    []
                    if common
                    else [
                        "no_shared_reviewed_dimension" if published_dimensions else "missing_facet"
                    ]
                )
            if show_side_coverage:
                row["published_dimensions"] = sorted(published_dimensions)
                row["unpaired_published_dimensions"] = sorted(
                    published_dimensions - (common or set())
                )
            row["gap_reasons"] = reasons

    @staticmethod
    def _comparison_value_text(cell):
        if cell["value_status"] == "known":
            return str(cell["value"])
        if cell.get("reviewed_values"):
            values = list(dict.fromkeys(str(item["value"]) for item in cell["reviewed_values"]))
            return "多份已审记录，需分别核对条件：" + " / ".join(values)
        return cell["value_status"]

    @staticmethod
    def _comparison_conditions(db, actor, units, content):
        """Keep each known value tied to its own published conditions and source quotes."""
        facts = {
            (unit["source_id"], fact["claim_id"]): (unit, fact)
            for unit in units
            for fact in unit["facts"]
        }
        dimensions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        citations = {}
        for row in content["rows"]:
            sid = row["source_id"]
            for cell in row["cells"]:
                statements = []
                for claim in dict.fromkeys(
                    cell["claim_ids"]
                    if cell["value_status"] == "known" or cell.get("reviewed_values")
                    else []
                ):
                    if (sid, claim) not in facts:
                        raise DomainError("COMPARISON_SCOPE", "比较维度引用未保留的事实。", 409)
                    unit, fact = facts[sid, claim]
                    ref_ids = set(require_object(db, actor, claim, "claim").payload["support_refs"])
                    refs = [ref for ref in unit["citations"] if ref["citation_id"] in ref_ids]
                    if not refs:
                        raise DomainError(
                            "COMPARISON_SCOPE", "比较维度缺少当前公开的精确证据。", 409
                        )
                    citations.update({ref["citation_id"]: ref for ref in refs})
                    statements.append(
                        {
                            "fact_atom_id": fact["fact_atom_id"],
                            "statement": fact["text"],
                            "conditions": fact["conditions"],
                            "citation_ids": [ref["citation_id"] for ref in refs],
                        }
                    )
                dimensions.setdefault((cell["topic_key"], cell["facet_key"]), []).append(
                    {
                        "source_id": sid,
                        "value_status": cell["value_status"],
                        "reviewed_facts": statements,
                        **(
                            {
                                "reviewed_values": [
                                    {
                                        "facet_id": value["facet_id"],
                                        "value": value["value"],
                                        "fact_atom_ids": [
                                            facts[(sid, claim)][1]["fact_atom_id"]
                                            for claim in value["claim_ids"]
                                        ],
                                    }
                                    for value in cell["reviewed_values"]
                                ],
                            }
                            if "reviewed_values" in cell
                            else {}
                        ),
                    }
                )
        output = []
        for (topic, facet), sides in dimensions.items():
            present = [side for side in sides if side["reviewed_facts"]]
            condition_sets = {
                tuple(
                    sorted(
                        {
                            condition
                            for fact in side["reviewed_facts"]
                            for condition in fact["conditions"]
                        }
                    )
                )
                for side in present
            }
            alignment = (
                "missing_side_evidence"
                if len(present) < 2 or len(present) != len(sides)
                else "multiple_reviewed_values"
                if any(
                    side["value_status"] in {"conflicting", "multiple_reviewed_values"}
                    and side.get("reviewed_values")
                    for side in present
                )
                else "conditions_not_fully_recorded"
                if () in condition_sets
                else "different_recorded_conditions"
                if len(condition_sets) > 1
                else "same_recorded_text_only"
            )
            output.append(
                {
                    "topic_key": topic,
                    "facet_key": facet,
                    "trigger_alignment": alignment,
                    "sides": sides,
                }
            )
        return {
            "contract": COMPARISON_SCOPE_CONTRACT,
            "scope": "reviewed_facets_side_by_side",
            "architectural_equivalence_verified": False,
            "quality_ranking_supported": False,
            "dimensions": output,
        }, list(citations.values())

    def propose_plan(self, actor, publication_ids: list[str], intent: str, title: str):
        if (
            intent not in INTENTS
            or not 1 <= len(publication_ids) <= 20
            or not title.strip()
            or len(title) > 200
        ):
            raise DomainError("PLAN_INPUT", "计划范围或意图无效。", 422)
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            _no_independent_assistance(db, actor)
            ns = private_namespace(db, actor)
            plan = LearningPlan(principal_id=actor, namespace_id=ns.id)
            db.add(plan)
            db.flush()
            selection = register(
                db,
                actor,
                ns.id,
                "selection_manifest",
                {
                    "purpose": "deterministic_learning_plan_proposal",
                    "publication_ids": list(dict.fromkeys(publication_ids)),
                },
            )
            for pid in dict.fromkeys(publication_ids):
                depend(db, actor, selection.id, pid, "selects_source")
            modules: list[str] = []
            for i, pid in enumerate(dict.fromkeys(publication_ids)):
                p = self.app.knowledge.publication_pack(
                    db, actor, pid, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
                )
                eligible = standalone_fact_ids(p)
                if not eligible:
                    raise DomainError(
                        "FACT_CONTEXT_REQUIRED", "当前发布没有获独立使用审核的事实。", 422
                    )
                payload = {
                    "consumer_context_contract": CONSUMER_CONTEXT_CONTRACT,
                    "focus_fact_ids": [
                        f["fact_atom_id"] for f in p["facts"] if f["fact_atom_id"] in eligible
                    ],
                    "omitted_facts": [
                        {"fact_atom_id": f["fact_atom_id"], "reason": "standalone_review_required"}
                        for f in p["facts"]
                        if f["fact_atom_id"] not in eligible
                    ],
                    "title": p["title"],
                    "publication_id": pid,
                    "topic_keys": [x["topic_key"] for x in p["topic_links"]],
                    "prerequisite_module_ids": modules[-1:],
                    "learning_intent": intent,
                    "steps": [
                        "阅读已审核说明与原始代码",
                        "说明关键条件与边界",
                        "完成一个受限变体或解释任务",
                    ],
                    "rubric": [
                        "引用正确版本并解释关键条件",
                        "追踪实际输入输出与错误边界",
                        "说明迁移变体的差异",
                    ],
                    "competence_verified": False,
                    "position": i,
                }
                module = register(db, actor, ns.id, "plan_module", payload)
                depend(db, actor, module.id, pid, "teaches_from")
                depend(db, actor, module.id, selection.id, "generated_under", "provenance_only")
                for link in p["topic_links"]:
                    depend(db, actor, module.id, link["id"], "requires_topic_link")
                modules.append(module.id)
            version = PlanVersion(
                plan_id=plan.id, version=1, title=title, learning_intent=intent, module_ids=modules
            )
            db.add(version)
            db.flush()
            frozen = {
                "module_ids": modules,
                "selection_manifest_id": selection.id,
                "generation_mode": "deterministic_evidence_only_not_personalized_model",
            }
            db.add(
                PlanProposalManifest(
                    principal_id=actor,
                    plan_version_id=version.id,
                    selection_manifest_id=selection.id,
                    content_hash=fingerprint(frozen),
                    payload=frozen,
                )
            )
            return {
                "plan_id": plan.id,
                "version_id": version.id,
                "revision": plan.revision,
                "accepted": False,
                "module_ids": modules,
                "generation_mode": "deterministic_evidence_only_not_personalized_model",
                "selection_manifest_id": selection.id,
            }

    def detail(self, actor, plan_id):
        with self.db.read() as db:
            ns = private_namespace(db, actor)
            plan = db.get(LearningPlan, plan_id)
            if not plan or plan.principal_id != actor or plan.namespace_id != ns.id:
                raise DomainError("NOT_FOUND", "计划不存在。", 404)
            versions = []
            for v in db.scalars(
                select(PlanVersion)
                .where(PlanVersion.plan_id == plan.id)
                .order_by(PlanVersion.version)
            ):
                try:
                    proposal = self._validated_plan_manifest(db, actor, v)
                    projection = (
                        self._project_manifest(proposal.payload)
                        if proposal
                        else self._project_manifest({})
                    )
                    metadata_available = True
                except DomainError:
                    projection = {"uncovered_goal_requirements": [], "raw_draft_exposed": False}
                    metadata_available = False
                modules = []
                for mid in v.module_ids:
                    try:
                        obj = require_object(db, actor, mid, "plan_module", active=False)
                        check_closure(db, actor, [mid])
                        valid = metadata_available and obj.payload.get("schema_version") in {
                            None,
                            MODULE_SCHEMA,
                        }
                    except DomainError:
                        valid = False
                    modules.append(
                        {
                            "module_id": mid,
                            "available": valid,
                            "content": obj.payload if valid else {"status": "blocked_by_evidence"},
                        }
                    )
                versions.append(
                    {
                        "version_id": v.id,
                        "version": v.version,
                        "title": v.title,
                        "modules": modules,
                        "proposal_metadata_available": metadata_available,
                        "accepted": plan.active_version_id == v.id,
                        "competence_verified": False,
                        **projection,
                    }
                )
            return {
                "plan_id": plan.id,
                "revision": plan.revision,
                "active_version_id": plan.active_version_id,
                "versions": versions,
            }

    def accept_plan(self, actor, plan_id, version_id, revision, *, db=None):
        with self.db.transaction() if db is None else nullcontext(db) as db:
            ns = private_namespace(db, actor)
            plan = locked(db, LearningPlan, plan_id)
            version = db.get(PlanVersion, version_id)
            if (
                not plan
                or plan.principal_id != actor
                or plan.namespace_id != ns.id
                or not version
                or version.plan_id != plan.id
            ):
                raise DomainError("NOT_FOUND", "计划不存在。", 404)
            if plan.revision != revision:
                raise DomainError("REVISION_CONFLICT", "计划已经变化。")
            check_closure(db, actor, version.module_ids)
            proposal = self._validated_plan_manifest(db, actor, version)
            self._validate_module_order(db, actor, version.module_ids)
            adjustment = db.scalar(
                select(LearningAdjustment).where(
                    LearningAdjustment.proposed_version_id == version.id,
                )
            )
            if adjustment:
                if adjustment.source_version_id != plan.active_version_id:
                    raise DomainError("PLAN_BASE_CHANGED", "调整提议的基线已变化。")
                for identifier in adjustment.observation_ids:
                    observation = db.get(LearningObservationRecord, identifier)
                    if (
                        not observation
                        or observation.principal_id != actor
                        or observation.status != "active"
                    ):
                        raise DomainError(
                            "ADJUSTMENT_EVIDENCE", "调整依据已更正或撤回，请重新提议。"
                        )
            plan.active_version_id = version_id
            plan.revision += 1
            profile = _profile(db, actor)
            # Same Plan -> Profile order as explicit module ACK. A different
            # plan can be accepted while another transaction advances a module;
            # refresh under the row lock so neither loses the profile revision.
            profile = locked(db, LearningProfile, profile.id)
            profile.active_plan_id = plan.id
            profile.active_module_id = version.module_ids[0] if version.module_ids else None
            profile.revision += 1
            result = {
                "plan_id": plan.id,
                "active_version_id": version_id,
                "revision": plan.revision,
            }
            if proposal and proposal.payload.get("input", {}).get("session_contract"):
                projected = self._project_manifest(proposal.payload)
                result.update(
                    session_schedule=projected["schedule"],
                    planned_minutes=projected["planned_minutes"],
                    total_minutes=projected["total_minutes"],
                    schedule_status="accepted",
                    active_module_id=profile.active_module_id,
                )
            return result

    def observe(self, actor, module_id, response, rubric, help_mode):
        if help_mode not in {"assisted", "self_declared_independent", "unknown"}:
            raise DomainError("HELP_MODE", "帮助条件无效。", 422)
        if not response or len(response) > 10000 or not isinstance(rubric, dict):
            raise DomainError("OBSERVATION", "教学观察输入无效。", 422)
        with self.db.transaction() as db:
            module = require_object(db, actor, module_id, "plan_module")
            namespace = private_namespace(db, actor)
            if module.namespace_id != namespace.id:
                raise DomainError("NOT_FOUND", "学习模块不存在。", 404)
            row = TeachingObservation(
                principal_id=actor,
                module_id=module_id,
                response=response,
                rubric=rubric,
                help_mode=help_mode,
                evaluator="user_self_review",
                competence_verified=False,
            )
            db.add(row)
            db.flush()
            missed = [k for k, v in rubric.items() if v is False]
            action = "reduce_step_or_prerequisite" if missed else "offer_transfer_exercise"
            return {
                "observation_id": row.id,
                "suggested_action": action,
                "missing_items": missed,
                "competence_verified": False,
                "plan_changed": False,
                "observation_scope": "legacy_unstructured_self_report_not_frozen_rubric_assessment",
            }

    def revise_plan(self, actor, plan_id, revision, reason, ordered_module_ids):
        with self.db.transaction() as db:
            ns = private_namespace(db, actor)
            plan = locked(db, LearningPlan, plan_id)
            if not plan or plan.principal_id != actor or plan.namespace_id != ns.id:
                raise DomainError("NOT_FOUND", "计划不存在。", 404)
            if plan.revision != revision:
                raise DomainError("REVISION_CONFLICT", "计划已变化。")
            old = db.get(PlanVersion, plan.active_version_id)
            if (
                not old
                or set(ordered_module_ids) != set(old.module_ids)
                or len(set(ordered_module_ids)) != len(ordered_module_ids)
            ):
                raise DomainError(
                    "PLAN_SCOPE", "修订只允许重排现有模块；替换知识需创建明确的新提议。", 422
                )
            check_closure(db, actor, ordered_module_ids)
            self._validate_module_order(db, actor, ordered_module_ids)
            self._validated_plan_manifest(db, actor, old)
            versions = db.scalars(select(PlanVersion).where(PlanVersion.plan_id == plan_id)).all()
            version = PlanVersion(
                plan_id=plan_id,
                version=max(x.version for x in versions) + 1,
                title=old.title,
                learning_intent=old.learning_intent,
                module_ids=ordered_module_ids,
                source_version_id=old.id,
                change_reason=reason[:1000],
            )
            db.add(version)
            db.flush()
            self._inherit_plan_manifest(db, actor, old, version)
            return {
                "plan_id": plan.id,
                "version_id": version.id,
                "expected_revision": plan.revision,
                "accepted": False,
            }
