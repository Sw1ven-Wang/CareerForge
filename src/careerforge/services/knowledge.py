from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..contracts import AnalysisOutput, ClaimReview, CoverageOutput, ReviewOutput, UnitDraft
from ..contracts_scoped_review_v166 import (
    MECHANISM_PUBLICATION_CONTRACT,
    PARTIAL_LIMITATIONS,
    PARTIAL_TITLE,
    SCOPED_REVIEW_VERSION,
    TARGETED_REVIEW_CONTRACT,
    MechanismCoverageOutput,
    RelationScopedCoverageOutput,
    ScopedCoverageOutput,
    ScopedReviewOutput,
)
from ..db import Database, locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    ArtifactDependency,
    Base,
    Claim,
    Evidence,
    FacetValue,
    FactAtom,
    KnowledgeUnit,
    ModelReceipt,
    Namespace,
    Outbox,
    ProjectSelection,
    Publication,
    ReviewDecision,
    SelectionHead,
    SourceRead,
    StageArtifact,
    Topic,
    TopicLink,
    UnitVersion,
)
from ..util import canonical, digest, fingerprint, uid
from .build_execution import EVIDENCE_RETRY_CONTRACT
from .consumer_context import CONSUMER_CONTEXT_CONTRACT, validate_context_contract
from .dependencies import assert_source_scope, check_closure, depend, register, require_object
from .mechanism_edges import analysis_candidates, reviewed_edges
from .public_read_authority import retained_public_review_session
from .runtime import Runtime, active_funding, leased
from .scoped_review import derive_scope, scoped_review_enabled
from .security import accessible_namespaces, namespace_access, private_namespace
from .sources import (
    QUOTE_MAX_CANDIDATES,
    QUOTE_MAX_FEEDBACK_BYTES,
    SourceService,
    quote_line_contract_supported,
    quote_occurrences,
)
from .taxonomy import facet_object_id, require_topic, schema_view, topic_object_id

RecordT = TypeVar("RecordT", bound=Base)


def _required_record(db: Session, model: type[RecordT], identifier: str) -> RecordT:
    """Require a relational projection after its enclosing object was authorized.

    A dependency ObjectRow is not proof that its typed record still exists. Missing
    projections must become a domain failure, never an unchecked None dereference.
    """
    record = db.get(model, identifier)
    if record is None:
        raise DomainError("ARTIFACT_MISSING", "知识关联记录缺失，无法使用该资料。")
    return record


class KnowledgeService:
    @staticmethod
    def pack_dependencies(pack: dict[str, Any]) -> list[str]:
        """Explicit authority roots for the exact validated publication view.

        A Publication deliberately has impact-only links to optional taxonomy
        and facet annotations. If a caller actually sends their text to a model,
        those exposed objects become direct egress dependencies for that request.
        This helper accepts server-validated/frozen packs, never arbitrary model
        prose, and does not guess identifiers by scanning strings.
        """
        roots: set[str] = set()
        for key in ("publication_id", "unit_version_id", "source_id"):
            value = pack.get(key)
            if not isinstance(value, str) or not value:
                raise DomainError("PUBLICATION_CONTRACT", "模型知识包缺少固定身份。", 422)
            roots.add(value)
        for fact in pack.get("facts", []):
            roots.update(
                fact[key] for key in ("fact_atom_id", "claim_id") if isinstance(fact.get(key), str)
            )
        for citation in pack.get("citations", []):
            if isinstance(citation.get("citation_id"), str):
                roots.add(citation["citation_id"])
        for link in pack.get("topic_links", []):
            roots.update(
                link[key] for key in ("id", "topic_version_id") if isinstance(link.get(key), str)
            )
        for facet in pack.get("facets", []):
            roots.update(
                facet[key] for key in ("id", "facet_schema_id") if isinstance(facet.get(key), str)
            )
        return sorted(roots)

    def __init__(self, database: Database, runtime: Runtime):
        self.db, self.runtime = database, runtime

    def preflight_quote_references(
        self,
        actor: str,
        run_id: str,
        epoch: int,
        worker: str,
        source_id: str,
        session_key: str,
        bundle: dict[str, Any],
        output: AnalysisOutput | ReviewOutput,
        *,
        role: str,
        access_dependencies: list[str],
        unit_version_id: str | None = None,
    ) -> dict[str, Any]:
        """No-write, fully fenced preflight; only actual duplicate quotes are feedback.

        This never accepts evidence or changes a model answer. The ordinary
        save_analysis/save_review and publication gates must still run after it.
        A whole diagnostic must fit the bound; never truncate candidates and
        then guess that a reference is unique.
        """
        issues, read_contexts = [], {}
        candidate_count = 0
        with self.db.read() as db:
            run = leased(db, run_id, epoch, worker)
            if run.principal_id != actor:
                raise DomainError("QUOTE_AUTHORITY", "引用预检执行身份不一致。", 403)
            roots = sorted(set(access_dependencies) | {source_id})
            if role == "analysis":
                if (
                    run.workflow != "analysis"
                    or source_id not in run.input_manifest.get("source_ids", [])
                    or not isinstance(output, AnalysisOutput)
                    or session_key != "analysis:" + source_id
                ):
                    raise DomainError("ANALYSIS_AUTHORITY", "分析范围或执行身份不一致。", 403)
                if len(output.units) > run.input_manifest.get("max_units", 0):
                    raise DomainError("UNIT_BUDGET", "分析超出冻结的知识单元数量。", 422)
                stage_key = "reading:" + source_id
                refs = [
                    (f"/units/{ui}/claims/{ci}/{group}/{ri}", ref)
                    for ui, unit in enumerate(output.units)
                    for ci, claim in enumerate(unit.claims)
                    for group in ("support", "counterevidence")
                    for ri, ref in enumerate(getattr(claim, group))
                ]
            elif role == "reviewer":
                if (
                    run.workflow != "review"
                    or not isinstance(unit_version_id, str)
                    or unit_version_id not in run.input_manifest.get("unit_version_ids", [])
                    or not isinstance(output, ReviewOutput)
                    or session_key != "reviewer:" + str(unit_version_id)
                ):
                    raise DomainError("REVIEW_AUTHORITY", "审核范围或执行身份不一致。", 403)
                version = self._validated_version(db, actor, unit_version_id)
                if version.source_id != source_id or version.analysis_run_id == run.id:
                    raise DomainError("INDEPENDENCE", "独立审核来源或角色不一致。", 403)
                roots.append(version.id)
                expected = set(
                    db.scalars(select(Claim.id).where(Claim.unit_version_id == version.id))
                )
                actual = [claim.claim_id for claim in output.claim_decisions]
                if len(actual) != len(set(actual)) or set(actual) != expected:
                    raise DomainError("REVIEW_COVERAGE", "审核必须逐项覆盖所有断言。", 422)
                if any(
                    claim.outcome == "supported_within_scope" and not claim.independent_reads
                    for claim in output.claim_decisions
                ):
                    raise DomainError(
                        "INDEPENDENT_READ_REQUIRED", "接受断言需要独立读取原文。", 422
                    )
                stage_key = "independent:" + version.id
                refs = [
                    (f"/claim_decisions/{ci}/{group}/{ri}", ref)
                    for ci, claim in enumerate(output.claim_decisions)
                    for group in ("independent_reads", "counterevidence")
                    for ri, ref in enumerate(getattr(claim, group))
                ]
            else:
                raise DomainError("QUOTE_AUTHORITY", "此角色不能进行引用纠正。", 403)
            self._update_scope_in(db, actor, run.input_manifest.get("update_scope"))
            check_closure(db, actor, roots)
            assert_source_scope(db, roots, active_funding(db, run).allowed_source_ids)
            frozen = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == stage_key
                )
            )
            if (
                frozen is None
                or frozen.input_hash != run.input_hash
                or fingerprint(frozen.payload) != fingerprint(bundle)
                or bundle.get("role") != role
                or bundle.get("session_key") != session_key
            ):
                raise DomainError("QUOTE_BUNDLE", "引用预检不属于冻结的已读证据包。", 422)
            allowed = set()
            supported_read_lines = {}
            for item in bundle.get("reads", []):
                identifier = item.get("read_id")
                row = db.get(SourceRead, identifier)
                if (
                    row is None
                    or row.principal_id != actor
                    or row.source_id != source_id
                    or row.run_id != run.id
                    or row.role != role
                    or row.session_key != session_key
                ):
                    raise DomainError("UNREAD_EVIDENCE", "读取不属于本次角色或来源。", 422)
                row, source = SourceService.validated_read(db, actor, identifier)
                if any(
                    item.get(key) != value
                    for key, value in {
                        "source_id": source.id,
                        "path": row.path,
                        "start_line": row.first_line,
                        "end_line": row.last_line,
                        "content": row.text,
                        "file_hash": row.file_hash,
                        "quote_hash": row.quote_hash,
                        "commit": source.commit_sha,
                    }.items()
                ):
                    raise DomainError("EVIDENCE_HASH", "冻结读取与来源回执不一致。")
                allowed.add(identifier)
                supported_read_lines[identifier] = quote_line_contract_supported(row.text)
            for pointer, ref in refs:
                if ref.read_id not in allowed:
                    raise DomainError(
                        "INDEPENDENT_BUNDLE" if role == "reviewer" else "QUOTE_BUNDLE",
                        "引用不属于本次冻结的已读证据包。",
                        422,
                    )
                if not supported_read_lines[ref.read_id]:
                    raise DomainError(
                        "QUOTE_LINE_CONTRACT",
                        "本执行合同仅支持LF或CRLF行分隔的引用读取；其他分隔不纠正、不发布。",
                        422,
                    )
                detail = SourceService.inspect_quote_from_read(
                    db,
                    actor,
                    source_id,
                    ref.read_id,
                    ref.quote,
                    role,
                    session_key,
                    run.id,
                    report_missing=run.input_manifest.get("missing_quote_correction")
                    == "frozen-read-missing-quote-v1",
                )
                if detail["match_count"] != 1:
                    candidate_count += detail["match_count"]
                    if candidate_count > QUOTE_MAX_CANDIDATES:
                        raise DomainError(
                            "QUOTE_FEEDBACK_LIMIT",
                            "完整引用候选总量超过256个上限，未截断或自动选择。",
                            422,
                        )
                    read_contexts[ref.read_id] = detail.pop("read")
                    issues.append({"pointer": pointer, **detail})
            result = {
                "issues": issues,
                "reads": [read_contexts[key] for key in sorted(read_contexts)],
                "reference_count": len(refs),
                "candidate_status": "unaccepted_locations_not_evidence",
                "all_candidates_returned": True,
            }
            if len(canonical(result).encode("utf-8")) > QUOTE_MAX_FEEDBACK_BYTES:
                raise DomainError(
                    "QUOTE_FEEDBACK_LIMIT", "完整引用候选超过反馈上限，未截断或自动选择。", 422
                )
            return result

    @staticmethod
    def _validated_version(db: Session, actor: str, version_id: str) -> UnitVersion:
        obj = require_object(db, actor, version_id, "knowledge_unit_version")
        version = db.get(UnitVersion, version_id)
        if version is None:
            raise DomainError("UNIT_HASH", "知识版本记录缺失。")
        draft = obj.payload.get("draft", {})
        unit = db.get(KnowledgeUnit, version.unit_id)
        source = SourceService.get(db, actor, version.source_id)
        if "dependency_manifest" in obj.payload:
            dependencies = obj.payload["dependency_manifest"]
        else:
            # Legacy artifacts used a deterministic partial manifest. Never accept
            # a projection-only upgrade to "complete" as carry-forward authority.
            read_ids = [
                ref["read_id"]
                for claim in draft.get("claims", [])
                for ref in claim.get("support", []) + claim.get("counterevidence", [])
            ]
            paths = set(draft.get("dependency_paths", [])) | set(
                db.scalars(select(SourceRead.path).where(SourceRead.id.in_(read_ids)))
            )
            dependencies = {
                "paths": {
                    path: digest(source.files[path])
                    for path in sorted(paths)
                    if path in source.files
                },
                "closure": "partial",
                "scope_hash": source.scope_hash,
            }
        if (
            version.dependency_manifest != dependencies
            or obj.payload.get("unit_id") != version.unit_id
            or obj.payload.get("source_id") != version.source_id
            or version.content_hash != fingerprint(draft)
            or version.render_hash != fingerprint(version.render_ast)
            or version.render_ast != obj.payload.get("render")
            or version.title != draft.get("title")
            or version.limitations != draft.get("limitations")
            or ("synthetic" in obj.payload and version.synthetic != obj.payload["synthetic"])
            or (source.metadata_json.get("synthetic") and not version.synthetic)
            or unit is None
            or unit.namespace_id != obj.namespace_id
            or unit.origin_kind != source.origin_kind
            or unit.origin_id != source.repository_key
            or unit.unit_type != draft.get("unit_type")
            or unit.subject_key != draft.get("subject_key")
            or unit.applicability_key != digest(draft.get("applicability", "default"))
        ):
            raise DomainError("UNIT_HASH", "知识版本正文与不可变审核输入不一致。")
        return version

    def validated_review(
        self, db: Session, actor: str, review_id: str, version: UnitVersion
    ) -> ReviewDecision:
        obj = require_object(db, actor, review_id, "review_decision")
        review = db.get(ReviewDecision, review_id)
        if (
            review is None
            or review.unit_version_id != version.id
            or review.report != obj.payload
            or review.decision != obj.payload.get("decision")
            or review.independent_bundle_hash != obj.payload.get("independent_bundle_hash")
            or review.reviewed_content_hash != version.content_hash
            or review.reviewed_render_hash != version.render_hash
        ):
            raise DomainError("REVIEW_HASH", "审核决议与固定版本不一致。")
        for decision in review.report.get("claim_decisions", []):
            for evidence_id in decision.get("independent_evidence_ids", []) + decision.get(
                "counterevidence_ids", []
            ):
                self.evidence_dict(db, actor, evidence_id)
        if review.decision == "accept_partial_scoped":
            run = _required_record(db, AgentRun, review.reviewer_run_id)
            if (
                not scoped_review_enabled(run.input_manifest)
                or fingerprint(run.input_manifest) != run.input_hash
                or run.workflow != "review"
                or version.id not in run.input_manifest.get("unit_version_ids", [])
                or run.id == version.analysis_run_id
            ):
                raise DomainError("SCOPED_REVIEW_CONTRACT", "部分发布缺少原冻结审核合同。")
            subject = self._scope_subject_in(db, actor, version)
            try:
                original = ScopedReviewOutput.model_validate(
                    review.report.get("scoped_original_review")
                )
                coverage = self._coverage_type(run).model_validate(
                    review.report.get("fact_coverage")
                )
            except ValidationError as exc:
                raise DomainError(
                    "SCOPED_REVIEW_BINDING", "部分审核的原始合同输出缺失或损坏。"
                ) from exc
            expected = self._review_partition(db, actor, run, version, subject, original, coverage)
            if expected["status"] != "partial" or expected != review.report.get("scope_derivation"):
                raise DomainError("SCOPED_REVIEW_BINDING", "部分发布推导与原逐项审核不一致。")
            bundle = self._scoped_bundle_in(db, actor, run, version)
            if fingerprint(bundle) != review.independent_bundle_hash:
                raise DomainError("INDEPENDENT_BUNDLE", "独立证据包已变化。")
            if self._scoped_receipts_in(
                db, actor, run, version, original, coverage
            ) != review.report.get("scoped_receipts"):
                raise DomainError("SCOPED_REVIEW_BINDING", "部分发布的原模型回执已变化。")
        elif review.report.get("scope_derivation", {}).get("status") == "partial":
            raise DomainError("SCOPED_REVIEW_BINDING", "部分审核不得改写成整单元接受。")
        return review

    def _scope_subject_in(self, db: Session, actor: str, version: UnitVersion) -> dict[str, Any]:
        """Validate the complete original graph before selecting any subset."""
        check_closure(db, actor, [version.id])
        vo = require_object(db, actor, version.id, "knowledge_unit_version")
        source = SourceService.get(db, actor, version.source_id)
        draft = vo.payload["draft"]
        facts = {
            f.id: f
            for f in db.scalars(select(FactAtom).where(FactAtom.unit_version_id == version.id))
        }
        claims = {
            c.id: c for c in db.scalars(select(Claim).where(Claim.unit_version_id == version.id))
        }
        if (
            len(draft["claims"]) != len(version.render_ast)
            or len(facts) != len(claims)
            or len(facts) != len(version.render_ast)
        ):
            raise DomainError("FACT_COVERAGE", "原候选事实集合不完整。")
        claim_views, elements = [], []
        for item, rendered in zip(draft["claims"], version.render_ast, strict=True):
            fact = facts.get(rendered["fact_atom_id"])
            claim = claims.get(fact.claim_id) if fact else None
            if fact is None or claim is None:
                raise DomainError("FACT_COVERAGE", "原候选断言与事实不一致。")
            fp = require_object(db, actor, fact.id, "fact_atom").payload
            cp = require_object(db, actor, claim.id, "claim").payload
            ep = require_object(db, actor, rendered["element_id"], "render_element").payload
            if (
                ep != rendered
                or fp
                != {
                    "claim_id": claim.id,
                    "text": item["statement"],
                    "conditions": item["conditions"],
                }
                or rendered["text"] != item["statement"]
                or rendered["conditions"] != item["conditions"]
                or fact.approved_text != item["statement"]
                or fact.conditions != item["conditions"]
                or cp["statement"] != item["statement"]
                or cp["conditions"] != item["conditions"]
                or claim.statement != item["statement"]
                or claim.conditions != item["conditions"]
                or claim.kind != item["kind"]
            ):
                raise DomainError("FACT_HASH", "部分发布不得改变原断言、条件或证据类型。")
            for group, field in (
                ("support", "support_refs"),
                ("counterevidence", "counterevidence_refs"),
            ):
                if len(item[group]) != len(cp[field]):
                    raise DomainError("EVIDENCE_HASH", "原断言引用集合不一致。")
                for ref, eid in zip(item[group], cp[field], strict=True):
                    evidence = self.evidence_dict(db, actor, eid)
                    if (
                        evidence["source_id"] != source.id
                        or evidence["read_id"] != ref["read_id"]
                        or evidence["quote"] != ref["quote"]
                    ):
                        raise DomainError("EVIDENCE_HASH", "原断言引用不属于其固定来源。")
            claim_views.append(
                {
                    "claim_id": claim.id,
                    "kind": claim.kind,
                    "counterevidence_ids": cp["counterevidence_refs"],
                }
            )
            elements.append({**rendered, "claim_id": claim.id})
        if len({e["fact_atom_id"] for e in elements}) != len(facts) or len(
            {e["claim_id"] for e in elements}
        ) != len(claims):
            raise DomainError("FACT_COVERAGE", "原候选重复或替换了事实。")
        return {
            "unit_version_id": version.id,
            "content_hash": version.content_hash,
            "render_hash": version.render_hash,
            "source_id": source.id,
            "source_commit": source.commit_sha,
            "source_scope_hash": source.scope_hash,
            "claims": claim_views,
            "elements": elements,
            "topic_link_ids": sorted(
                db.scalars(select(TopicLink.id).where(TopicLink.unit_version_id == version.id))
            ),
            "facet_ids": sorted(
                db.scalars(select(FacetValue.id).where(FacetValue.unit_version_id == version.id))
            ),
        }

    @staticmethod
    def _coverage_type(run: AgentRun) -> type[ScopedCoverageOutput]:
        marker = run.input_manifest.get("targeted_review_contract")
        if marker is None:
            return ScopedCoverageOutput
        if marker != TARGETED_REVIEW_CONTRACT:
            raise DomainError("MECHANISM_REVIEW_CONTRACT", "未知的定向审核合同。", 409)
        if run.input_manifest.get(
            "mechanism_publication_contract"
        ) == MECHANISM_PUBLICATION_CONTRACT and run.input_manifest.get("mechanism_edge_targets"):
            return MechanismCoverageOutput
        return RelationScopedCoverageOutput

    def mechanism_candidates(self, db, actor, reviewer, version):
        return analysis_candidates(self, db, actor, reviewer, version)

    def _review_partition(
        self, db, actor, run, version, subject, review, coverage
    ) -> dict[str, Any]:
        if run.input_manifest.get("coverage_scope_contract") == "review-global-scope-v1":
            from .coverage_issues import validate_global_scope

            validate_global_scope(review, coverage)
        partition = derive_scope(subject, review, coverage)
        if not isinstance(coverage, RelationScopedCoverageOutput):
            return partition
        bundle = self._scoped_bundle_in(db, actor, run, version)
        valid_reads = {item["read_id"] for item in bundle["reads"]}
        retained = set(partition["retained_claim_ids"])
        reviewer_reads = {
            decision.claim_id: {ref.read_id for ref in decision.independent_reads}
            for decision in review.claim_decisions
        }
        issues = review.scope_review.issue_scopes if review.scope_review else []
        blocked = {
            target
            for issue in issues
            if issue.scope in {"topic_link", "facet"}
            for target in issue.target_ids
        }
        draft = require_object(db, actor, version.id, "knowledge_unit_version").payload["draft"]
        original_topics = {(item["topic_key"], item["relation"]) for item in draft["topics"]}
        audit = coverage.relation_audit
        approvals = {}

        def support(items, kind, eligible, accepted):
            if len({a.annotation_id for a in items}) != len(items):
                raise DomainError("ANNOTATION_SCOPE", "重复的关系审核。", 422)
            for a in items:
                claims, reads = set(a.claim_ids), set(a.independent_read_ids)
                if (
                    a.annotation_id not in eligible
                    or a.annotation_id not in accepted
                    or a.annotation_id in blocked
                    or len(claims) != len(a.claim_ids)
                    or len(reads) != len(a.independent_read_ids)
                    or not reads <= valid_reads
                    or not claims <= reviewer_reads.keys()
                    or not all(reviewer_reads[claim] & reads for claim in claims)
                ):
                    raise DomainError("ANNOTATION_SCOPE", "关系审核缺少固定的独立证据或批准。", 422)
                if claims <= retained:
                    approvals[a.annotation_id] = {"kind": kind, **a.model_dump(mode="json")}

        support(
            audit.topic_links,
            "topic_link",
            subject["topic_link_ids"],
            review.accepted_topic_link_ids,
        )
        support(audit.facets, "facet", subject["facet_ids"], review.accepted_facet_ids)
        retained_links = []
        retained_topics = set()
        for proof in audit.topic_links:
            link = _required_record(db, TopicLink, proof.annotation_id)
            topic = _required_record(db, Topic, link.topic_id)
            if (topic.key, link.relation) not in original_topics:
                raise DomainError("ANNOTATION_SCOPE", "主题不属于原冻结版本。", 422)
            # A partial publication can illustrate a mechanism. It cannot claim
            # a complete implementation merely because a local fact survived.
            if link.relation == "illustrates" and link.id in approvals:
                retained_links.append(link.id)
                retained_topics.add(link.topic_id)
        retained_facets = []
        for proof in audit.facets:
            facet = _required_record(db, FacetValue, proof.annotation_id)
            if (
                facet.id in approvals
                and facet.topic_id in retained_topics
                and facet.value_status == "known"
                and not facet.required
                and set(facet.claim_ids) == set(proof.claim_ids)
            ):
                retained_facets.append(facet.id)
        partition.pop("derivation_hash")
        partition.update(
            retained_topic_link_ids=sorted(retained_links),
            retained_facet_ids=sorted(retained_facets),
            quarantined_topic_link_ids=sorted(set(subject["topic_link_ids"]) - set(retained_links)),
            quarantined_facet_ids=sorted(set(subject["facet_ids"]) - set(retained_facets)),
            annotation_reason="separate_review_of_retained_claims",
            annotation_witnesses={
                key: approvals[key] for key in sorted(retained_links + retained_facets)
            },
        )
        if isinstance(coverage, MechanismCoverageOutput):
            partition["retained_mechanism_edges"] = reviewed_edges(
                self, db, actor, run, version, review, coverage, partition, bundle
            )
        return {**partition, "derivation_hash": fingerprint(partition)}

    @staticmethod
    def _scoped_bundle_in(
        db: Session, actor: str, run: AgentRun, version: UnitVersion
    ) -> dict[str, Any]:
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key == "independent:" + version.id,
            )
        )
        if stage is None or stage.input_hash != run.input_hash:
            raise DomainError("INDEPENDENT_BUNDLE", "部分审核缺少冻结的独立证据包。")
        bundle = stage.payload
        session = "reviewer:" + version.id
        if bundle.get("role") != "reviewer" or bundle.get("session_key") != session:
            raise DomainError("INDEPENDENT_BUNDLE", "部分审核的独立角色身份不一致。")
        seen = set()
        for item in bundle.get("reads", []):
            row, source = SourceService.validated_read(db, actor, item["read_id"])
            if (
                row.id in seen
                or row.run_id != run.id
                or row.principal_id != run.principal_id
                or row.role != "reviewer"
                or (
                    row.session_key != session
                    and not retained_public_review_session(db, actor, run, version, bundle, row)
                )
                or row.source_id != version.source_id
                or any(
                    item.get(key) != value
                    for key, value in {
                        "source_id": source.id,
                        "path": row.path,
                        "start_line": row.first_line,
                        "end_line": row.last_line,
                        "content": row.text,
                        "file_hash": row.file_hash,
                        "quote_hash": row.quote_hash,
                        "commit": source.commit_sha,
                    }.items()
                )
            ):
                raise DomainError("INDEPENDENT_BUNDLE", "独立包与实际读取回执不一致。")
            seen.add(row.id)
        return bundle

    @staticmethod
    def _scoped_receipts_in(
        db: Session,
        actor: str,
        run: AgentRun,
        version: UnitVersion,
        review: ScopedReviewOutput,
        coverage: ScopedCoverageOutput,
    ) -> list[dict[str, Any]]:
        """Bind scoped authority to actual completed role outputs, never caller assertions."""
        result = []
        final_key = "review.final:" + version.id
        correction = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id,
                ModelReceipt.stage_key == final_key + ":quote-correction:1",
            )
        )
        keys: list[tuple[str, str, ScopedReviewOutput | ScopedCoverageOutput | None]] = [
            (final_key, "reviewer", None if correction else review)
        ]
        if correction is not None:
            keys.append((final_key + ":quote-correction:1", "reviewer", review))
        keys.append(("coverage:" + version.id, "coverage", coverage))
        for key, role, output in keys:
            receipt = db.scalar(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run.id, ModelReceipt.stage_key == key
                )
            )
            if (
                receipt is None
                or receipt.status != "completed"
                or receipt.response is None
                or receipt.role != role
                or receipt.payer_id != run.principal_id
                or receipt.profile_version != run.input_manifest.get("model_profile")
            ):
                raise DomainError("MODEL_RESULT_UNCERTAIN", "部分审核缺少已完成的精确模型回执。")
            if receipt.response.get("synthetic") is not False and not version.synthetic:
                raise DomainError(
                    "SYNTHETIC_PUBLICATION", "合成或不明审核不能批准真实来源的部分发布。", 403
                )
            if output is not None:
                receipt_structured = receipt.response.get("structured", {})
                if key == final_key + ":quote-correction:1" and run.input_manifest.get(
                    "review_quote_patch"
                ) in {"reference-patch-v1", "read-line-selection-v2"}:
                    from .quote_correction import replay_review_quote_patches

                    receipt_structured = replay_review_quote_patches(
                        db, run, final_key, receipt_structured
                    )
                structured = {
                    k: v
                    for k, v in receipt_structured.items()
                    if k != "task_coverage"
                    and not (
                        k == "evidence_read_requests"
                        and role == "reviewer"
                        and run.input_manifest.get("evidence_retry_contract")
                        == EVIDENCE_RETRY_CONTRACT
                    )
                }
                try:
                    parsed = (type(output)).model_validate(structured)
                except ValidationError as exc:
                    raise DomainError(
                        "SCOPED_REVIEW_BINDING", "部分审核的模型回执输出损坏。"
                    ) from exc
                if parsed.model_dump(mode="json") != output.model_dump(mode="json"):
                    raise DomainError("SCOPED_REVIEW_BINDING", "部分审核输出与原模型回执不一致。")
            witness = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id,
                    StageArtifact.stage_key == "model-access:" + receipt.id,
                )
            )
            if (
                witness is None
                or witness.input_hash != run.input_hash
                or witness.payload.get("request_hash") != receipt.request_hash
                or witness.payload.get("receipt_id") != receipt.id
            ):
                raise DomainError("SCOPED_REVIEW_BINDING", "部分审核缺少原请求权限见证。")
            roots = witness.payload.get("access_dependencies", [])
            if version.id not in roots:
                raise DomainError("SCOPED_REVIEW_BINDING", "部分审核请求未绑定原候选。")
            check_closure(db, actor, roots)
            result.append(
                {
                    "receipt_id": receipt.id,
                    "stage_key": key,
                    "request_hash": receipt.request_hash,
                    "response_hash": fingerprint(receipt.response),
                }
            )
        return result

    def _update_scope_in(
        self, db: Session, actor: str, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if scope is None:
            return None
        from ..models_governance import SourceUpdatePlan

        plan = db.get(SourceUpdatePlan, scope.get("plan_id"))
        if plan is None:
            raise DomainError("UPDATE_SCOPE", "更新复核计划不存在。")
        namespace_access(db, actor, plan.namespace_id)
        if (
            plan.state != "scheduled"
            or plan.content_hash != fingerprint(plan.manifest)
            or scope.get("plan_hash") != plan.content_hash
            or scope.get("old_source_id") != plan.old_source_id
            or scope.get("new_source_id") != plan.new_source_id
            or scope.get("changes") != plan.manifest["changes"]
            or scope.get("prior_approval_is_not_new_evidence") is not True
            or not isinstance(scope.get("units"), list)
            or scope["units"] != plan.manifest["units"][: len(scope["units"])]
        ):
            raise DomainError("UPDATE_SCOPE", "更新复核范围不符合冻结计划。")
        SourceService.get(db, actor, plan.old_source_id)
        SourceService.get(db, actor, plan.new_source_id)
        for unit in scope["units"]:
            self.publication_pack(db, actor, unit["publication_id"])
        return scope

    def validated_update_scope(
        self, actor: str, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Reauthorize old context immediately before it is sent to a reviewer/model."""
        with self.db.read() as db:
            return self._update_scope_in(db, actor, scope)

    def save_analysis(
        self,
        actor: str,
        run_id: str,
        epoch: int,
        worker: str,
        source_id: str,
        session_key: str,
        output: AnalysisOutput,
        synthetic: bool,
    ) -> list[str]:
        key = "drafts:" + source_id
        with self.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            if (
                run.principal_id != actor
                or run.workflow != "analysis"
                or source_id not in run.input_manifest.get("source_ids", [])
            ):
                raise DomainError("ANALYSIS_AUTHORITY", "分析范围或执行身份不一致。", 403)
            old = self.runtime.stage(run_id, key)
            if old:
                check_closure(db, actor, old["unit_version_ids"])
                return old["unit_version_ids"]
            source = SourceService.get(db, actor, source_id)
            update_scope = self._update_scope_in(db, actor, run.input_manifest.get("update_scope"))
            if update_scope:
                if source_id != update_scope["new_source_id"]:
                    raise DomainError("UPDATE_SCOPE", "更新复核不能切换到别的来源。")
                for target in update_scope["units"]:
                    if target["route"] != "B_targeted_review":
                        continue
                    matching = [
                        unit
                        for unit in output.units
                        if unit.subject_key == target["subject_key"]
                        and unit.unit_type == target["unit_type"]
                    ]
                    expected = {
                        fingerprint(
                            {
                                "statement": claim["statement"],
                                "conditions": claim["conditions"],
                                "kind": claim["kind"],
                            }
                        )
                        for claim in target["claims_to_recheck"]
                    }
                    observed = {
                        fingerprint(
                            {
                                "statement": claim.statement,
                                "conditions": claim.conditions,
                                "kind": claim.kind,
                            }
                        )
                        for unit in matching
                        for claim in unit.claims
                    }
                    if not expected <= observed:
                        raise DomainError(
                            "UPDATE_RECHECK_SCOPE",
                            "局部复核必须核验原命题；改写命题需要新的完整调查。",
                            422,
                        )
            output_namespace = run.namespace_id
            ids = []
            for unit in output.units:
                frozen_topics = run.input_manifest.get("taxonomy_versions")
                topic_keys = {link.topic_key for link in unit.topics}
                if frozen_topics is not None and not topic_keys <= set(frozen_topics):
                    raise DomainError("TOPIC_UNREGISTERED", "主题不在本次冻结的分类发布中。", 422)
                facet_keys = {(facet.topic_key, facet.facet_key) for facet in unit.facets}
                if len(facet_keys) != len(unit.facets):
                    raise DomainError("FACET_DUPLICATE", "同一主题维度不能有多个冲突投影。", 422)
                if any(facet.topic_key not in topic_keys for facet in unit.facets):
                    raise DomainError("FACET_TOPIC", "维度必须属于本单元已声明的主题。", 422)
                for topic_key in topic_keys:
                    view = schema_view(
                        db,
                        actor,
                        topic_key,
                        frozen_topics.get(topic_key) if frozen_topics is not None else None,
                    )
                    required = {
                        (topic_key, key)
                        for key, rule in view["facets"].items()
                        if rule.get("required")
                    }
                    if not required <= facet_keys:
                        raise DomainError(
                            "FACET_COVERAGE", "必需维度必须明确填写值或未知状态。", 422
                        )
                if source.origin_kind == "concept" and unit.unit_type != "concept":
                    raise DomainError("UNIT_ORIGIN", "概念来源只能生成概念教学单元。", 422)
                if source.origin_kind == "repository" and unit.unit_type == "concept":
                    raise DomainError("UNIT_ORIGIN", "项目观察不能冒充独立概念教材。", 422)
                if any(c.kind == "runtime_observation" for c in unit.claims):
                    raise DomainError(
                        "NO_RUNTIME_OBSERVATION", "静态源码分析不能创建真实运行观察。", 422
                    )
                unit_identity = db.scalar(
                    select(KnowledgeUnit)
                    .where(
                        KnowledgeUnit.namespace_id == output_namespace,
                        KnowledgeUnit.origin_kind == source.origin_kind,
                        KnowledgeUnit.origin_id == source.repository_key,
                        KnowledgeUnit.unit_type == unit.unit_type,
                        KnowledgeUnit.subject_key == unit.subject_key,
                        KnowledgeUnit.applicability_key == digest(unit.applicability),
                    )
                    .with_for_update()
                )
                if unit_identity is None:
                    unit_identity = KnowledgeUnit(
                        namespace_id=output_namespace,
                        origin_kind=source.origin_kind,
                        origin_id=source.repository_key,
                        unit_type=unit.unit_type,
                        subject_key=unit.subject_key,
                        applicability_key=digest(unit.applicability),
                    )
                    db.add(unit_identity)
                    db.flush()
                revision = unit_identity.next_revision
                unit_identity.next_revision += 1
                claims = {c.key: uid() for c in unit.claims}
                facts = {c.key: uid() for c in unit.claims}
                render_ids = {c.key: uid() for c in unit.claims}
                render = [
                    {
                        "element_id": render_ids[c.key],
                        "kind": "fact",
                        "fact_atom_id": facts[c.key],
                        "text": c.statement,
                        "conditions": c.conditions,
                    }
                    for c in unit.claims
                ]
                paths = sorted(
                    set(unit.dependency_paths + [r["path"] for r in self._read_paths(db, unit)])
                )
                for path in paths:
                    if path not in source.files:
                        raise DomainError("DEPENDENCY_PATH", "依赖路径不属于当前快照。", 422)
                dependency_manifest = {
                    "paths": {p: digest(source.files[p]) for p in paths},
                    "closure": "partial",
                    "scope_hash": source.scope_hash,
                }
                obj = register(
                    db,
                    actor,
                    output_namespace,
                    "knowledge_unit_version",
                    {
                        "unit_id": unit_identity.id,
                        "source_id": source.id,
                        "draft": unit.model_dump(mode="json"),
                        "render": render,
                        "dependency_manifest": dependency_manifest,
                        "synthetic": synthetic or bool(source.metadata_json.get("synthetic")),
                    },
                )
                version = UnitVersion(
                    id=obj.id,
                    unit_id=unit_identity.id,
                    revision_no=revision,
                    source_id=source.id,
                    title=unit.title,
                    content_hash=fingerprint(unit.model_dump(mode="json")),
                    render_hash=fingerprint(render),
                    analysis_run_id=run.id,
                    render_ast=render,
                    dependency_manifest=dependency_manifest,
                    limitations=unit.limitations,
                    synthetic=synthetic or bool(source.metadata_json.get("synthetic")),
                )
                db.add(version)
                db.flush()
                depend(db, actor, version.id, source.id, "grounded_in")
                for draft in unit.claims:
                    supports = [
                        SourceService.evidence_from_read(
                            db,
                            actor,
                            output_namespace,
                            source.id,
                            ref.read_id,
                            ref.quote,
                            "analysis",
                            session_key,
                            run.id,
                        )
                        for ref in draft.support
                    ]
                    counters = [
                        SourceService.evidence_from_read(
                            db,
                            actor,
                            output_namespace,
                            source.id,
                            ref.read_id,
                            ref.quote,
                            "analysis",
                            session_key,
                            run.id,
                        )
                        for ref in draft.counterevidence
                    ]
                    register(
                        db,
                        actor,
                        output_namespace,
                        "claim",
                        {
                            "statement": draft.statement,
                            "conditions": draft.conditions,
                            "support_refs": supports,
                            "counterevidence_refs": counters,
                        },
                        claims[draft.key],
                    )
                    db.add(
                        Claim(
                            id=claims[draft.key],
                            unit_version_id=version.id,
                            statement=draft.statement,
                            conditions=draft.conditions,
                            criticality="blocking",
                            kind=draft.kind,
                        )
                    )
                    db.flush()
                    for evidence_id in [*supports, *counters]:
                        depend(db, actor, claims[draft.key], evidence_id, "requires_evidence")
                    register(
                        db,
                        actor,
                        output_namespace,
                        "fact_atom",
                        {
                            "claim_id": claims[draft.key],
                            "text": draft.statement,
                            "conditions": draft.conditions,
                        },
                        facts[draft.key],
                    )
                    db.add(
                        FactAtom(
                            id=facts[draft.key],
                            unit_version_id=version.id,
                            claim_id=claims[draft.key],
                            approved_text=draft.statement,
                            conditions=draft.conditions,
                        )
                    )
                    depend(db, actor, facts[draft.key], claims[draft.key], "requires_claim")
                    register(
                        db,
                        actor,
                        output_namespace,
                        "render_element",
                        render[[c.key for c in unit.claims].index(draft.key)],
                        render_ids[draft.key],
                    )
                    depend(db, actor, render_ids[draft.key], facts[draft.key], "renders_fact")
                    depend(db, actor, version.id, render_ids[draft.key], "contains_fact")
                for link in unit.topics:
                    frozen_version = run.input_manifest.get("taxonomy_versions", {}).get(
                        link.topic_key
                    )
                    topic = require_topic(db, actor, link.topic_key, frozen_version)
                    topic_version = frozen_version or topic.version
                    lo = register(
                        db, actor, output_namespace, "topic_link", link.model_dump(mode="json")
                    )
                    db.add(
                        TopicLink(
                            id=lo.id,
                            unit_version_id=version.id,
                            topic_id=topic.id,
                            relation=link.relation,
                        )
                    )
                    depend(
                        db, actor, lo.id, topic_object_id(topic.key, topic_version), "classified_by"
                    )
                for facet in unit.facets:
                    frozen_version = run.input_manifest.get("taxonomy_versions", {}).get(
                        facet.topic_key
                    )
                    topic = require_topic(db, actor, facet.topic_key, frozen_version)
                    schema = schema_view(db, actor, facet.topic_key, frozen_version)
                    if facet.facet_key not in schema["facets"]:
                        raise DomainError(
                            "FACET_UNREGISTERED", "比较维度不属于已发布 Schema。", 422
                        )
                    spec = schema["facets"][facet.facet_key]
                    predicates: list[bool | None] = []
                    for condition in spec.get("preconditions", []):
                        if not isinstance(condition, dict):
                            raise DomainError("FACET_RULE", "未注册的适用规则不能执行。", 422)
                        name, operand = condition.get("predicate"), condition.get("operand")
                        if name == "origin_kind_is":
                            predicates.append(source.origin_kind == operand)
                        elif name == "unit_type_is":
                            predicates.append(unit.unit_type == operand)
                        elif name == "path_present":
                            # A scoped import can prove presence, not repository-wide absence.
                            predicates.append(True if operand in source.files else None)
                        else:
                            raise DomainError("FACET_RULE", "未注册的适用规则不能执行。", 422)
                    applies = False if False in predicates else None if None in predicates else True
                    if predicates and (
                        (facet.value_status == "known" and applies is not True)
                        or (facet.value_status == "not_applicable" and applies is not False)
                    ):
                        raise DomainError(
                            "FACET_APPLICABILITY", "证据适用前提尚未确认或已被反证。", 422
                        )
                    if facet.value_status == "known":
                        value_type = spec.get("value_type", "string")
                        valid = {
                            "string": isinstance(facet.value, str),
                            "enum": isinstance(facet.value, str),
                            "number": type(facet.value) in {int, float},
                            "boolean": type(facet.value) is bool,
                            "object": isinstance(facet.value, dict),
                        }.get(value_type, False)
                        if not valid:
                            raise DomainError(
                                "FACET_VALUE_TYPE", "比较值与冻结Schema类型不符。", 422
                            )
                        if value_type == "enum" and facet.value not in spec.get("enum_values", []):
                            raise DomainError("FACET_VALUE_TYPE", "比较值不在已批准枚举中。", 422)
                        if value_type == "object":
                            fields = spec.get("object_fields", {})
                            if (
                                not isinstance(facet.value, dict)
                                or not fields
                                or set(facet.value) != set(fields)
                            ):
                                raise DomainError(
                                    "FACET_VALUE_TYPE", "对象维度必须匹配明确批准的字段。", 422
                                )
                            for field, field_type in fields.items():
                                item = facet.value[field]
                                if not {
                                    "string": isinstance(item, str),
                                    "number": type(item) in {int, float},
                                    "boolean": type(item) is bool,
                                }.get(field_type, False):
                                    raise DomainError(
                                        "FACET_VALUE_TYPE", "对象维度字段类型不符。", 422
                                    )
                    if facet.value_status == "conflicting" and len(set(facet.claim_keys)) < 2:
                        raise DomainError(
                            "FACET_SUPPORT", "冲突状态需要至少两条受审核的冲突断言。", 422
                        )
                    if (
                        facet.value_status == "not_applicable"
                        and not facet.applicability_reason.strip()
                    ):
                        raise DomainError(
                            "FACET_APPLICABILITY", "不适用需要明确的受审核条件说明。", 422
                        )
                    if facet.value_status == "not_applicable" and not facet.claim_keys:
                        raise DomainError(
                            "FACET_APPLICABILITY", "不适用必须有受审核的条件反证。", 422
                        )
                    if (
                        facet.value_status == "not_applicable"
                        and spec.get("applicability") == "always"
                    ):
                        raise DomainError(
                            "FACET_APPLICABILITY", "该维度适用，不能用不适用隐藏缺口。", 422
                        )
                    if facet.value_status == "known" and (
                        not facet.claim_keys or facet.value is None
                    ):
                        raise DomainError("FACET_SUPPORT", "已知比较值必须有支持断言。", 422)
                    if facet.value_status != "known" and facet.value is not None:
                        raise DomainError("FACET_UNKNOWN", "未知/不适用不能伪填比较值。", 422)
                    fo = register(
                        db, actor, output_namespace, "facet_value", facet.model_dump(mode="json")
                    )
                    claim_ids = [claims[key] for key in facet.claim_keys]
                    db.add(
                        FacetValue(
                            id=fo.id,
                            unit_version_id=version.id,
                            topic_id=topic.id,
                            schema_version=schema["version"],
                            facet_key=facet.facet_key,
                            value_status=facet.value_status,
                            value=facet.value,
                            claim_ids=claim_ids,
                            required=bool(spec.get("required")),
                        )
                    )
                    depend(db, actor, fo.id, schema["facet_schema_id"], "conforms_to")
                    for claim_id in claim_ids:
                        depend(db, actor, fo.id, claim_id, "requires_claim")
                ids.append(version.id)
            self.runtime.save_stage_in(db, run, key, {"unit_version_ids": ids})
            return ids

    @staticmethod
    def _read_paths(db: Session, unit: UnitDraft) -> list[dict[str, Any]]:
        from ..models import SourceRead

        refs = [x.read_id for c in unit.claims for x in c.support + c.counterevidence]
        return [
            {"path": r.path} for r in db.scalars(select(SourceRead).where(SourceRead.id.in_(refs)))
        ]

    @staticmethod
    def _topic_link_view(db: Session, actor: str, link: TopicLink) -> dict[str, Any]:
        check_closure(db, actor, [link.id])
        topic = _required_record(db, Topic, link.topic_id)
        payload = require_object(db, actor, link.id, "topic_link").payload
        refs = list(
            db.scalars(
                select(ArtifactDependency.source_id).where(
                    ArtifactDependency.dependent_id == link.id,
                    ArtifactDependency.kind == "classified_by",
                    ArtifactDependency.enforcement == "blocking",
                    ArtifactDependency.source_type == "topic_version",
                )
            )
        )
        if len(refs) != 1 or payload != {"topic_key": topic.key, "relation": link.relation}:
            raise DomainError("TOPIC_LINK_HASH", "主题关联缺少唯一冻结版本。")
        version = require_object(db, actor, refs[0], "topic_version")
        number = version.payload.get("version", 1)
        if refs[0] != topic_object_id(topic.key, number):
            raise DomainError("TOPIC_LINK_HASH", "主题关联版本不匹配。")
        return {
            "id": link.id,
            "topic_key": topic.key,
            "relation": link.relation,
            "topic_version": number,
            "topic_version_id": refs[0],
        }

    def review_subject(
        self, actor: str, unit_version_id: str, show_evidence: bool = False, *, scoped: bool = False
    ) -> dict[str, Any]:
        with self.db.read() as db:
            check_closure(db, actor, [unit_version_id])
            v = self._validated_version(db, actor, unit_version_id)
            source = SourceService.get(db, actor, v.source_id)
            claims = db.scalars(select(Claim).where(Claim.unit_version_id == v.id)).all()
            value: dict[str, Any] = {
                "unit_version_id": v.id,
                "source_id": v.source_id,
                "title": v.title,
                "content_hash": v.content_hash,
                "render_hash": v.render_hash,
                "repository": source.repository_key,
                "commit": source.commit_sha,
                "claims": [
                    {
                        "claim_id": c.id,
                        "statement": c.statement,
                        "conditions": c.conditions,
                        "criticality": c.criticality,
                    }
                    for c in claims
                ],
            }
            if scoped:
                vo = require_object(db, actor, v.id, "knowledge_unit_version")
                value["scope_identity"] = {
                    key: value[key] for key in ("unit_version_id", "content_hash", "render_hash")
                }
                value["limitations"] = list(v.limitations)
                value["applicability"] = vo.payload["draft"]["applicability"]
                by_id = {claim.id: claim for claim in claims}
                for item in value["claims"]:
                    item["kind"] = by_id[item["claim_id"]].kind
                if show_evidence:
                    value["scope_elements"] = self._scope_subject_in(db, actor, v)["elements"]
                    # The roles must approve each unchanged claim in its actual
                    # neutral standalone view, not borrow premises from removed
                    # unit prose, annotations, or another candidate claim.
                    value["standalone_publication_views"] = [
                        {
                            "title": PARTIAL_TITLE,
                            "source_id": source.id,
                            "repository": source.repository_key,
                            "commit": source.commit_sha,
                            "facts": [
                                {
                                    "fact_atom_id": element["fact_atom_id"],
                                    "claim_id": element["claim_id"],
                                    "text": element["text"],
                                    "conditions": element["conditions"],
                                    "kind": by_id[element["claim_id"]].kind,
                                }
                            ],
                            "topic_links": [],
                            "facets": [],
                            "limitations": list(PARTIAL_LIMITATIONS),
                            "runtime_observed": False,
                        }
                        for element in value["scope_elements"]
                    ]
            if show_evidence:
                value["render_ast"] = v.render_ast
                analysis_evidence: list[dict[str, Any]] = []
                for claim in claims:
                    claim_payload = require_object(db, actor, claim.id, "claim").payload
                    analysis_evidence.append(
                        {
                            "claim_id": claim.id,
                            "support": [
                                self.evidence_dict(db, actor, e)
                                for e in claim_payload["support_refs"]
                            ],
                            "counterevidence": [
                                self.evidence_dict(db, actor, e)
                                for e in claim_payload["counterevidence_refs"]
                            ],
                        }
                    )
                value["analysis_evidence"] = analysis_evidence
                value["topic_links"] = [
                    self._topic_link_view(db, actor, link)
                    for link in db.scalars(
                        select(TopicLink).where(TopicLink.unit_version_id == v.id)
                    )
                ]
                facet_subjects = []
                for facet in db.scalars(
                    select(FacetValue).where(FacetValue.unit_version_id == v.id)
                ):
                    check_closure(db, actor, [facet.id])
                    payload = require_object(db, actor, facet.id, "facet_value").payload
                    topic = _required_record(db, Topic, facet.topic_id)
                    frozen = schema_view(db, actor, topic.key, facet.schema_version)
                    if facet.facet_key not in frozen["facets"]:
                        raise DomainError("FACET_HASH", "审核维度缺少冻结Schema规则。")
                    facet_subjects.append(
                        {
                            "id": facet.id,
                            "topic_key": topic.key,
                            "facet_key": facet.facet_key,
                            "schema_version": facet.schema_version,
                            "schema_rule": frozen["facets"][facet.facet_key],
                            "facet_schema_id": frozen["facet_schema_id"],
                            "value": facet.value,
                            "value_status": facet.value_status,
                            "claim_ids": facet.claim_ids,
                            "applicability_reason": payload.get("applicability_reason", ""),
                        }
                    )
                value["facets"] = facet_subjects
            return value

    @staticmethod
    def evidence_dict(db: Session, actor: str, evidence_id: str) -> dict[str, Any]:
        obj = require_object(db, actor, evidence_id, "evidence")
        evidence = db.get(Evidence, evidence_id)
        if evidence is None:
            raise DomainError("EVIDENCE_HASH", "证据记录不存在。")
        read, source = SourceService.validated_read(db, actor, evidence.read_id)
        binding = {
            "source_id": evidence.source_id,
            "read_id": evidence.read_id,
            "path": evidence.path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "quote_hash": evidence.quote_hash,
        }
        if (
            binding != obj.payload
            or read.source_id != evidence.source_id
            or read.path != evidence.path
            or evidence.file_hash != read.file_hash
            or evidence.quote_hash != digest(evidence.exact_quote)
            or not evidence.exact_quote
            or len(quote_occurrences(read.text, evidence.exact_quote)) != 1
        ):
            raise DomainError("EVIDENCE_HASH", "证据内容与固定来源不一致。")
        position = read.text.index(evidence.exact_quote)
        start = read.first_line + read.text[:position].count("\n")
        end = start + evidence.exact_quote.rstrip("\n").count("\n")
        if (evidence.start_line, evidence.end_line) != (start, end):
            raise DomainError("EVIDENCE_HASH", "证据内容与固定来源不一致。")
        return {
            "citation_id": evidence.id,
            "source_id": source.id,
            "repository": source.repository_key,
            "commit": source.commit_sha,
            "path": evidence.path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "quote": evidence.exact_quote,
            "file_hash": evidence.file_hash,
            "quote_hash": evidence.quote_hash,
            "read_id": evidence.read_id,
        }

    def save_review(
        self,
        actor: str,
        run_id: str,
        epoch: int,
        worker: str,
        unit_version_id: str,
        session_key: str,
        independent_bundle: dict[str, Any],
        review: ReviewOutput,
        coverage: CoverageOutput,
    ) -> str:
        key = "review:" + unit_version_id
        with self.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            if (
                run.principal_id != actor
                or run.workflow != "review"
                or unit_version_id not in run.input_manifest.get("unit_version_ids", [])
            ):
                raise DomainError("REVIEW_AUTHORITY", "审核范围或执行身份不一致。", 403)
            old = self.runtime.stage(run_id, key)
            if old:
                check_closure(db, actor, [old["review_id"]])
                return old["review_id"]
            v = self._validated_version(db, actor, unit_version_id)
            if v.analysis_run_id == run.id:
                raise DomainError(
                    "INDEPENDENCE", "Analysis 与 Reviewer 必须使用隔离的角色运行合同。"
                )
            claims = db.scalars(select(Claim).where(Claim.unit_version_id == v.id)).all()
            expected = {c.id for c in claims}
            actual = [c.claim_id for c in review.claim_decisions]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise DomainError("REVIEW_COVERAGE", "审核必须逐项覆盖所有断言。", 422)
            initial_ids = {r["read_id"] for r in independent_bundle["reads"]}
            decisions: list[dict[str, Any]] = []
            for claim_review in review.claim_decisions:
                if (
                    claim_review.outcome == "supported_within_scope"
                    and not claim_review.independent_reads
                ):
                    raise DomainError(
                        "INDEPENDENT_READ_REQUIRED", "接受断言需要独立读取原文。", 422
                    )
                if (
                    not {
                        r.read_id
                        for r in claim_review.independent_reads + claim_review.counterevidence
                    }
                    <= initial_ids
                ):
                    raise DomainError("INDEPENDENT_BUNDLE", "审核引用不属于冻结的独立证据包。", 422)
                refs = [
                    SourceService.evidence_from_read(
                        db,
                        actor,
                        run.namespace_id,
                        v.source_id,
                        r.read_id,
                        r.quote,
                        "reviewer",
                        session_key,
                        run.id,
                    )
                    for r in claim_review.independent_reads
                ]
                counters = [
                    SourceService.evidence_from_read(
                        db,
                        actor,
                        run.namespace_id,
                        v.source_id,
                        r.read_id,
                        r.quote,
                        "reviewer",
                        session_key,
                        run.id,
                    )
                    for r in claim_review.counterevidence
                ]
                decisions.append(
                    {
                        **claim_review.model_dump(mode="json"),
                        "independent_evidence_ids": refs,
                        "counterevidence_ids": counters,
                    }
                )
            links = {
                link.id
                for link in db.scalars(select(TopicLink).where(TopicLink.unit_version_id == v.id))
            }
            facets = {
                f.id
                for f in db.scalars(select(FacetValue).where(FacetValue.unit_version_id == v.id))
            }
            if (
                not set(review.accepted_topic_link_ids) <= links
                or not set(review.accepted_facet_ids) <= facets
            ):
                raise DomainError("REVIEW_RELATION", "审核引用了错误版本的主题/维度。", 422)
            accept = (
                review.decision == "accept_scoped"
                and not review.blocking_issues
                and all(c.outcome == "supported_within_scope" for c in review.claim_decisions)
                and not any(c.counterevidence for c in review.claim_decisions)
                and set(review.accepted_topic_link_ids) == links
                and set(review.accepted_facet_ids) == facets
                and coverage.all_facts_bound
                and coverage.conditions_preserved
                and not coverage.uncovered_elements
                and not coverage.unsupported_titles
            )
            publication_decision = (
                "accept_scoped"
                if accept
                else "reject"
                if review.decision == "reject"
                else "needs_evidence"
            )
            scoped = scoped_review_enabled(run.input_manifest)
            scope_fields: dict[str, Any] = {}
            if scoped:
                scoped_review = ScopedReviewOutput.model_validate(review.model_dump(mode="json"))
                scoped_coverage = self._coverage_type(run).model_validate(
                    coverage.model_dump(mode="json")
                )
                subject = self._scope_subject_in(db, actor, v)
                partition = self._review_partition(
                    db, actor, run, v, subject, scoped_review, scoped_coverage
                )
                # New associations are licensed only for the reviewed subset.
                # Whole-unit claims require the separate legacy full-scope path.
                if isinstance(scoped_coverage, RelationScopedCoverageOutput):
                    accept = False
                    if review.decision != "reject":
                        publication_decision = "needs_evidence"
                if (
                    scoped_review.scope_review and scoped_review.scope_review.global_blocking_issues
                ) or (
                    scoped_coverage.scope_audit
                    and scoped_coverage.scope_audit.global_blocking_issues
                ):
                    accept = False
                    publication_decision = (
                        "reject" if review.decision == "reject" else "needs_evidence"
                    )
                # Full-unit acceptance retains its original stricter semantics.
                # Missing new scope fields can never open the partial branch.
                if not accept and partition["status"] == "partial":
                    frozen_bundle = self._scoped_bundle_in(db, actor, run, v)
                    if fingerprint(frozen_bundle) != fingerprint(independent_bundle):
                        raise DomainError("INDEPENDENT_BUNDLE", "部分审核未使用冻结的独立证据包。")
                    scope_fields = {
                        "scoped_original_review": scoped_review.model_dump(mode="json"),
                        "scope_derivation": partition,
                        "scoped_receipts": self._scoped_receipts_in(
                            db, actor, run, v, scoped_review, scoped_coverage
                        ),
                    }
                    publication_decision = "accept_partial_scoped"
                scope_fields["knowledge_review_contract_version"] = SCOPED_REVIEW_VERSION
            elif isinstance(review, ScopedReviewOutput) or isinstance(
                coverage, ScopedCoverageOutput
            ):
                raise DomainError("SCOPED_REVIEW_CONTRACT", "历史审核不能改用新的部分发布合同。")
            report: dict[str, Any] = {
                **review.model_dump(mode="json"),
                "claim_decisions": decisions,
                "fact_coverage": coverage.model_dump(mode="json"),
                "decision": publication_decision,
                "independent_bundle_hash": fingerprint(independent_bundle),
                **scope_fields,
            }
            obj = register(db, actor, run.namespace_id, "review_decision", report)
            db.add(
                ReviewDecision(
                    id=obj.id,
                    unit_version_id=v.id,
                    reviewer_run_id=run.id,
                    independent_bundle_hash=fingerprint(independent_bundle),
                    reviewed_content_hash=v.content_hash,
                    reviewed_render_hash=v.render_hash,
                    decision=publication_decision,
                    report=report,
                )
            )
            depend(db, actor, obj.id, v.id, "reviewed_version", "provenance_only")
            for claim_id in expected:
                depend(db, actor, obj.id, claim_id, "reviewed_claim", "provenance_only")
            for claim_decision in decisions:
                for evidence_id in (
                    claim_decision["independent_evidence_ids"]
                    + claim_decision["counterevidence_ids"]
                ):
                    depend(db, actor, obj.id, evidence_id, "requires_evidence")
            self.runtime.save_stage_in(
                db, run, key, {"review_id": obj.id, "decision": publication_decision}
            )
            return obj.id

    def publish(
        self, actor: str, run_id: str, epoch: int, worker: str, review_id: str
    ) -> str | None:
        with self.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            require_object(db, actor, review_id, "review_decision")
            review = _required_record(db, ReviewDecision, review_id)
            version = self._validated_version(db, actor, review.unit_version_id)
            review = self.validated_review(db, actor, review_id, version)
            identity = _required_record(db, KnowledgeUnit, version.unit_id)
            reviewer_run = _required_record(db, AgentRun, review.reviewer_run_id)
            analysis_run = _required_record(db, AgentRun, version.analysis_run_id)
            if (
                run.principal_id != actor
                or run.workflow != "knowledge_build"
                or reviewer_run.workflow != "review"
                or reviewer_run.parent_id != run.id
                or reviewer_run.status != "succeeded"
                or analysis_run.workflow != "analysis"
                or analysis_run.parent_id != run.id
                or analysis_run.status != "succeeded"
                or run.namespace_id != identity.namespace_id
                or run.id == review.reviewer_run_id
            ):
                raise DomainError(
                    "PUBLICATION_AUTHORITY", "审核子任务不得自行发布，且发布范围必须一致。", 403
                )
            if review.decision not in {"accept_scoped", "accept_partial_scoped"}:
                return None
            scope = (
                review.report.get("scope_derivation")
                if review.decision == "accept_partial_scoped"
                else None
            )
            old = db.scalar(select(Publication).where(Publication.unit_version_id == version.id))
            if old:
                self.publication_pack(db, actor, old.id)
                return old.id
            if version.synthetic and not self.db.settings.allow_fixture_publication:
                raise DomainError(
                    "SYNTHETIC_PUBLICATION", "合成 Provider 的结果不能作为生产知识发布。", 403
                )
            if (
                review.reviewed_content_hash != version.content_hash
                or review.reviewed_render_hash != fingerprint(version.render_ast)
            ):
                raise DomainError("REVIEW_HASH", "审核版本与发布内容不一致。")
            check_closure(db, actor, [version.id, review.id])
            SourceService.get(db, actor, version.source_id)
            facts = db.scalars(select(FactAtom).where(FactAtom.unit_version_id == version.id)).all()
            if {r["fact_atom_id"] for r in version.render_ast} != {f.id for f in facts}:
                raise DomainError("FACT_COVERAGE", "正文事实与 FactAtom 不一致。")
            facts_by_id = {f.id: f for f in facts}
            for element in version.render_ast:
                fact = facts_by_id[element["fact_atom_id"]]
                if (
                    element["text"] != fact.approved_text
                    or element["conditions"] != fact.conditions
                ):
                    raise DomainError("FACT_DRIFT", "发布正文改变了已审命题或条件。")
            if scope is not None:
                facts = [fact for fact in facts if fact.id in scope["retained_fact_ids"]]
            obj = register(
                db,
                actor,
                run.namespace_id,
                "publication",
                {
                    "unit_version_id": version.id,
                    "review_id": review.id,
                    "content_hash": version.content_hash,
                    "render_hash": version.render_hash,
                    **({"scope_derivation": scope} if scope is not None else {}),
                },
            )
            pub = Publication(
                id=obj.id,
                unit_version_id=version.id,
                review_id=review.id,
                namespace_id=run.namespace_id,
            )
            db.add(pub)
            db.flush()
            depend(db, actor, pub.id, version.id, "publishes_version")
            depend(db, actor, pub.id, review.id, "requires_review")
            for fact in facts:
                depend(db, actor, pub.id, fact.id, "requires_fact")
            for link in db.scalars(
                select(TopicLink).where(TopicLink.unit_version_id == version.id)
            ):
                if scope is None or link.id in scope.get("retained_topic_link_ids", []):
                    depend(db, actor, pub.id, link.id, "classified_by", "impact_only")
            for facet in db.scalars(
                select(FacetValue).where(FacetValue.unit_version_id == version.id)
            ):
                if scope is not None and facet.id not in scope.get("retained_facet_ids", []):
                    continue
                depend(
                    db,
                    actor,
                    pub.id,
                    facet.id,
                    "uses_facet",
                    "blocking" if facet.required else "impact_only",
                )
            scope_key = version.source_id if identity.origin_kind == "repository" else "concept"
            head = db.scalar(
                select(SelectionHead).where(
                    SelectionHead.selection_namespace_id == run.namespace_id,
                    SelectionHead.unit_id == identity.id,
                    SelectionHead.selection_context == "default",
                    SelectionHead.scope_key == scope_key,
                )
            )
            if not head:
                self._set_head(db, actor, run.namespace_id, pub.id, 0)
            if identity.origin_kind == "repository":
                project = db.scalar(
                    select(ProjectSelection).where(
                        ProjectSelection.namespace_id == run.namespace_id,
                        ProjectSelection.repository_key == identity.origin_id,
                    )
                )
                if not project:
                    db.add(
                        ProjectSelection(
                            namespace_id=run.namespace_id,
                            repository_key=identity.origin_id,
                            source_id=version.source_id,
                        )
                    )
            locked(db, Namespace, run.namespace_id).revision += 1
            db.add(Outbox(kind="index", aggregate_id=pub.id, payload={}))
            return pub.id

    def _set_head(
        self,
        db: Session,
        actor: str,
        selection_ns: str,
        publication_id: str,
        expected_revision: int,
    ) -> str:
        namespace_access(db, actor, selection_ns, True)
        require_object(db, actor, publication_id, "publication")
        check_closure(db, actor, [publication_id])
        pub = _required_record(db, Publication, publication_id)
        v = self._validated_version(db, actor, pub.unit_version_id)
        unit = _required_record(db, KnowledgeUnit, v.unit_id)
        scope = v.source_id if unit.origin_kind == "repository" else "concept"
        head = db.scalar(
            select(SelectionHead)
            .where(
                SelectionHead.selection_namespace_id == selection_ns,
                SelectionHead.unit_id == unit.id,
                SelectionHead.selection_context == "default",
                SelectionHead.scope_key == scope,
            )
            .with_for_update()
        )
        if head:
            if head.revision != expected_revision:
                raise DomainError("HEAD_CONFLICT", "默认知识版本已变化。")
            obj = require_object(db, actor, head.id, active=False)
            db.execute(
                delete(ArtifactDependency).where(
                    ArtifactDependency.dependent_id == head.id, ArtifactDependency.kind == "selects"
                )
            )
            head.publication_id = publication_id
            head.revision += 1
            obj.payload = {"publication_id": publication_id, "unit_id": unit.id, "scope_key": scope}
            obj.content_hash = fingerprint(obj.payload)
            obj.revision += 1
        else:
            if expected_revision != 0:
                raise DomainError("HEAD_CONFLICT", "默认知识版本不存在。")
            obj = register(
                db,
                actor,
                selection_ns,
                "selection_head",
                {"publication_id": publication_id, "unit_id": unit.id, "scope_key": scope},
            )
            head = SelectionHead(
                id=obj.id,
                selection_namespace_id=selection_ns,
                unit_id=unit.id,
                scope_key=scope,
                publication_id=publication_id,
            )
            db.add(head)
        depend(db, actor, head.id, publication_id, "selects")
        return head.id

    def select_head(
        self, actor: str, selection_ns: str, publication_id: str, expected_revision: int
    ) -> str:
        with self.db.transaction() as db:
            result = self._set_head(db, actor, selection_ns, publication_id, expected_revision)
            locked(db, Namespace, selection_ns).revision += 1
            return result

    def set_project_selection(
        self, actor: str, selection_ns: str, source_id: str, expected_revision: int
    ) -> None:
        with self.db.transaction() as db:
            namespace_access(db, actor, selection_ns, True)
            source = SourceService.get(db, actor, source_id)
            if source.origin_kind != "repository":
                raise DomainError("SELECTION_TYPE", "概念不需要项目版本选择。", 422)
            pubs = db.scalars(
                select(Publication)
                .join(UnitVersion, Publication.unit_version_id == UnitVersion.id)
                .where(UnitVersion.source_id == source_id)
            ).all()
            valid = []
            for p in pubs:
                try:
                    check_closure(db, actor, [p.id])
                    valid.append(p)
                except DomainError:
                    continue
            if not valid:
                raise DomainError("SELECTION_MISSING", "这个快照尚无可用的已审核知识。")
            row = db.scalar(
                select(ProjectSelection)
                .where(
                    ProjectSelection.namespace_id == selection_ns,
                    ProjectSelection.repository_key == source.repository_key,
                )
                .with_for_update()
            )
            if row:
                if row.revision != expected_revision:
                    raise DomainError("REVISION_CONFLICT", "项目选择已变化。")
                row.source_id, row.revision = source_id, row.revision + 1
            elif expected_revision == 0:
                db.add(
                    ProjectSelection(
                        namespace_id=selection_ns,
                        repository_key=source.repository_key,
                        source_id=source_id,
                    )
                )
            else:
                raise DomainError("REVISION_CONFLICT", "项目选择不存在。")
            locked(db, Namespace, selection_ns).revision += 1

    def selected_publications(
        self,
        db: Session,
        actor: str,
        source_ids: list[str] | None = None,
        pinned_ids: list[str] | None = None,
    ) -> list[Publication]:
        result: list[Publication] = []
        if pinned_ids:
            for identifier in dict.fromkeys(pinned_ids):
                check_closure(db, actor, [identifier])
                pub = db.get(Publication, identifier)
                if not pub:
                    raise DomainError("PUBLICATION_REQUIRED", "必须选择具体发布版本。", 422)
                version = _required_record(db, UnitVersion, pub.unit_version_id)
                if source_ids and version.source_id not in source_ids:
                    raise DomainError("PUBLICATION_SCOPE", "固定发布版本不属于所选来源范围。", 422)
                result.append(pub)
            return result
        visible = {ns.id for ns in accessible_namespaces(db, actor)}
        private = private_namespace(db, actor).id
        heads = db.scalars(
            select(SelectionHead).where(SelectionHead.selection_namespace_id.in_(visible))
        ).all()
        chosen: dict[tuple[str, str], SelectionHead] = {}
        for head in sorted(heads, key=lambda h: (h.selection_namespace_id == private, h.id)):
            try:
                obj = require_object(db, actor, head.id, "selection_head")
            except DomainError:
                continue
            if obj.payload != {
                "publication_id": head.publication_id,
                "unit_id": head.unit_id,
                "scope_key": head.scope_key,
            }:
                continue
            identity = db.get(KnowledgeUnit, head.unit_id)
            if identity is None or head.selection_namespace_id not in {
                identity.namespace_id,
                private,
            }:
                continue
            chosen[head.unit_id, head.scope_key] = head
        project_defaults: dict[str, str] = {}
        candidates: dict[str, list[tuple[int, str]]] = {}
        # Explicit snapshot selection does not depend on unrelated default conflicts.
        if not source_ids:
            for p in db.scalars(
                select(ProjectSelection).where(ProjectSelection.namespace_id.in_(visible))
            ):
                ns = db.get(Namespace, p.namespace_id)
                if ns is None:
                    continue
                priority = (
                    2 if p.namespace_id == private else 1 if ns.kind == "shared_public" else 0
                )
                candidates.setdefault(p.repository_key, []).append((priority, p.source_id))
            for repository, options in candidates.items():
                highest = max(priority for priority, _ in options)
                sources = {source for priority, source in options if priority == highest}
                if len(sources) > 1:
                    raise DomainError(
                        "PROJECT_VERSION_AMBIGUOUS",
                        "授权域中的默认项目版本冲突，请固定所需快照。",
                        409,
                    )
                project_defaults[repository] = sources.pop()
        for head in chosen.values():
            pub = db.get(Publication, head.publication_id)
            if pub is None:
                continue
            selected_version = db.get(UnitVersion, pub.unit_version_id)
            if selected_version is None:
                continue
            version = selected_version
            unit = db.get(KnowledgeUnit, version.unit_id)
            if unit is None:
                continue
            if source_ids and version.source_id not in source_ids:
                continue
            if (
                not source_ids
                and unit.origin_kind == "repository"
                and project_defaults.get(unit.origin_id) != version.source_id
            ):
                continue
            try:
                check_closure(db, actor, [pub.id])
                SourceService.get(db, actor, version.source_id)
            except DomainError:
                continue
            result.append(pub)
        return result

    def _consumer_context_in(
        self,
        db: Session,
        actor: str,
        pub: Publication,
        version: UnitVersion,
        review: ReviewDecision,
        facts: Sequence[FactAtom],
    ) -> dict[str, Any]:
        """Revalidate actual role receipts even for a whole-unit acceptance.

        Partial publication already has these guards. Full-unit approval alone
        is not standalone approval; missing old audit fields stay unknown.
        """
        approved: set[str] = set()
        reason = "standalone_review_unavailable"
        report = review.report
        if report.get("knowledge_review_contract_version") == SCOPED_REVIEW_VERSION:
            run = _required_record(db, AgentRun, review.reviewer_run_id)
            if (
                not scoped_review_enabled(run.input_manifest)
                or fingerprint(run.input_manifest) != run.input_hash
                or run.workflow != "review"
                or run.status != "succeeded"
                or version.id not in run.input_manifest.get("unit_version_ids", [])
                or run.id == version.analysis_run_id
            ):
                raise DomainError("CONSUMER_CONTEXT_BINDING", "独立使用审核未绑定原角色合同。")
            raw = report.get("scoped_original_review")
            if raw is None:
                raw = {k: report[k] for k in ScopedReviewOutput.model_fields if k in report}
                raw["claim_decisions"] = [
                    {k: d[k] for k in ClaimReview.model_fields if k in d}
                    for d in report.get("claim_decisions", [])
                ]
            try:
                original = ScopedReviewOutput.model_validate(raw)
                coverage = self._coverage_type(run).model_validate(report["fact_coverage"])
            except (ValidationError, KeyError) as exc:
                raise DomainError(
                    "CONSUMER_CONTEXT_BINDING", "原独立使用审核输出缺失或损坏。"
                ) from exc
            if original.scope_review is not None and coverage.scope_audit is not None:
                subject = self._scope_subject_in(db, actor, version)
                partition = self._review_partition(
                    db, actor, run, version, subject, original, coverage
                )
                bundle = self._scoped_bundle_in(db, actor, run, version)
                if fingerprint(bundle) != review.independent_bundle_hash:
                    raise DomainError("INDEPENDENT_BUNDLE", "独立证据包已变化。")
                self._scoped_receipts_in(db, actor, run, version, original, coverage)
                approved = set(partition["retained_fact_ids"])
                reason = "requires_original_reviewed_context"
        elif report.get("knowledge_review_contract_version") is not None:
            raise DomainError("CONSUMER_CONTEXT_CONTRACT", "未知的原独立审核合同。")
        return {
            "contract": CONSUMER_CONTEXT_CONTRACT,
            "publication_id": pub.id,
            "unit_version_id": version.id,
            "review_id": review.id,
            "review_hash": fingerprint(report),
            "policy": "only_positive_independent_standalone_facts",
            # Include only published facts. Never expose quarantined prose/IDs,
            # invent a dependency graph, or turn raw citations into premises.
            "facts": {
                fact.id: {
                    "claim_id": fact.claim_id,
                    "standalone": fact.id in approved,
                    "reason": "independently_reviewed_standalone"
                    if fact.id in approved
                    else reason,
                }
                for fact in facts
            },
        }

    def publication_pack(
        self,
        db: Session,
        actor: str,
        publication_id: str,
        *,
        consumer_context_contract: str | None = None,
    ) -> dict[str, Any]:
        validate_context_contract(consumer_context_contract)
        check_closure(db, actor, [publication_id])
        po = require_object(db, actor, publication_id, "publication")
        pub = db.get(Publication, publication_id)
        if not pub:
            raise DomainError("NOT_FOUND", "知识发布不存在。", 404)
        v = self._validated_version(db, actor, pub.unit_version_id)
        source = SourceService.get(db, actor, v.source_id)
        vo = require_object(db, actor, v.id, "knowledge_unit_version")
        review = self.validated_review(db, actor, pub.review_id, v)
        scope = (
            review.report.get("scope_derivation")
            if review.decision == "accept_partial_scoped"
            else None
        )
        if (
            po.namespace_id != pub.namespace_id
            or vo.namespace_id != pub.namespace_id
            or po.payload
            != {
                "unit_version_id": v.id,
                "review_id": pub.review_id,
                "content_hash": v.content_hash,
                "render_hash": v.render_hash,
                **({"scope_derivation": scope} if scope is not None else {}),
            }
        ):
            raise DomainError("PUBLICATION_HASH", "发布记录与固定审核版本不一致。")
        if review.decision not in {"accept_scoped", "accept_partial_scoped"}:
            raise DomainError("REVIEW_HASH", "发布缺少有效的接受决议。")
        unit = _required_record(db, KnowledgeUnit, v.unit_id)
        facts = db.scalars(select(FactAtom).where(FactAtom.unit_version_id == v.id)).all()
        claims: list[Claim] = []
        for fact in facts:
            claim = db.get(Claim, fact.claim_id)
            if claim is None or claim.unit_version_id != v.id:
                raise DomainError("FACT_COVERAGE", "发布事实集合与审核版本不一致。")
            claims.append(claim)
        render_fact_ids = [element["fact_atom_id"] for element in v.render_ast]
        if (
            len(render_fact_ids) != len(set(render_fact_ids))
            or set(render_fact_ids) != {fact.id for fact in facts}
            or {decision["claim_id"] for decision in review.report["claim_decisions"]}
            != {claim.id for claim in claims}
        ):
            raise DomainError("FACT_COVERAGE", "发布事实集合与审核版本不一致。")
        claim_payloads: dict[str, dict[str, Any]] = {}
        for fact, verified_claim in zip(facts, claims, strict=True):
            fp = require_object(db, actor, fact.id, "fact_atom").payload
            cp = require_object(db, actor, verified_claim.id, "claim").payload
            if (
                fp.get("claim_id") != fact.claim_id
                or fp["text"] != fact.approved_text
                or fp["conditions"] != fact.conditions
                or cp["statement"] != verified_claim.statement
                or cp["conditions"] != verified_claim.conditions
                or fact.approved_text != verified_claim.statement
                or fact.conditions != verified_claim.conditions
            ):
                raise DomainError("FACT_HASH", "事实与已审核断言不一致。")
            claim_payloads[verified_claim.id] = cp
        if scope is not None:
            facts = [fact for fact in facts if fact.id in scope["retained_fact_ids"]]
            claims = [claim for claim in claims if claim.id in scope["retained_claim_ids"]]
            expected_fact_dependencies = set(
                db.scalars(
                    select(ArtifactDependency.source_id).where(
                        ArtifactDependency.dependent_id == pub.id,
                        ArtifactDependency.kind == "requires_fact",
                        ArtifactDependency.enforcement == "blocking",
                    )
                )
            )
            if expected_fact_dependencies != set(scope["retained_fact_ids"]):
                raise DomainError("FACT_COVERAGE", "部分发布依赖集合与原推导不一致。")
        evidence_ids: list[str] = list(
            dict.fromkeys(e for c in claims for e in claim_payloads[c.id]["support_refs"])
        )
        links: list[dict[str, Any]] = []
        for link in db.scalars(select(TopicLink).where(TopicLink.unit_version_id == v.id)):
            if scope is not None and link.id not in scope.get("retained_topic_link_ids", []):
                continue
            try:
                link_view = self._topic_link_view(db, actor, link)
                if link.id not in review.report["accepted_topic_link_ids"]:
                    raise DomainError("TOPIC_LINK_HASH", "主题关联与审核记录不一致。")
                links.append(link_view)
            except DomainError:
                continue
        facets: list[dict[str, Any]] = []
        required_facets = set(
            db.scalars(
                select(ArtifactDependency.source_id).where(
                    ArtifactDependency.dependent_id == pub.id,
                    ArtifactDependency.kind == "uses_facet",
                    ArtifactDependency.enforcement == "blocking",
                )
            )
        )
        for facet in db.scalars(select(FacetValue).where(FacetValue.unit_version_id == v.id)):
            if scope is not None and (
                facet.id not in scope.get("retained_facet_ids", [])
                or facet.topic_id
                not in {
                    link.topic_id
                    for link in db.scalars(
                        select(TopicLink).where(TopicLink.id.in_([item["id"] for item in links]))
                    )
                }
            ):
                continue
            try:
                check_closure(db, actor, [facet.id])
                payload = require_object(db, actor, facet.id, "facet_value").payload
                topic = _required_record(db, Topic, facet.topic_id)
                schema_id = facet_object_id(topic.key, facet.schema_version)
                schema = require_object(db, actor, schema_id, "facet_schema").payload
                schema_refs = set(
                    db.scalars(
                        select(ArtifactDependency.source_id).where(
                            ArtifactDependency.dependent_id == facet.id,
                            ArtifactDependency.kind == "conforms_to",
                            ArtifactDependency.enforcement == "blocking",
                        )
                    )
                )
                if schema_refs != {schema_id}:
                    raise DomainError("FACET_HASH", "比较维度与冻结Schema绑定不一致。")
                facts_by_id = {fact.id: fact for fact in facts}
                claim_keys = {
                    draft["key"]: facts_by_id[element["fact_atom_id"]].claim_id
                    for draft, element in zip(
                        vo.payload["draft"]["claims"], v.render_ast, strict=True
                    )
                    if element["fact_atom_id"] in facts_by_id
                }
                if any(
                    payload.get(key) != value
                    for key, value in {
                        "topic_key": topic.key,
                        "facet_key": facet.facet_key,
                        "value_status": facet.value_status,
                        "value": facet.value,
                    }.items()
                ):
                    raise DomainError("FACET_HASH", "比较维度与审核记录不一致。")
                if (
                    facet.id not in review.report["accepted_facet_ids"]
                    or facet.facet_key not in schema
                    or facet.required != bool(schema[facet.facet_key].get("required"))
                    or any(key not in claim_keys for key in payload.get("claim_keys", []))
                    or facet.claim_ids != [claim_keys[key] for key in payload.get("claim_keys", [])]
                ):
                    raise DomainError("FACET_HASH", "比较维度的适用范围或支持断言已改变。")
                facets.append(
                    {
                        "id": facet.id,
                        "topic_key": topic.key,
                        "facet_key": facet.facet_key,
                        "schema_version": facet.schema_version,
                        "facet_schema_id": schema_id,
                        "unit": schema[facet.facet_key].get("unit"),
                        "applicability_reason": payload.get("applicability_reason", ""),
                        "value_status": facet.value_status,
                        "value": facet.value,
                        "claim_ids": facet.claim_ids,
                    }
                )
            except DomainError:
                if facet.id in required_facets:
                    raise
                continue
        # A blocking object/edge can survive a deleted or reassigned projection.
        # Checking only the rows returned above would silently omit that requirement.
        if not required_facets <= {facet["id"] for facet in facets}:
            raise DomainError("FACET_COVERAGE", "发布缺少必需的已审核比较维度。")
        return {
            "publication_id": pub.id,
            "unit_version_id": v.id,
            "unit_id": unit.id,
            "unit_type": unit.unit_type,
            "origin_kind": unit.origin_kind,
            "title": scope["title"] if scope is not None else v.title,
            "source_id": source.id,
            "repository": source.repository_key,
            "commit": source.commit_sha,
            "namespace_id": pub.namespace_id,
            "topic_links": links,
            "facets": facets,
            "facts": [
                {
                    "fact_atom_id": f.id,
                    "claim_id": f.claim_id,
                    "text": f.approved_text,
                    "conditions": f.conditions,
                    **(
                        {"kind": next(c.kind for c in claims if c.id == f.claim_id)}
                        if scope is not None
                        else {}
                    ),
                }
                for f in facts
            ],
            "citations": [self.evidence_dict(db, actor, e) for e in evidence_ids],
            **(
                {"mechanism_edges": scope["retained_mechanism_edges"]}
                if scope is not None and "retained_mechanism_edges" in scope
                else {}
            ),
            "limitations": list(PARTIAL_LIMITATIONS) if scope is not None else v.limitations,
            "runtime_observed": False,
            "synthetic": v.synthetic,
            **(
                {"consumer_context": self._consumer_context_in(db, actor, pub, v, review, facts)}
                if consumer_context_contract is not None
                else {}
            ),
            **(
                {
                    "publication_scope": {
                        "contract_version": scope["contract_version"],
                        "status": "partial",
                        "derivation_hash": scope["derivation_hash"],
                        "retained_claim_ids": scope["retained_claim_ids"],
                        "quarantined_claim_count": len(scope["quarantined_claims"]),
                        "whole_unit_complete": False,
                        "task_satisfied": False,
                    }
                }
                if scope is not None
                else {}
            ),
        }
