from alembic import context
from careerforge.infrastructure.db.models import Base
from careerforge.infrastructure.db.session import get_engine

with get_engine().connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        include_object=lambda obj, name, kind, reflected, compare: (
            not (kind == "table" and reflected and name not in Base.metadata.tables)
        ),
    )
    with context.begin_transaction():
        context.run_migrations()
