"""Reproducible engineering comparison; no live model or claimed human learning quality."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from careerforge.application.planning_service import exact_ids
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.code_index.lexical import rank
from careerforge.infrastructure.repository.snapshot_reader import catalog, read_curated


def evaluate():
    manifest = catalog()
    _, files = read_curated(manifest["github_url"], manifest["requested_ref"])
    inputs = [{"path": p, "content": text} for p, text, _ in files if p.endswith(".py")]
    cases = [
        {"query": "get_tuple", "expected": "get_tuple", "group": "saved_public_repo"},
        {"query": "状态恢复", "expected": "get_tuple", "group": "saved_public_repo"},
        {"query": "持久化", "expected": "put", "group": "saved_public_repo"},
        {"query": "nonexistent_zz_2026", "expected": None, "group": "negative"},
    ]
    observations = []
    for case in cases:
        direct = [f["path"] for f in inputs if case["query"].casefold() in f["content"].casefold()]
        indexed = rank(inputs, case["query"])
        expected = case["expected"]
        observations.append(
            {
                **case,
                "direct_file_hits": len(direct),
                "direct_expected_found": bool(direct) if expected else not direct,
                "ast_symbol_hits": len(indexed),
                "ast_expected_found": any(r["name"] == expected for r in indexed)
                if expected
                else not indexed,
                "locations": [
                    {
                        "path": r["path"],
                        "symbol": r["qualified_name"],
                        "start_line": r["start_line"],
                    }
                    for r in indexed[:3]
                ],
            }
        )
    complete = [UUID(int=1), UUID(int=2), UUID(int=3)]
    mutations = [
        complete,
        complete[:1],
        [complete[0], complete[0], complete[2]],
        [complete[0], complete[1], UUID(int=4)],
    ]
    guards = []
    for ids in mutations:
        try:
            exact_ids(complete, ids)
            accepted = True
        except BusinessError:
            accepted = False
        guards.append(
            {
                "criterion_ids": [str(i) for i in ids],
                "client_subset_baseline_accepts": bool(ids),
                "full_set_guard_accepts": accepted,
                "expected_accepts": ids == complete,
            }
        )
    return {
        "schema_version": 1,
        "mode": "offline_engineering_only",
        "repository_commit": manifest["requested_ref"],
        "files": manifest["files"],
        "input_hash": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "data_split": "development only; one repository; no holdout or calibration",
        "retrieval": observations,
        "criterion_guard_comparison": guards,
        "model_calls": 0,
        "model_cost": 0,
        "quality_claim": None,
        "decisions": {
            "retrieval": "keep lexical aliases/AST; no measured embedding benefit",
            "roles": "single bounded role; no live evidence for extra roles",
            "observability": "DB metadata retained; external Langfuse/MCP disabled",
        },
        "limitations": [
            "定义定位不代表引用支持机制结论",
            "synthetic guard comparison is not recommendation or model quality",
            "真实模型、独立校准/留出及作者迁移证据待授权或实际发生",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("evals/reports/offline_comparison.json")
    )
    args = parser.parse_args()
    result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(args.output),
                "retrieval_cases": len(result["retrieval"]),
                "guard_cases": len(result["criterion_guard_comparison"]),
                "model_calls": 0,
            }
        )
    )
