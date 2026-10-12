"""Append historical compatibility snapshots. Default is read-only; never infer missing raw."""

import argparse
from uuid import UUID

from careerforge.application.job_contract_service import from_legacy, job_for_source
from careerforge.infrastructure.db.models import Source, SourceSnapshot
from careerforge.infrastructure.db.session import get_engine
from sqlalchemy import select
from sqlalchemy.orm import Session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 100:
        parser.error("batch-size must be 1..100")
    cursor = None
    count = 0
    while True:
        with Session(get_engine()) as db, db.begin():
            query = (
                select(SourceSnapshot)
                .join(Source, Source.id == SourceSnapshot.source_id)
                .where(
                    SourceSnapshot.owner_id == args.owner_id,
                    Source.revoked_at.is_(None),
                    SourceSnapshot.metadata_values["source_type"].astext
                    == "job_posting",
                )
            )
            if cursor:
                query = query.where(SourceSnapshot.id > cursor)
            rows = list(
                db.scalars(query.order_by(SourceSnapshot.id).limit(args.batch_size))
            )
            if not rows:
                break
            for row in rows:
                if job_for_source(db, args.owner_id, row.id):
                    continue
                count += 1
                if args.apply:
                    from_legacy(db, args.owner_id, db.get(Source, row.source_id), row)
            cursor = rows[-1].id
    print(
        {
            "mode": "append_legacy_partial" if args.apply else "dry_run",
            "eligible_historical_snapshots": count,
            "raw_recovered": False,
        }
    )


if __name__ == "__main__":
    main()
