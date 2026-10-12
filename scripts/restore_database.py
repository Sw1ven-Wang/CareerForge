"""Restore only into a NEW isolated database; never overwrite a running workspace."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"careerforge_restore_[a-z0-9_]+", args.database):
        raise SystemExit("Restore requires a new careerforge_restore_ database name")
    raw = args.backup.read_bytes()
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "createdb",
            "-U",
            "careerforge",
            args.database,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "careerforge",
            "--no-owner",
            "--exit-on-error",
            "--dbname",
            args.database,
        ],
        input=raw,
        check=True,
        capture_output=True,
    )
    print(
        json.dumps(
            {"database": args.database, "restored": True, "original_database_untouched": True}
        )
    )


if __name__ == "__main__":
    main()
