"""Bounded research slices retain the complete goal and an explicit backlog."""

from ..errors import DomainError
from ..util import fingerprint

CONTRACT = "jd-research-batch-v1"


def first_batch(snapshot, language_profile=None):
    rows = sorted(
        snapshot["requirements"], key=lambda r: (r.get("span_start", 0), r["requirement_id"])
    )
    first = rows[0].get("paragraph_id") if rows else None
    selected, topics = [], []
    for row in rows:
        if row.get("paragraph_id") != first:
            continue
        new_topics = list(
            dict.fromkeys([*topics, *([row["topic_key"]] if row.get("topic_key") else [])])
        )
        if len(selected) >= 30 or len(new_topics) > 15:
            break
        selected.append(row["requirement_id"])
        topics = new_topics
    value = {
        "contract": CONTRACT,
        "goal_version_id": snapshot["goal_version_id"],
        "goal_snapshot_hash": fingerprint(snapshot),
        "requirement_ids": selected,
        "remaining_requirement_ids": [
            r["requirement_id"] for r in rows if r["requirement_id"] not in selected
        ],
        "topic_keys": topics,
        "selection_basis": "first_jd_paragraph_in_source_order",
        "language_profile": language_profile,
    }
    return {**value, "batch_hash": fingerprint(value)}


def validate_batch(snapshot, batch):
    if (
        not isinstance(batch, dict)
        or batch.get("contract") != CONTRACT
        or batch.get("goal_version_id") != snapshot["goal_version_id"]
        or batch.get("goal_snapshot_hash") != fingerprint(snapshot)
        or batch.get("batch_hash")
        != fingerprint({k: v for k, v in batch.items() if k != "batch_hash"})
    ):
        raise DomainError("JD_RESEARCH_BATCH", "研究分批与完整目标不一致。", 409)
    if (
        batch != first_batch(snapshot, batch.get("language_profile"))
        or not batch["requirement_ids"]
    ):
        raise DomainError("JD_RESEARCH_BATCH", "研究分批不能丢失、重复或替换原要求。", 409)
    by_id = {r["requirement_id"]: r for r in snapshot["requirements"]}
    return [by_id[key] for key in batch["requirement_ids"]]
