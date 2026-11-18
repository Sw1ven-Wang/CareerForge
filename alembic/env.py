"""Fresh v1.5 schema only. Deliberately rejects the legacy database, never drops it."""

from sqlalchemy import create_engine, inspect, pool

from alembic import context
from careerforge.config import Settings
from careerforge.models import Base

config = context.config
settings = Settings.from_env()
settings.validate()
target_metadata = Base.metadata


def run():
    url = settings.database_url
    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        if tables & {"study_tracks", "study_artifacts", "users"}:
            raise RuntimeError(
                "Legacy CareerForge database detected. Use a NEW v1.5 database; no destructive implicit migration."
            )
        # Inspector autobegins a transaction; finish it before Alembic owns migration commit.
        conn.commit()
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
