"""Compare aggregate checksums, without printing any stored account or material content."""

import argparse
import hashlib
import json

from careerforge.config.settings import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def manifest(database):
    url = make_url(get_settings().database_url.get_secret_value()).set(database=database)
    engine = create_engine(url, hide_parameters=True)
    output = {}
    with engine.connect() as connection:
        tables = (
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            )
            .scalars()
            .all()
        )
        for table in tables:
            # Table names come only from pg_catalog; quote them as SQL identifiers.
            quoted = engine.dialect.identifier_preparer.quote(table)
            rows = (
                connection.execute(
                    text(f"SELECT to_jsonb(t)::text FROM {quoted} t ORDER BY to_jsonb(t)::text")
                )
                .scalars()
                .all()
            )
            output[table] = {
                "rows": len(rows),
                "hash": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
            }
    engine.dispose()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("restored")
    args = parser.parse_args()
    original = manifest(args.source)
    copy = manifest(args.restored)
    result = {
        "source": args.source,
        "restored": args.restored,
        "identical": original == copy,
        "table_count": len(original),
        "row_count": sum(t["rows"] for t in original.values()),
        "content_exposed": False,
    }
    print(json.dumps(result))
    raise SystemExit(0 if result["identical"] else 1)
