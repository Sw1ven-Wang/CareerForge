#!/usr/bin/env python3
"""Exercise the production retrieval service against explicit synthetic judgments.

No paid calls or network. This is a runnable engineering benchmark, NOT human Gold
or evidence that real multilingual semantic quality is acceptable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def metrics(
    retrieved: list[str],
    relevant: list[str],
    projects: list[str],
    expected_projects: list[str],
    facets: list[str],
    expected_facets: list[str],
    k: int,
) -> dict[str, float | None]:
    if k < 1 or not relevant:
        raise ValueError("A positive K and explicit nonempty relevance judgments are required")
    ranked = list(dict.fromkeys(retrieved))[:k]
    relevant_set = set(relevant)
    hits = [i + 1 for i, identity in enumerate(ranked) if identity in relevant_set]
    dcg = sum(1 / math.log2(i + 1) for i in hits)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant_set))))
    return {
        "recall_at_k": len(hits) / len(relevant_set),
        "mrr": 1 / hits[0] if hits else 0.0,
        "ndcg_at_k": dcg / ideal,
        "project_scope_precision": sum(p in set(expected_projects) for p in projects)
        / len(projects)
        if projects
        else 0.0,
        "facet_coverage": len(set(facets) & set(expected_facets)) / len(set(expected_facets))
        if expected_facets
        else None,
    }


def percentile(values: list[float], percent: float) -> float:
    if not values or not 0 <= percent <= 100:
        raise ValueError("Invalid percentile input")
    ranked = sorted(values)
    position = (len(ranked) - 1) * percent / 100
    lower, upper = math.floor(position), math.ceil(position)
    return ranked[lower] + (ranked[upper] - ranked[lower]) * (position - lower)


async def benchmark(output: Path, repeats: int) -> dict:
    # Fixture adapters live outside the installable package by design.
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    from conftest import publish_fixture
    from support import FixtureModel, FixtureRepositories

    from careerforge.application import Application
    from careerforge.config import Settings
    from careerforge.contracts import RetrievalTaskSpec, TaskType

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="retrieval-fixture-", dir=output) as directory:
        model, repos = FixtureModel(), FixtureRepositories()
        settings = Settings(
            mode="test",
            database_url="sqlite+pysqlite:///" + directory + "/bench.db",
            allow_fixture_publication=True,
            embedding_dimensions=8,
            state_dir=Path(directory),
        )
        app = Application(settings, model, repos)
        owner = app.bootstrap()
        actor, ns = owner["principal_id"], owner["namespace_id"]
        funding = app.runtime.create_funding(actor, ns, 200)
        env = {
            "app": app,
            "actor": actor,
            "ns": ns,
            "funding": funding,
            "model": model,
            "repos": repos,
        }
        try:
            source_a, pub_a, _ = await publish_fixture(env, "bench-a", "fixtures/alpha")
            source_b, pub_b, _ = await publish_fixture(env, "bench-b", "fixtures/beta", "b" * 40)
            cases = [
                ("zh_alias", "工具调用的返回值", TaskType.CONCEPT, [source_a], [pub_a]),
                ("en_alias", "Tool Calling return", TaskType.CONCEPT, [source_b], [pub_b]),
                (
                    "project_compare",
                    "Tool Calling 实现比较",
                    TaskType.COMPARE,
                    [source_a, source_b],
                    [pub_a, pub_b],
                ),
                ("exact_symbol", "echo", TaskType.LOCATE, [source_a], [pub_a]),
            ]
            rows, latencies = [], []
            aggregates: dict[str, list[float]] = {}
            calls_before = len(model.calls)
            for name, question, task, projects, relevant in cases:
                samples = []
                for iteration in range(repeats):
                    spec = RetrievalTaskSpec(
                        turn_id=f"benchmark-{name}-{iteration}",
                        task_type=task,
                        user_question=question,
                        focus_revision=1,
                        project_source_ids=projects,
                        max_project_samples=2,
                        max_evidence_tokens=24000,
                    )
                    started = time.perf_counter()
                    result = await app.retrieval.retrieve(actor, spec)
                    samples.append((time.perf_counter() - started) * 1000)
                    units = result["knowledge_units"]
                    score = metrics(
                        [u["publication_id"] for u in units],
                        relevant,
                        [u["source_id"] for u in units],
                        projects,
                        [f for u in units for f in u.get("facet_coverage", {})],
                        [],
                        5,
                    )
                for key, value in score.items():
                    if value is not None:
                        aggregates.setdefault(key, []).append(value)
                latencies.extend(samples)
                rows.append(
                    {
                        "case": name,
                        "judgment_origin": "synthetic_fixture",
                        "k": 5,
                        **score,
                        "p50_ms": percentile(samples, 50),
                        "p95_ms": percentile(samples, 95),
                    }
                )
            result = {
                "schema_version": 1,
                "synthetic": True,
                "human_gold_validated": False,
                "live_semantic_quality_validated": False,
                "paid_external_calls": 0,
                "retrieval_model_calls": len(model.calls) - calls_before,
                "scope": "four synthetic cases; cold and warm cache mixed; not a production capacity test",
                "repeats": repeats,
                "cases": rows,
                "aggregate": {
                    key: mean(aggregates[key]) if key in aggregates else None
                    for key in (
                        "recall_at_k",
                        "mrr",
                        "ndcg_at_k",
                        "project_scope_precision",
                        "facet_coverage",
                    )
                },
                "p50_ms": percentile(latencies, 50),
                "p95_ms": percentile(latencies, 95),
            }
            (output / "fixture-benchmark.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
            return result
        finally:
            app.db.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be between 1 and 100")
    print(
        json.dumps(asyncio.run(benchmark(args.output, args.repeats)), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
