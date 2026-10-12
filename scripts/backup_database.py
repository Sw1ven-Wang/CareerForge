"""Binary-safe local Docker pg_dump; output stays private by default."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="careerforge_test")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"careerforge_[a-z0-9_]+", args.database):
        raise SystemExit("Expected an explicit careerforge_ database name")
    if args.output.exists():
        raise SystemExit("Destination exists; choose a new backup filename")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "careerforge",
            "-Fc",
            "--no-owner",
            "--dbname",
            args.database,
        ],
        capture_output=True,
        check=True,
    )
    with args.output.open("xb") as stream:
        stream.write(result.stdout)
    print(
        json.dumps(
            {
                "database": args.database,
                "backup": str(args.output),
                "bytes": len(result.stdout),
                "sha256": hashlib.sha256(result.stdout).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
