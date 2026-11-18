"""Trusted backend entrypoint: assemble a safe URL from Compose's private DB components."""

from __future__ import annotations

import os
import sys
from urllib.parse import quote


def configure_database(env: dict[str, str]) -> None:
    host = env.get("CAREERFORGE_DATABASE_HOST")
    if not host:
        return
    if not host.replace("-", "").replace(".", "").isalnum():
        raise ValueError("invalid database service hostname")
    user = env.get("CAREERFORGE_DATABASE_USER", "careerforge")
    password = env.get("CAREERFORGE_DATABASE_PASSWORD", "")
    database = env.get("CAREERFORGE_DATABASE_NAME", "careerforge_v15")
    port = env.get("CAREERFORGE_DATABASE_PORT", "5432")
    if not password or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("database password and valid port are required")
    env["CAREERFORGE_DATABASE_URL"] = (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )
    env.pop("CAREERFORGE_DATABASE_PASSWORD", None)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("backend entrypoint requires a command")
    env = dict(os.environ)
    configure_database(env)
    os.execvpe(sys.argv[1], sys.argv[1:], env)


if __name__ == "__main__":
    main()
