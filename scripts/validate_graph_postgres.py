"""Run PostgreSQL Graph checks with a new disposable database per test.

Requires CREATE DATABASE on an explicit *_validation_* database URL and an empty
CAREERFORGE_PG_EVIDENCE output path. Test models are synthetic. Each created
cf550_validation_* database is removed after its test. The input DB is retained.
"""

import os
import pathlib
import subprocess
import xml.etree.ElementTree as ET
from uuid import uuid4

import psycopg
from sqlalchemy.engine import make_url

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = pathlib.Path(os.environ["CAREERFORGE_PG_EVIDENCE"]).resolve()
OUT.mkdir(parents=True, exist_ok=False)
files = [
    "test_langgraph_postgres_m1.py",
    "test_langgraph_postgres_m2.py",
    "test_session_graph_postgres.py",
    "test_session_controls_postgres.py",
    "test_learning_graph_postgresql_m4.py",
    "test_session_claim_postgresql_m6.py",
]
col = subprocess.run(
    ["python", "-m", "pytest", "--collect-only", "-q", *[str(ROOT / "tests" / f) for f in files]],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
)
nodes = [s.strip() for s in col.stdout.splitlines() if s.startswith("tests/") and "::" in s]
assert nodes
merged = ET.Element("testsuites")
failed = []

base = make_url(os.environ["CAREERFORGE_TEST_POSTGRES_URL"])
assert base.get_backend_name() == "postgresql" and "_validation_" in (base.database or ""), (
    "Disposable validation URL required"
)
admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
for index, node in enumerate(nodes):
    name = "cf550_validation_" + uuid4().hex[:12]
    with psycopg.connect(admin, autocommit=True) as db:
        db.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    try:
        env = {
            **os.environ,
            "CAREERFORGE_TEST_POSTGRES_URL": base.set(
                database=name, drivername="postgresql+psycopg"
            ).render_as_string(hide_password=False),
            "CAREERFORGE_TEST_GRAPH_POSTGRES_DSN": base.set(
                database=name, drivername="postgresql"
            ).render_as_string(hide_password=False),
        }
        with (OUT / f"{index}.log").open("w") as log:
            run = subprocess.run(
                [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    node,
                    "--junitxml=" + str(OUT / f"{index}.xml"),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        if run.returncode:
            failed.append(node)
        for suite in ET.parse(OUT / f"{index}.xml").getroot():
            merged.append(suite)
        print(index, node, run.returncode, flush=True)
    finally:
        with psycopg.connect(admin, autocommit=True) as db:
            db.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(name)
                )
            )
ET.ElementTree(merged).write(OUT / "junit.xml", encoding="utf-8", xml_declaration=True)
raise SystemExit(bool(failed))
