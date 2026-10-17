"""Separate located evidence from model-proposed statements and counterevidence."""

from careerforge.application.coverage_service import evidence_verification
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.code_index.investigation import digest, is_test


def claims_for_review(matches: list[Json], counterevidence: list[Json], commit: str) -> Json:
    evidence: dict[str, Json] = {}

    def reference(point: Json, counter: bool = False) -> str:
        identity = digest(
            f"{commit}:{point['path']}:{point['start_line']}:{point['end_line']}:{point['quote']}:{counter}"
        )
        identifier = "evidence-" + identity[:24]
        evidence[identifier] = {
            "evidence_id": identifier,
            "commit": commit,
            **{k: point[k] for k in ("path", "start_line", "end_line", "quote")},
            "quote_hash": digest(point["quote"]),
            "file_hash": point.get("file_hash"),
            "citation_id": point.get("citation_id"),
            "purpose": "counterevidence"
            if counter
            else "test_source"
            if is_test(point["path"])
            else "implementation",
            "verification": evidence_verification(),
        }
        return identifier

    claims = []
    for point in matches:
        support = reference(point)
        claims.append(
            {
                "claim_id": "claim-"
                + digest(point["requirement_key"] + support + point["explanation"])[:24],
                "mechanism_id": point["requirement_key"],
                "statement": point["explanation"],
                "support_refs": [support],
                "counterevidence_refs": [
                    reference(c, True)
                    for c in counterevidence
                    if c["requirement_key"] == point["requirement_key"]
                ],
                "verification_kind": "literal_location",
                "review_status": "needs_semantic_review",
                "scope_and_limitations": (
                    "固定版本的已读摘录；解释与反证均为模型提议，未独立核验，未执行。"
                ),
            }
        )
    for point in counterevidence:
        reference(point, True)
    return {"claims": claims, "evidence": list(evidence.values())}
