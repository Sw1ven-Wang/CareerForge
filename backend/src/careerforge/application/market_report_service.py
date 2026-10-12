import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, digest, fingerprint, idempotency
from careerforge.application.import_service import materialize_job
from careerforge.application.run_service import create_run
from careerforge.application.source_service import get_snapshot
from careerforge.domain.errors import BusinessError, not_found
from careerforge.domain.jobs import (
    DICTIONARY_VERSION,
    SKILLS,
    canonical_url,
    groups,
    normalize_skill,
)
from careerforge.infrastructure.db.models import (
    CareerGoalVersion,
    Citation,
    DedupDecision,
    DuplicateCandidate,
    JobPostingVersion,
    MarketReport,
    Requirement,
    RequirementCollection,
    Source,
    SourceSnapshot,
)
from careerforge.schemas.job_extraction import (
    DuplicateDecisionInput,
    ExtractedRequirement,
    JobExtraction,
    MarketRunInput,
    RequirementCorrection,
)

VERSION = "market-offline-v1"


def inputs(db: Session, owner: UUID, snapshot_ids: list[str]) -> list[SourceSnapshot]:
    rows = []
    for identifier in snapshot_ids:
        row = db.get(SourceSnapshot, UUID(identifier))
        if row is None or row.owner_id != owner:
            raise not_found()
        rows.append(get_snapshot(db, owner, row.source_id, row.id))
    return rows


def goal(db: Session, owner: UUID, identifier: UUID) -> CareerGoalVersion:
    row = db.get(CareerGoalVersion, identifier)
    if row is None or row.owner_id != owner:
        raise not_found()
    return row


def start(db: Session, owner: UUID, body: MarketRunInput, key: str | None) -> dict[str, Any]:
    from careerforge.config.settings import get_settings

    if get_settings().job_contract_write and get_settings().job_contract_read == "on":
        from careerforge.application.job_contract_service import start_market

        return start_market(db, owner, body, key)
    record = idempotency(db, owner, "market.create", key, body.model_dump(mode="json"))
    goal(db, owner, body.goal_version_id)
    rows = inputs(db, owner, [str(i) for i in body.source_snapshot_ids])
    if len({r.id for r in rows}) != len(rows):
        raise BusinessError(422, "DUPLICATE_INPUT", "同一快照只需选择一次。")
    if not any(r.metadata_values.get("source_type") == "job_posting" for r in rows):
        raise BusinessError(422, "NO_READABLE_JD", "至少需要一份可读岗位正文。")
    if record.response_summary is not None:
        return record.response_summary
    postings = [materialize_job(db, r) for r in rows]
    posting_ids = {p.id for p in postings if p is not None}
    frozen = []
    seen_candidates: set[UUID] = set()
    for identifier in body.dedup_decision_ids:
        decision = db.get(DedupDecision, identifier)
        candidate = db.get(DuplicateCandidate, decision.candidate_id) if decision else None
        if (
            candidate is None
            or candidate.owner_id != owner
            or candidate.goal_version_id != body.goal_version_id
        ):
            raise not_found()
        if candidate.left_id not in posting_ids or candidate.right_id not in posting_ids:
            raise BusinessError(422, "DECISION_INPUT_MISMATCH", "去重决定不属于本次输入集合。")
        assert decision is not None
        if candidate.id in seen_candidates:
            raise BusinessError(422, "AMBIGUOUS_DECISIONS", "同一候选对只能选择一个决定版本。")
        seen_candidates.add(candidate.id)
        frozen.append(
            {
                "decision_id": str(identifier),
                "left": str(candidate.left_id),
                "right": str(candidate.right_id),
                "decision": decision.decision,
            }
        )
    # Validate contradictions before scheduling and freeze exactly the selected decisions.
    try:
        groups(posting_items(db, [p for p in postings if p is not None]), frozen)
    except ValueError as exc:
        raise BusinessError(409, "DEDUP_CONFLICT", "去重决定与确定重复组矛盾。") from exc
    manifest = {
        "goal_version_id": str(body.goal_version_id),
        "source_snapshot_ids": [str(r.id) for r in rows],
        "snapshot_hashes": {str(r.id): [r.content_hash, r.metadata_hash] for r in rows},
        "dedup_decisions": frozen,
        "mode": "offline",
        "dictionary_version": DICTIONARY_VERSION,
    }
    run = create_run(
        db,
        owner,
        "market_research",
        VERSION,
        manifest,
        call_budget=len(rows),
        goal_version_id=body.goal_version_id,
    )
    result = {
        "run_id": str(run.id),
        "run_type": "market_research",
        "status": run.status,
        "version": run.version,
        "events_url": f"/api/v1/runs/{run.id}/events",
    }
    record.response_summary = result
    return result


def posting_items(db: Session, postings: list[JobPostingVersion]) -> list[dict[str, Any]]:
    result = []
    for p in postings:
        snapshot = db.get(SourceSnapshot, p.snapshot_id)
        source = db.get(Source, p.source_id)
        assert snapshot is not None and source is not None
        result.append(
            {
                "id": str(p.id),
                "source_id": str(p.source_id),
                "snapshot_id": str(p.snapshot_id),
                "version_no": snapshot.version_no,
                "content_hash": snapshot.content_hash,
                "url": canonical_url(source.source_url),
            }
        )
    return result


def validate_quote(snapshot: SourceSnapshot, value: ExtractedRequirement) -> None:
    if (
        not 0 <= value.char_start < value.char_end <= len(snapshot.content)
        or snapshot.content[value.char_start : value.char_end] != value.text
    ):
        raise BusinessError(422, "INVALID_CITATION", "提取引用与规范原文不一致。")
    concept = normalize_skill(value.normalized_skill)
    aliases = SKILLS.get(concept, [concept])
    if value.origin == "explicit" and not any(
        re.search(
            r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])"
            if alias.isascii()
            else re.escape(alias),
            value.text,
            re.I,
        )
        for alias in aliases
    ):
        raise BusinessError(422, "UNSUPPORTED_REQUIREMENT", "明确要求的技能必须出现在支持原文中。")
    if (value.origin == "inferred") != (value.requirement_type == "inferred"):
        raise BusinessError(422, "INVALID_REQUIREMENT_ORIGIN", "推断项必须单独标注。")


def persist_requirement(
    db: Session,
    collection: RequirementCollection,
    snapshot: SourceSnapshot,
    value: ExtractedRequirement,
    supersedes: UUID | None = None,
) -> Requirement:
    validate_quote(snapshot, value)
    citation = Citation(
        owner_id=snapshot.owner_id,
        source_id=snapshot.source_id,
        snapshot_id=snapshot.id,
        char_start=value.char_start,
        char_end=value.char_end,
        text_hash=digest(value.text),
    )
    db.add(citation)
    db.flush()
    payload = value.model_dump()
    payload["normalized_skill"] = normalize_skill(value.normalized_skill)
    row = Requirement(
        collection_id=collection.id,
        collection_version=collection.revision,
        supersedes_id=supersedes,
        citation_id=citation.id,
        payload=payload,
    )
    db.add(row)
    db.flush()
    return row


def extraction_collection(
    db: Session, posting: JobPostingVersion, value: JobExtraction
) -> RequirementCollection:
    db.scalar(select(JobPostingVersion).where(JobPostingVersion.id == posting.id).with_for_update())
    snapshot = db.get(SourceSnapshot, posting.snapshot_id)
    assert snapshot is not None
    for requirement in value.requirements:
        validate_quote(snapshot, requirement)
    collection = db.scalar(
        select(RequirementCollection)
        .where(
            RequirementCollection.posting_version_id == posting.id,
            RequirementCollection.extractor_version == VERSION,
        )
        .with_for_update()
    )
    if collection is None:
        collection = RequirementCollection(posting_version_id=posting.id, extractor_version=VERSION)
        db.add(collection)
        db.flush()
        for requirement in value.requirements:
            persist_requirement(db, collection, snapshot, requirement)
    return collection


def requirement_result(db: Session, row: Requirement) -> dict[str, Any]:
    collection = db.get(RequirementCollection, row.collection_id)
    assert collection is not None
    posting = db.get(JobPostingVersion, collection.posting_version_id)
    assert posting is not None
    citation = db.get(Citation, row.citation_id)
    assert citation is not None
    return {
        **row.payload,
        "requirement_id": str(row.id),
        "collection_id": str(collection.id),
        "collection_version": row.collection_version,
        "posting_version_id": str(posting.id),
        "supersedes_requirement_id": str(row.supersedes_id) if row.supersedes_id else None,
        "citation": {
            "citation_id": str(citation.id),
            "source_snapshot_id": str(posting.snapshot_id),
            "source_id": str(posting.source_id),
            "char_start": citation.char_start,
            "char_end": citation.char_end,
            "text_hash": citation.text_hash,
        },
    }


def current_requirements(db: Session, collection: RequirementCollection) -> list[Requirement]:
    rows = list(db.scalars(select(Requirement).where(Requirement.collection_id == collection.id)))
    replaced = {r.supersedes_id for r in rows if r.supersedes_id}
    return [r for r in rows if r.id not in replaced]


def text_similarity(left: str, right: str) -> float:
    # Bounded linear token-shingle similarity; never declares a certain duplicate.
    def shingles(value: str) -> set[str]:
        text = re.sub(r"\s+", "", value.casefold())[:100_000]
        return {text[i : i + 4] for i in range(max(1, len(text) - 3))}

    a, b = shingles(left), shingles(right)
    return len(a & b) / len(a | b) if a | b else 0.0


def region_key(value: str) -> str:
    return value.removesuffix("市").strip()


def candidates(
    db: Session, owner: UUID, goal_id: UUID, run_id: UUID, postings: list[JobPostingVersion]
) -> list[DuplicateCandidate]:
    result = []
    for n, left in enumerate(postings):
        a = db.get(SourceSnapshot, left.snapshot_id)
        assert a is not None
        for right in postings[n + 1 :]:
            b = db.get(SourceSnapshot, right.snapshot_id)
            assert b is not None
            left_source = db.get(Source, left.source_id)
            right_source = db.get(Source, right.source_id)
            assert left_source is not None and right_source is not None
            certain = (
                left.source_id == right.source_id
                or a.content_hash == b.content_hash
                or canonical_url(left_source.source_url)
                == canonical_url(right_source.source_url)
                is not None
            )
            if certain:
                continue
            same_title = a.metadata_values.get("title") and a.metadata_values.get(
                "title"
            ) == b.metadata_values.get("title")
            if not same_title and text_similarity(a.content, b.content) < 0.88:
                continue
            l_id, r_id = sorted([left.id, right.id])
            row = db.scalar(
                select(DuplicateCandidate).where(
                    DuplicateCandidate.goal_version_id == goal_id,
                    DuplicateCandidate.left_id == l_id,
                    DuplicateCandidate.right_id == r_id,
                )
            )
            if row is None:
                row = DuplicateCandidate(
                    owner_id=owner,
                    goal_version_id=goal_id,
                    run_id=run_id,
                    left_id=l_id,
                    right_id=r_id,
                    reason="same_title_or_similar_text",
                )
                db.add(row)
                db.flush()
            result.append(row)
    return result


def build_report(
    db: Session,
    owner: UUID,
    run_id: UUID,
    manifest: dict[str, Any],
    extracted: dict[str, dict[str, Any]],
) -> MarketReport:
    db.scalar(
        select(CareerGoalVersion)
        .where(CareerGoalVersion.id == UUID(manifest["goal_version_id"]))
        .with_for_update()
    )
    existing = db.scalar(select(MarketReport).where(MarketReport.run_id == run_id))
    if existing:
        return existing
    goal_id = UUID(manifest["goal_version_id"])
    target = goal(db, owner, goal_id)
    snapshots = inputs(db, owner, manifest["source_snapshot_ids"])
    postings = []
    valid: dict[str, dict[str, Any]] = {}
    unresolved = []
    requirement_rows: list[dict[str, Any]] = []
    collection_versions: dict[str, int] = {}
    counts = {
        "raw": len(snapshots),
        "readable": len(snapshots),
        "job_postings": 0,
        "extraction_completed": 0,
        "deduplicated": 0,
        "interview_experience": 0,
        "other_material": 0,
        "unresolved_duplicates": 0,
    }
    for snapshot in snapshots:
        kind = snapshot.metadata_values.get("source_type")
        if kind != "job_posting":
            counts[
                "interview_experience" if kind == "interview_experience" else "other_material"
            ] += 1
            continue
        counts["job_postings"] += 1
        posting = materialize_job(db, snapshot)
        assert posting is not None
        postings.append(posting)
        outcome = extracted[str(snapshot.id)]
        if outcome.get("error"):
            unresolved.append({"source_snapshot_id": str(snapshot.id), "reason": outcome["error"]})
            continue
        value = JobExtraction.model_validate(outcome["extraction"])
        counts["extraction_completed"] += 1
        collection = extraction_collection(db, posting, value)
        collection_versions[str(collection.id)] = collection.revision
        requirement_rows.extend(
            requirement_result(db, r) for r in current_requirements(db, collection)
        )
        valid[str(posting.id)] = value.model_dump()
    items = posting_items(db, postings)
    grouped = groups(items, manifest["dedup_decisions"])
    counts["deduplicated"] = len(grouped)
    choices = candidates(db, owner, goal_id, run_id, postings)
    selected_decisions = {
        frozenset([d["left"], d["right"]]): d["decision"] for d in manifest["dedup_decisions"]
    }
    group_index = {p: n for n, members in enumerate(grouped) for p in members}
    counts["unresolved_duplicates"] = sum(
        group_index[str(c.left_id)] != group_index[str(c.right_id)]
        and selected_decisions.get(frozenset([str(c.left_id), str(c.right_id)]), "unknown")
        == "unknown"
        for c in choices
    )
    by_id = {i["id"]: i for i in items}
    eligible = []
    scope_exclusions = []
    requested_type = target.payload.get("job_type", "unspecified")
    requested_regions = target.payload.get("regions", [])
    for members in grouped:
        completed = [m for m in members if m in valid]
        if not completed:
            continue
        # Prefer newest completed version of a stable source; never mix two versions' facts.
        representative = max(completed, key=lambda m: (by_id[m]["version_no"], m))
        scoped_value = valid[representative]
        if (
            requested_type != "unspecified"
            and scoped_value["job_type"] != requested_type
            or requested_regions
            and not {region_key(r) for r in requested_regions}.intersection(
                region_key(r) for r in scoped_value["regions"]
            )
        ):
            scope_exclusions.append(
                {"posting_version_id": representative, "reason": "outside_or_unknown_scope"}
            )
            continue
        eligible.append(representative)
    statistics = []
    concepts = sorted({r["normalized_skill"] for r in requirement_rows})
    for concept in concepts:
        supporting = sorted(
            {
                r["posting_version_id"]
                for r in requirement_rows
                if r["posting_version_id"] in eligible
                and r["normalized_skill"] == concept
                and r["origin"] == "explicit"
            }
        )
        denominator = len(eligible)
        statistics.append(
            {
                "skill_concept_id": concept,
                "numerator": len(supporting),
                "denominator": denominator,
                "ratio": len(supporting) / denominator if denominator else None,
                "supporting_posting_version_ids": supporting,
                "denominator_posting_version_ids": eligible,
                "extraction_coverage": {
                    "completed": counts["extraction_completed"],
                    "total": counts["job_postings"],
                },
            }
        )
    regional = {
        region: [p for p in eligible if region in valid[p]["regions"]]
        for region in sorted({r for p in eligible for r in valid[p]["regions"]})
    }
    snapshot_map = {r.id: r for r in snapshots}
    payload = {
        "goal_version_id": str(goal_id),
        "source_snapshot_ids": manifest["source_snapshot_ids"],
        "job_posting_version_ids": [str(p.id) for p in postings],
        "input_summaries": [
            {
                "source_snapshot_id": str(r.id),
                "source_type": r.metadata_values.get("source_type"),
                "title": r.metadata_values.get("title"),
                "read_status": "available",
            }
            for r in snapshots
        ],
        "counts": counts,
        "filters": {"job_type": requested_type, "regions": requested_regions},
        "dedup_policy": "source_or_canonical_url_or_exact_hash_then_selected_decisions",
        "dedup_groups": grouped,
        "dedup_decisions": manifest["dedup_decisions"],
        "eligible_posting_version_ids": eligible,
        "scope_exclusions": scope_exclusions,
        "regional_posting_groups": regional,
        "requirements": requirement_rows,
        "skill_statistics": statistics,
        "unresolved_sources": unresolved,
        "observations": [
            {
                "text": "技能出现率仅描述本次已读、提取完成且符合范围的样本。",
                "citation_ids": [],
                "limitations": ["非市场占比或录用概率"],
            }
        ],
        "warnings": [
            "离线规则候选 + FakeModel 契约验证；未验证真实模型质量。",
            "未决去重项仍可能重复。",
            "时间缺失保留未知；工具名不代表能力已掌握。",
        ],
        "synthetic_model": True,
        "extraction_mode": "offline_rules_v1+FakeModel_contract",
        "generated_by_run_id": str(run_id),
        "config_fingerprint": fingerprint(
            {"manifest": manifest, "collection_versions": collection_versions, "extractor": VERSION}
        ),
        "dictionary_version": DICTIONARY_VERSION,
        "posting_summaries": [
            {
                "posting_version_id": str(p.id),
                "source_snapshot_id": str(p.snapshot_id),
                "source_id": str(p.source_id),
                "title": valid.get(str(p.id), {}).get("title"),
                "job_type": valid.get(str(p.id), {}).get("job_type"),
                "regions": valid.get(str(p.id), {}).get("regions", []),
                "experience": valid.get(str(p.id), {}).get("experience"),
                "extraction_limitations": valid.get(str(p.id), {}).get("unresolved", []),
                "published_at": snapshot_map[p.snapshot_id].metadata_values.get("published_at"),
                "published_at_precision": snapshot_map[p.snapshot_id].metadata_values.get(
                    "published_at_precision", "unknown"
                ),
            }
            for p in postings
        ],
        "requirement_collection_versions": sorted(collection_versions.items()),
    }
    row = MarketReport(owner_id=owner, goal_version_id=goal_id, run_id=run_id, payload=payload)
    db.add(row)
    db.flush()
    return row


def read_report(db: Session, owner: UUID, identifier: UUID) -> MarketReport:
    row = db.scalar(
        select(MarketReport).where(MarketReport.id == identifier, MarketReport.owner_id == owner)
    )
    if row is None:
        raise not_found()
    inputs(db, owner, row.payload["source_snapshot_ids"])
    if row.payload.get("demand_report_id"):
        from careerforge.application.job_contract_service import read

        read(db, owner, UUID(row.payload["demand_report_id"]), "report")
    return row


def correct(
    db: Session, owner: UUID, identifier: UUID, body: RequirementCorrection, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"requirement.{identifier}.correct", key, body.model_dump(mode="json")
    )
    requirement = db.get(Requirement, identifier)
    if requirement is None:
        raise not_found()
    collection = db.scalar(
        select(RequirementCollection)
        .where(RequirementCollection.id == requirement.collection_id)
        .with_for_update()
    )
    assert collection is not None
    posting = db.get(JobPostingVersion, collection.posting_version_id)
    if posting is None or posting.owner_id != owner:
        raise not_found()
    snapshot = get_snapshot(db, owner, posting.source_id, posting.snapshot_id)
    if record.response_summary is not None:
        return record.response_summary
    check_revision(collection.revision, body.expected_version)
    if db.scalar(select(Requirement.id).where(Requirement.supersedes_id == identifier)):
        raise BusinessError(409, "REQUIREMENT_SUPERSEDED", "此条目已有更正，请读取新条目。")
    collection.revision += 1
    value = ExtractedRequirement.model_validate(
        body.model_dump(exclude={"expected_version", "reason"})
    )
    row = persist_requirement(db, collection, snapshot, value, identifier)
    row.payload = {**row.payload, "correction_reason": body.reason, "origin_actor": "user"}
    record.response_summary = {
        "requirement_id": str(row.id),
        "supersedes_requirement_id": str(identifier),
        "collection_id": str(collection.id),
        "collection_version": collection.revision,
        "dependent_reports_need_review": True,
    }
    return record.response_summary


def decide(
    db: Session, owner: UUID, identifier: UUID, body: DuplicateDecisionInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"candidate.{identifier}.decide", key, body.model_dump(mode="json")
    )
    candidate = db.scalar(
        select(DuplicateCandidate)
        .where(DuplicateCandidate.id == identifier, DuplicateCandidate.owner_id == owner)
        .with_for_update()
    )
    if candidate is None:
        raise not_found()
    # Serialize all decisions for this goal to validate transitive groups atomically.
    db.scalar(
        select(CareerGoalVersion)
        .where(CareerGoalVersion.id == candidate.goal_version_id)
        .with_for_update()
    )
    all_candidates = list(
        db.scalars(
            select(DuplicateCandidate).where(
                DuplicateCandidate.owner_id == owner,
                DuplicateCandidate.goal_version_id == candidate.goal_version_id,
            )
        )
    )
    ids = {p for c in all_candidates for p in [c.left_id, c.right_id]}
    postings = list(db.scalars(select(JobPostingVersion).where(JobPostingVersion.id.in_(ids))))
    inputs(db, owner, [str(p.snapshot_id) for p in postings])
    if record.response_summary is not None:
        return record.response_summary
    check_revision(candidate.revision, body.expected_version)
    decisions = []
    for c in all_candidates:
        latest = db.scalar(
            select(DedupDecision)
            .where(DedupDecision.candidate_id == c.id)
            .order_by(DedupDecision.version_no.desc())
            .limit(1)
        )
        choice = body.decision if c.id == identifier else latest.decision if latest else "unknown"
        decisions.append({"left": str(c.left_id), "right": str(c.right_id), "decision": choice})
    try:
        if candidate.reason.startswith("contract_"):
            from careerforge.domain.job_requirements import validate_dedup_decisions

            validate_dedup_decisions(decisions)
        else:
            groups(posting_items(db, postings), decisions)
    except ValueError as exc:
        raise BusinessError(
            409, "DEDUP_CONFLICT", "决定与已有重复组矛盾，请先复核相关决定。"
        ) from exc
    candidate.revision += 1
    row = DedupDecision(
        candidate_id=candidate.id,
        version_no=candidate.revision,
        decision=body.decision,
        reason=body.reason,
    )
    db.add(row)
    db.flush()
    record.response_summary = {"decision_id": str(row.id), "version": candidate.revision}
    return record.response_summary
