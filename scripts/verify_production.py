"""Real PG/pgvector, Redis and isolated Celery delivery; never replaces services with fixtures.

Requires a NEW empty careerforge_v15_validation_* database and explicit consent.
No model credentials are inherited by the worker or dispatcher. Reports are additive.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PROVIDER_ENV = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "CAREERFORGE_ROLE_MODELS",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "RERANK_URL",
    "RERANK_API_KEY",
    "RERANK_MODEL",
    "GITHUB_TOKEN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
CHECKS = (
    "empty_database",
    "alembic_migration",
    "postgres_vector_sql",
    "redis_cross_client_singleflight",
    "celery_real_delivery",
    "no_provider_calls",
)


class Blocked(RuntimeError):
    """A safe-to-display fixed reason code, never a raw connection exception."""


def validate_targets(settings) -> None:
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    if settings.mode != "production" or url.get_backend_name() != "postgresql":
        raise Blocked("require_production_postgresql")
    if not re.fullmatch(r"careerforge_v15_validation_[a-zA-Z0-9_]+", url.database or ""):
        raise Blocked("require_dedicated_validation_database")
    if settings.redis_broker_url == settings.redis_cache_url:
        raise Blocked("require_separate_broker_and_cache")


def safe_environment(database_url: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for name in PROVIDER_ENV:
        env.pop(name, None)
    env.update(
        CAREERFORGE_ROLE_MODELS="{}",
        CAREERFORGE_STRUCTURED_OUTPUT="auto",
        PYTHONPATH=str(ROOT / "src"),
        PYTHONUNBUFFERED="1",
    )
    if database_url:
        env["CAREERFORGE_DATABASE_URL"] = database_url
    return env


def redact_log(text: str, secrets: list[str]) -> str:
    for value in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+", "[redacted-url]", text)[-100_000:]


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


async def cache_roundtrip(url: str) -> None:
    from careerforge.adapters.cache import RedisCache

    a, b = RedisCache(url), RedisCache(url)
    counter, key = 0, "validation-" + uuid4().hex

    async def load():
        nonlocal counter
        counter += 1
        await asyncio.sleep(0.1)
        return {"observed": True}

    try:
        require(await a.redis.ping(), "redis_ping_failed")
        results = await asyncio.wait_for(
            asyncio.gather(
                *[(a if i % 2 else b).singleflight("exact", key, load, 5) for i in range(12)]
            ),
            timeout=20,
        )
        require(
            counter == 1 and all(x == {"observed": True} for x in results), "singleflight_mismatch"
        )
    finally:
        await a.redis.aclose()
        await b.redis.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-infrastructure-test", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation" / "current" / ("production-" + uuid4().hex[:12]),
    )
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "production.json"
    if report_path.exists():
        parser.error("refusing to overwrite production.json; choose a new --output-dir")
    report = {
        "status": "blocked",
        "scope": "real_infrastructure_only_not_docker_or_model_quality",
        "started_at": datetime.now(UTC).isoformat(),
        "provider_calls_enabled": False,
        "real_provider_calls": None,
        "checks": {name: "not_run" for name in CHECKS},
    }
    worker = app = private_log = None
    stage = None
    secrets = []
    started = time.monotonic()
    try:
        if not args.confirm_infrastructure_test:
            raise Blocked("explicit_infrastructure_test_authorization_required")
        missing = [
            name
            for name in ("sqlalchemy", "alembic", "psycopg", "redis", "celery")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            report["missing_modules"] = missing
            raise Blocked("production_dependencies_missing")
        from sqlalchemy import create_engine, func, select, text
        from sqlalchemy.engine import make_url

        from careerforge.application import Application
        from careerforge.config import Settings
        from careerforge.models import AgentRun, ModelReceipt

        settings = Settings.from_env()
        validate_targets(settings)
        settings.validate()
        database_url = make_url(settings.database_url).update_query_dict({"connect_timeout": "5"})
        secrets = [
            make_url(url).password
            for url in (settings.database_url, settings.redis_broker_url, settings.redis_cache_url)
        ]
        settings = replace(
            settings,
            database_url=database_url.render_as_string(hide_password=False),
            model_base_url="",
            model_api_key="",
            model_name="",
            role_models={},
            embedding_base_url="",
            embedding_api_key="",
            embedding_model="",
            rerank_url="",
            rerank_api_key="",
            rerank_model="",
            github_token="",
            otlp_endpoint="",
        )
        env = safe_environment(settings.database_url)
        env.update(
            CAREERFORGE_MODE="production",
            CAREERFORGE_REDIS_BROKER_URL=settings.redis_broker_url,
            CAREERFORGE_REDIS_CACHE_URL=settings.redis_cache_url,
        )
        stage = "empty_database"
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                relations = connection.scalar(
                    text("""SELECT count(*) FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                    AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')""")
                )
                if relations:
                    raise Blocked("validation_database_is_not_empty")
        finally:
            engine.dispose()
        report["checks"][stage] = "passed"
        stage = "alembic_migration"
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        (output / "migration.log").write_text(
            redact_log(migrated.stdout + migrated.stderr, secrets), encoding="utf-8"
        )
        require(migrated.returncode == 0, "migration_failed")
        report["checks"][stage] = "passed"
        app = Application(settings)
        owner = app.bootstrap("infra-" + uuid4().hex[:8])
        stage = "postgres_vector_sql"
        with app.db.read() as db:
            require(
                bool(db.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))),
                "vector_missing",
            )
            distance = db.scalar(text("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector"))
            require(float(distance) == 0, "vector_distance_mismatch")
        report["checks"][stage] = "passed"
        stage = "redis_cross_client_singleflight"
        asyncio.run(cache_roundtrip(settings.redis_cache_url))
        report["checks"][stage] = "passed"
        stage = "celery_real_delivery"
        funding = app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 1)
        run = app.runtime.create(
            owner["principal_id"],
            owner["namespace_id"],
            funding,
            "analysis",
            # A zero-source analysis intentionally tests queue/worker delivery only.
            # Freeze the explicit empty taxonomy contract; legacy manifests are refused.
            {"source_ids": [], "taxonomy_versions": {}, "taxonomy_data_classes": []},
            0,
            "infra-" + uuid4().hex,
        )
        queue = "cf-validation-" + uuid4().hex
        private_log = tempfile.TemporaryFile(mode="w+b")
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "careerforge.worker:celery_app",
                "worker",
                "-Q",
                queue,
                "--hostname",
                queue + "@%h",
                "--pool=solo",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--loglevel=WARNING",
            ],
            cwd=ROOT,
            env=env,
            stdout=private_log,
            stderr=subprocess.STDOUT,
        )
        dispatched = subprocess.run(
            [
                sys.executable,
                "-c",
                f"from careerforge.worker import dispatch_once; dispatch_once(queue={queue!r})",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (output / "dispatcher.log").write_text(
            redact_log(dispatched.stdout + dispatched.stderr, secrets), encoding="utf-8"
        )
        require(dispatched.returncode == 0, "dispatcher_failed")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            require(worker.poll() is None, "worker_exited_early")
            with app.db.read() as db:
                status = db.get(AgentRun, run).status
            if status == "succeeded":
                break
            require(
                status not in {"failed", "cancelled", "budget_stopped", "partially_succeeded"},
                "celery_task_not_succeeded",
            )
            time.sleep(0.25)
        else:
            raise RuntimeError("celery_delivery_deadline_exceeded")
        report["checks"][stage] = "passed"
        stage = "no_provider_calls"
        with app.db.read() as db:
            count = db.scalar(select(func.count()).select_from(ModelReceipt))
        report["real_provider_calls"] = count
        require(count == 0, "unexpected_model_receipts")
        report["checks"][stage] = "passed"
        report["status"] = "passed_infrastructure_only"
    except Blocked as exc:
        report["reason_code"] = str(exc)
        if stage:
            report["checks"][stage] = "blocked"
    except Exception as exc:
        report["status"] = "failed"
        report["reason_code"] = "check_failed"
        report["error_type"] = type(exc).__name__
        if stage:
            report["checks"][stage] = "failed"
            report["failed_stage"] = stage
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        if private_log is not None:
            private_log.seek(0)
            raw = private_log.read().decode("utf-8", "replace")
            (output / "celery.log").write_text(redact_log(raw, secrets), encoding="utf-8")
            private_log.close()
        if app is not None:
            app.db.engine.dispose()
        report.update(
            finished_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed_infrastructure_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
