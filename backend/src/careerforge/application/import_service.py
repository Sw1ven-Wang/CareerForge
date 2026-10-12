from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from careerforge.infrastructure.db.models import JobPostingVersion, SourceSnapshot


def materialize_job(db: Session, snapshot: SourceSnapshot) -> JobPostingVersion | None:
    if snapshot.metadata_values.get("source_type") != "job_posting":
        return None
    db.execute(
        insert(JobPostingVersion)
        .values(owner_id=snapshot.owner_id, source_id=snapshot.source_id, snapshot_id=snapshot.id)
        .on_conflict_do_nothing(index_elements=["snapshot_id"])
    )
    return db.scalar(select(JobPostingVersion).where(JobPostingVersion.snapshot_id == snapshot.id))
