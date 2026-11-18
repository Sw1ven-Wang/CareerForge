"""Read-only input snapshot replay of a public observed retrieval defect.

Only a SQLite backup is mutated. No models, repository network requests,
expected-answer injection, source/pin expansion, or evidence-budget increase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import RetrievalTaskSpec
from careerforge.models import ObjectRow, PolicyVersion
from careerforge.util import canonical, digest, fingerprint


class NoCallProvider:
    synthetic = False
    supports_embeddings = False
    supports_reranking = False
    profile_version = "native-astra-max-isolated-role-bridge-20261105-v1"

    async def complete(self, request):
        raise AssertionError("Read-only input replay must not call a model")


async def replay(args):
    database = args.evidence / "run.sqlite"
    before_hash = digest(database.read_bytes())
    with tempfile.TemporaryDirectory(prefix="cf-focused-retrieval-") as temp:
        work = Path(temp)
        copy_path = work / "copied.sqlite"
        original = sqlite3.connect("file:" + str(database.resolve()) + "?mode=ro", uri=True)
        copied = sqlite3.connect(copy_path)
        original.backup(copied)
        copied.close()
        original.close()
        app = Application(
            Settings(
                mode="development",
                database_url="sqlite+pysqlite:///" + str(copy_path),
                state_dir=work,
                embedding_dimensions=8,
            ),
            provider=NoCallProvider(),
        )
        try:
            actor = json.loads((args.evidence / "identity.json").read_text())["principal_id"]
            with app.db.read() as db:
                payload = db.get(ObjectRow, args.selection).payload
                spec = RetrievalTaskSpec.model_validate(payload["task_spec"])
                recipe = db.get(PolicyVersion, payload["policy_version"]).content
            result = await app.retrieval.retrieve(
                actor, spec, None, recipe, payload["policy_version"]
            )
            with app.db.read() as db:
                manifest = db.get(ObjectRow, result["retrieval_manifest_id"]).payload
                packs_unchanged = all(
                    fingerprint(unit)
                    == fingerprint(
                        app.knowledge.publication_pack(db, actor, unit["publication_id"])
                    )
                    for unit in result["knowledge_units"]
                )
            report = {
                "input_selection_id": args.selection,
                "input_task_spec": spec.model_dump(mode="json"),
                "original_publication_ids": payload["publication_ids"],
                "selected_publication_ids": manifest["publication_ids"],
                "selected_fact_ids": [
                    fact["fact_atom_id"]
                    for unit in result["knowledge_units"]
                    for fact in unit["facts"]
                ],
                "selected_citations": [
                    {
                        key: citation[key]
                        for key in (
                            "citation_id",
                            "source_id",
                            "repository",
                            "commit",
                            "path",
                            "start_line",
                            "end_line",
                            "file_hash",
                            "quote_hash",
                        )
                    }
                    for unit in result["knowledge_units"]
                    for citation in unit["citations"]
                ],
                "selected_bytes": sum(
                    len(canonical(unit).encode("utf-8")) for unit in result["knowledge_units"]
                ),
                "budget_unchanged": spec.max_evidence_tokens
                == payload["task_spec"]["max_evidence_tokens"],
                "source_scope_unchanged": manifest["task_spec"]["project_source_ids"]
                == spec.project_source_ids,
                "hard_topics_unchanged": manifest["task_spec"]["topic_keys"] == spec.topic_keys,
                "packs_equal_fresh_validated_publications": packs_unchanged,
                "model_calls": 0,
                "query_normalization": result["query_normalization"],
                "ranking_trace": manifest["ranking_trace"],
                "omitted_publications": manifest["omitted_publications"],
                "input_database_unchanged": digest(database.read_bytes()) == before_hash,
                "input_database_sha256": before_hash,
                "interpretation": "retrieval mechanism only; no new live tutor/checker response",
            }
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            assert report["input_database_unchanged"] and packs_unchanged
            assert report["source_scope_unchanged"] and report["hard_topics_unchanged"]
            assert report["selected_bytes"] <= spec.max_evidence_tokens
            if args.expected_fact:
                assert args.expected_fact in report["selected_fact_ids"], (
                    "Expected already-published fact was not retrieved"
                )
        finally:
            app.db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-fact")
    asyncio.run(replay(parser.parse_args()))
