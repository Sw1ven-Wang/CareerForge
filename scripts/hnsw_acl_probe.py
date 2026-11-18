#!/usr/bin/env python3
"""Opt-in PostgreSQL/pgvector HNSW configuration and ACL-filtered recall probe.

Default only emits bounded configuration SQL. --apply-index requires local DB
operator permission; it builds a rebuildable projection, not knowledge authority.
--probe compares approximate IDs with exact distances after the SAME current
actor/publication/version authorization filter. It never calls a model.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from typing import Any

from sqlalchemy import bindparam, text

from careerforge.config import Settings
from careerforge.db import Database
from careerforge.errors import DomainError
from careerforge.services.knowledge import KnowledgeService
from careerforge.services.runtime import Runtime
from careerforge.services.security import authorization_revision, principal
from careerforge.util import canonical, fingerprint


def configuration(profile: str, dimensions: int, ef_search: int = 100) -> dict[str, Any]:
    if (
        not 1 <= dimensions <= 2000
        or not 1 <= ef_search <= 1000
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", profile)
    ):
        raise ValueError("Profile, dimensions or ef_search outside bounded configuration")
    name = "cf_hnsw_" + fingerprint({"profile": profile, "dimensions": dimensions})[:20]
    literal = profile.replace("'", "''")
    return {
        "index_name": name,
        "model_profile": profile,
        "dimensions": dimensions,
        "ef_search": ef_search,
        "m": 16,
        "ef_construction": 64,
        "iterative_scan": "strict_order",
        "requires_pgvector": ">=0.8.0",
        "sql": f"CREATE INDEX IF NOT EXISTS {name} ON knowledge_embeddings USING hnsw "
        f"((vector::vector({dimensions})) vector_cosine_ops) WITH (m=16, ef_construction=64) "
        f"WHERE model_version='{literal}' AND dimensions={dimensions}",
        "tuning_validated": False,
    }


def probe(
    db: Database,
    actor: str,
    profile: str,
    query: list[float],
    *,
    k: int = 10,
    ef_search: int = 100,
    apply_index: bool = False,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    config = configuration(profile, len(query), ef_search)
    if db.engine.dialect.name != "postgresql" or not 1 <= k <= 100:
        raise DomainError("ANN_PROBE", "需要PostgreSQL及1..100的K值。", 422)
    if any(not math.isfinite(n) for n in query) or not any(query):
        raise DomainError("ANN_VECTOR", "需要有限、非零查询向量。", 422)
    knowledge = KnowledgeService(db, Runtime(db))
    with db.transaction() as session:
        if not principal(session, actor).is_admin:
            raise DomainError("FORBIDDEN", "索引运维探针需要管理员。", 403)
        revision = authorization_revision(session, actor)
        version = str(
            session.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        )
        parts = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
        if not parts or tuple(map(int, parts.groups())) < (0, 8, 0):
            raise DomainError("ANN_VERSION", "iterative_scan探针要求pgvector>=0.8.0。", 409)
        publications = knowledge.selected_publications(session, actor, source_ids or [])
        ids = []
        for publication in publications:
            knowledge.publication_pack(session, actor, publication.id)
            ids.append(publication.id)
        if apply_index:
            session.execute(text(config["sql"]))
        index_kind = session.scalar(
            text(
                "SELECT am.amname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_am am ON am.oid=c.relam WHERE c.relname=:name AND n.nspname=current_schema()"
            ),
            {"name": config["index_name"]},
        )
        if index_kind is not None and index_kind != "hnsw":
            raise DomainError("ANN_INDEX", "命名索引不是HNSW，不能声称近似查询验证。", 409)
        if not ids:
            return {
                "configuration": config,
                "eligible": 0,
                "recall_at_k": None,
                "ann_exercised": False,
            }
        dims = len(query)
        sql = (
            f"SELECT publication_id FROM knowledge_embeddings WHERE publication_id IN :ids "
            f"AND model_version=:profile AND dimensions={dims} "
            f"ORDER BY (vector::vector({dims})) <=> CAST(:query AS vector({dims})) LIMIT :k"
        )
        stmt = text(sql).bindparams(bindparam("ids", expanding=True))
        params = {"ids": ids, "profile": profile, "query": canonical(query), "k": k}
        session.execute(text("SET LOCAL enable_indexscan=off"))
        session.execute(text("SET LOCAL enable_bitmapscan=off"))
        exact = list(session.scalars(stmt, params))
        session.execute(text("SET LOCAL enable_indexscan=on"))
        session.execute(text("SET LOCAL enable_seqscan=off"))
        session.execute(
            text("SELECT set_config('hnsw.ef_search', :value, true)"), {"value": str(ef_search)}
        )
        session.execute(text("SET LOCAL hnsw.iterative_scan='strict_order'"))
        plan = session.scalar(
            text("EXPLAIN (FORMAT JSON) " + sql).bindparams(bindparam("ids", expanding=True)),
            params,
        )
        approx = list(session.scalars(stmt, params))
        if not set(approx) <= set(ids) or authorization_revision(session, actor) != revision:
            raise DomainError("ANN_SCOPE", "探针范围发生改变。", 409)
        exercised = index_kind == "hnsw" and config["index_name"] in canonical(plan)
        return {
            "configuration": config,
            "pgvector_version": version,
            "eligible": len(ids),
            "k": k,
            "exact_ids": exact,
            "approximate_ids": approx,
            "ann_exercised": exercised,
            "recall_at_k": len(set(exact) & set(approx)) / len(exact)
            if exact and exercised
            else None,
            "unauthorized_returned": 0,
            "plan": plan,
            "human_gold_validated": False,
            "semantic_quality_validated": False,
            "note": "distance recall only; no default ANN enablement or quality claim",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--dimensions", type=int, required=True)
    parser.add_argument("--ef-search", type=int, default=100)
    parser.add_argument("--actor")
    parser.add_argument("--query", help="JSON numeric vector, required for --probe")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--apply-index", action="store_true")
    args = parser.parse_args()
    if not args.probe:
        if args.apply_index:
            parser.error("--apply-index requires --probe and an authorized actor")
        result = configuration(args.profile, args.dimensions, args.ef_search)
    else:
        if not args.actor or not args.query:
            parser.error("--probe requires --actor and --query")
        query = json.loads(args.query)
        if (
            not isinstance(query, list)
            or len(query) != args.dimensions
            or any(type(v) not in {int, float} for v in query)
        ):
            parser.error("query must be a numeric vector of exactly --dimensions elements")
        db = Database(Settings.from_env())
        try:
            result = probe(
                db,
                args.actor,
                args.profile,
                query,
                ef_search=args.ef_search,
                apply_index=args.apply_index,
            )
        finally:
            db.engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
