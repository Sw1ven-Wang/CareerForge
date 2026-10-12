from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from careerforge.config.settings import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url.get_secret_value(), pool_pre_ping=True, hide_parameters=True
    )


def get_db() -> Iterator[Session]:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory() as session, session.begin():
        yield session
