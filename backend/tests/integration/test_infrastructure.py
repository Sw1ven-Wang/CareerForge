import os
from uuid import uuid4

import psycopg
import pytest
import redis
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.infrastructure


@pytest.fixture
def database_url():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL not configured; not an infrastructure pass")
    return value


def test_postgres_and_sqlalchemy(database_url):
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT current_database()").fetchone()[0]
    engine = create_engine(database_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_redis_roundtrip():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL not configured; not an infrastructure pass")
    client = redis.Redis.from_url(url)
    key = f"s00:{uuid4()}"
    try:
        assert client.ping()
        client.set(key, "roundtrip", ex=60)
        assert client.get(key) == b"roundtrip"
    finally:
        client.delete(key)
        client.close()
