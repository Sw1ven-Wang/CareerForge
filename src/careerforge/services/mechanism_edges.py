"""Publication metadata for explicitly proposed and independently checked calls."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from ..contracts_project_analysis_v166 import MechanismAnalysisOutput
from ..contracts_scoped_review_v166 import MECHANISM_PUBLICATION_CONTRACT
from ..errors import DomainError
from ..models import AgentRun, ModelReceipt, StageArtifact
from ..util import fingerprint
from .dependencies import check_closure, require_object
from .sources import SourceService


def edge_windows(
    targets: list[dict[str, Any]], additional: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    windows: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in [*(target[k] for target in targets for k in ("caller", "callee")), *additional]:
        windows.setdefault((row["path"], row["start_line"], row["end_line"]), row)
    return list(windows.values())


def _contains_symbol(text, symbol):
    return bool(
        re.search(r"(?<![\w$])" + re.escape(symbol.rsplit(".", 1)[-1]) + r"(?![\w$])", text)
    )


def _covers(ref, endpoint, symbol):
    return (
        ref["path"] == endpoint["path"]
        and endpoint["start_line"] <= ref["start_line"] <= ref["end_line"] <= endpoint["end_line"]
        and _contains_symbol(ref["quote"], symbol)
    )


def endpoint_refs(refs, target):
    # The call-site quote must mention the called symbol; the target-file quote
    # must locate that same symbol. The roles decide actual call semantics.
    symbol = target["callee"]["symbol"]
    return {
        side: [ref["citation_id"] for ref in refs if _covers(ref, target[side], symbol)]
        for side in ("caller", "callee")
    }


def analysis_candidates(knowledge, db, actor, reviewer, version):
    from .mechanism_discovery import require_bound_plan

    targets = reviewer.input_manifest.get("mechanism_edge_targets", [])
    analysis = db.get(AgentRun, version.analysis_run_id)
    if (
        analysis is None
        or analysis.workflow != "analysis"
        or analysis.status != "succeeded"
        or not analysis.parent_id
        or analysis.parent_id != reviewer.parent_id
        or analysis.principal_id != reviewer.principal_id
        or analysis.funding_id != reviewer.funding_id
        or analysis.namespace_id != reviewer.namespace_id
        or fingerprint(analysis.input_manifest) != analysis.input_hash
        or analysis.input_manifest.get("mechanism_publication_contract")
        != MECHANISM_PUBLICATION_CONTRACT
        or analysis.input_manifest.get("mechanism_edge_targets") != targets
        or analysis.input_manifest.get("source_ids") != [version.source_id]
    ):
        raise DomainError("MECHANISM_EDGE_SCOPE", "机制候选不属于同一构建和固定来源。", 409)
    require_bound_plan(db, analysis, version.source_id)
    require_bound_plan(db, reviewer, version.source_id)
    stage = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == analysis.id,
            StageArtifact.stage_key == "task_analysis:" + version.source_id,
        )
    )
    receipts = {
        row.stage_key: row
        for row in db.scalars(
            select(ModelReceipt).where(
                ModelReceipt.run_id == analysis.id,
                ModelReceipt.stage_key.in_(
                    [
                        "analysis.final:" + version.source_id,
                        "analysis.final:" + version.source_id + ":quote-correction:1",
                    ]
                ),
            )
        )
    }
    receipt = receipts.get(
        "analysis.final:" + version.source_id + ":quote-correction:1"
    ) or receipts.get("analysis.final:" + version.source_id)
    if (
        stage is None
        or stage.input_hash != analysis.input_hash
        or version.id not in stage.payload["unit_version_ids"]
        or receipt is None
        or receipt.role != "analysis"
        or receipt.status != "completed"
        or receipt.payer_id != analysis.principal_id
        or receipt.profile_version != analysis.input_manifest["model_profile"]
        or not receipt.response
    ):
        raise DomainError("MECHANISM_EDGE_SCOPE", "机制候选缺少原分析回执。", 409)
    witness = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == analysis.id,
            StageArtifact.stage_key == "model-access:" + receipt.id,
        )
    )
    if (
        witness is None
        or witness.input_hash != analysis.input_hash
        or witness.payload.get("receipt_id") != receipt.id
        or witness.payload.get("request_hash") != receipt.request_hash
        or version.source_id not in witness.payload.get("access_dependencies", [])
    ):
        raise DomainError("MECHANISM_EDGE_SCOPE", "机制候选缺少分析权限见证。", 409)
    check_closure(db, actor, witness.payload["access_dependencies"])
    structured = receipt.response["structured"]
    if (
        receipt.stage_key.endswith(":quote-correction:1")
        and analysis.input_manifest.get("analysis_quote_patch") == "read-line-selection-v2"
    ):
        from .quote_correction import replay_review_quote_patches

        structured = replay_review_quote_patches(
            db, analysis, "analysis.final:" + version.source_id, structured
        )
    original = MechanismAnalysisOutput.model_validate(structured)
    draft = require_object(db, actor, version.id, "knowledge_unit_version").payload["draft"]
    claims = {
        item["key"]: require_object(db, actor, element["fact_atom_id"], "fact_atom").payload[
            "claim_id"
        ]
        for item, element in zip(draft["claims"], version.render_ast, strict=True)
    }
    results = []
    for edge in original.mechanism_edges:
        if edge.subject_key != draft["subject_key"] or edge.claim_key not in claims:
            continue
        target = next(
            (
                target
                for target in targets
                if (
                    target["caller"]["path"],
                    target["caller"]["symbol"],
                    target["callee"]["path"],
                    target["callee"]["symbol"],
                )
                == (edge.from_path, edge.from_symbol, edge.to_path, edge.to_symbol)
            ),
            None,
        )
        if target is None:
            continue
        claim_id = claims[edge.claim_key]
        claim = require_object(db, actor, claim_id, "claim").payload
        if claim.get("counterevidence_refs"):
            continue
        refs = [knowledge.evidence_dict(db, actor, key) for key in claim["support_refs"]]
        proof = endpoint_refs(refs, target)
        if all(proof.values()):
            results.append(
                {
                    "edge_key": target["key"],
                    "claim_id": claim_id,
                    "target": target,
                    "analysis_receipt_id": receipt.id,
                    "analysis_evidence_ids": proof,
                }
            )
    return results


def reviewed_edges(knowledge, db, actor, reviewer, version, review, coverage, partition, bundle):
    source = SourceService.get(db, actor, version.source_id)
    candidates = {
        (item["edge_key"], item["claim_id"]): item
        for item in analysis_candidates(knowledge, db, actor, reviewer, version)
    }
    retained = dict(
        zip(partition["retained_claim_ids"], partition["retained_fact_ids"], strict=True)
    )
    reads = {row["read_id"]: row for row in bundle["reads"]}
    decisions = {row.claim_id: row for row in review.claim_decisions}
    edges = {}
    for approval in coverage.approved_mechanism_edges:
        candidate = candidates.get((approval.edge_key, approval.claim_id))
        if candidate is None or approval.claim_id not in retained:
            continue
        refs = []
        for ref in decisions[approval.claim_id].independent_reads:
            row = reads[ref.read_id]
            # Match exact evidence lines, not the entire containing read window.
            before, quote, after = row["content"].partition(ref.quote)
            if not quote or ref.quote in after or row["content"].count(ref.quote) != 1:
                continue
            start = row["start_line"] + before.count("\n")
            end = start + quote.rstrip("\r\n").count("\n")
            refs.append(
                {
                    "path": row["path"],
                    "start_line": start,
                    "end_line": end,
                    "quote": quote,
                    "citation_id": ref.read_id,
                }
            )
        independent = endpoint_refs(refs, candidate["target"])
        if not all(independent.values()):
            continue
        value = {
            "contract": MECHANISM_PUBLICATION_CONTRACT,
            "relation": "calls",
            "source_id": source.id,
            "commit": source.commit_sha,
            "unit_version_id": version.id,
            **candidate,
            "fact_atom_id": retained[approval.claim_id],
            "reviewer_run_id": reviewer.id,
            "independent_read_ids": independent,
        }
        # A target with conflicting claim assignments has no single approved edge.
        edges.setdefault(approval.edge_key, {})[fingerprint(value)] = value
    return [
        {"edge_id": key, **value}
        for assignments in edges.values()
        if len(assignments) == 1
        for key, value in assignments.items()
    ]
