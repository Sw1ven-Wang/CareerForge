"""Package only whitelisted public engineering evidence; never include private materials."""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/CareerForge_V1_Offline_Evidence.zip"
    )
    args = parser.parse_args()
    files = [ROOT / "README.md", ROOT / "NOTICE.md", ROOT / "docs/progress/V1_DELIVERY.md"]
    files.extend([ROOT / "docs/progress/V1_ENVIRONMENT.json", ROOT / "docs/progress/V1_BACKUP_RESTORE.json"])
    for stage in range(4, 12):
        files.append(ROOT / f"docs/progress/S{stage:02}_REPORT.md")
    for directory in ["docs/operations", "docs/interview", "docs/demo", "evals/reports"]:
        files.extend(p for p in (ROOT / directory).rglob("*") if p.is_file())
    files.extend((ROOT / "docs/progress").glob("V1*.xml"))
    files.extend((ROOT / "scripts").glob("*.py"))
    files.append(ROOT / "docs/decisions/0002-explicit-workflows.md")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    manifest = {
        "mode": "offline_engineering_evidence",
        "source_commit": commit,
        "real_learning_verified": False,
        "private_content_included": False,
        "files": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(files)):
            name = path.relative_to(ROOT).as_posix()
            raw = path.read_bytes()
            bundle.writestr(name, raw)
            manifest["files"].append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
        bundle.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "archive": str(args.output),
                "files": len(files),
                "source_commit": commit,
                "private_content_included": False,
            }
        )
    )


if __name__ == "__main__":
    main()
