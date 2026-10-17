"""Deterministic context packing; complete code quotes or explicit omission."""

import json
from copy import deepcopy

from careerforge.application.common import fingerprint
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.schemas.context import ContextManifest

CONTEXT_CHARS = 28000


def encode(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assemble(
    task: Json,
    working: Json,
    evidence: list[Json],
    learning: list[Json],
    sources: list[Json],
    observations: list[Json],
    recalls: list[Json],
    skills: list[Json],
    final: bool,
) -> Json:
    packet = deepcopy(
        {
            **working,
            "fixed": {
                "task": task,
                "permissions_source": "server",
                "inputs_untrusted": True,
                "competence_verified": False,
            },
            "history": [e for e in evidence if e["kind"] == "tutor_turn"],
            "learning_records": learning,
            "read_sources": sources,
            "observations": observations,
            "recalled_evidence": recalls,
            "loaded_skills": skills,
            "final": final,
        }
    )
    manifest = ContextManifest.model_validate(
        {
            "task": task,
            "areas": {
                "fixed": {"source": "server", "compressed": False},
                "working": {"question": "question", "module": "module", "pending": "pending"},
                "evidence": {"catalog_count": len(evidence), "read_code": len(sources)},
                "learning": {
                    "record_count": len(learning),
                    "source": "observations_and_declarations",
                    "ability_inferred": False,
                },
            },
            "evidence": evidence,
            "omitted": [],
            "payload_hash": "pending",
            "measurement": {
                "method": "utf8_bytes_upper_estimate",
                "estimated_input_tokens": 0,
                "actual_provider_usage": "see_model_receipts",
                "hard_context_chars": CONTEXT_CHARS,
            },
        }
    ).model_dump(mode="json")
    packet["context_manifest"] = manifest
    # Summaries remain addressable after eviction. Never trim a quote into a different quote.
    for key in ("observations", "recalled_evidence", "learning_records", "history", "read_sources"):
        while len(encode(packet)) > CONTEXT_CHARS - 200 and packet[key]:
            value = packet[key].pop(0)
            manifest["omitted"].append(
                str(value.get("evidence_id") or value.get("citation_id") or key)
            )
    manifest["areas"]["evidence"]["read_code"] = len(packet["read_sources"])
    manifest["areas"]["learning"]["record_count"] = len(packet["learning_records"])
    manifest["payload_hash"] = fingerprint(
        {k: v for k, v in packet.items() if k != "context_manifest"}
    )
    manifest["measurement"]["estimated_input_tokens"] = len(encode(packet).encode("utf-8")) + 100
    if len(encode(packet)) > CONTEXT_CHARS:
        raise BusinessError(413, "FIXED_CONTEXT_LIMIT", "固定任务上下文过长，请缩小模块范围。")
    return packet
