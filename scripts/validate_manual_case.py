"""Verify captured evidence; this checks integrity, not learning or model quality."""

import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    base = root / "evals/datasets/manual_case"
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["origin_type"] == "real"
    assert len(manifest["job_sources"]) >= 3
    for record in manifest["job_sources"]:
        body = (base / record["content_path"]).read_text(encoding="utf-8")
        assert hashlib.sha256(body.encode()).hexdigest() == record["sha256"]
        assert record["read_status"] == "available"
    requirement = manifest["requirement"]
    source = next(
        item for item in manifest["job_sources"] if item["id"] == requirement["source_id"]
    )
    body = (base / source["content_path"]).read_text(encoding="utf-8")
    assert body[requirement["char_start"] : requirement["char_end"]] == requirement["quote"]
    reference = json.loads((base / "reference_manifest.json").read_text(encoding="utf-8"))
    assert len(reference["commit"]) == 40
    for item in reference["files"]:
        data = (base / "reference" / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    assert any(item["path"] == "LICENSE" for item in reference["files"])
    assert manifest["personal_evidence"]["ability_verdict"] == "unknown"
    assert all(item["result"] == "not_executed" for item in manifest["acceptance_criteria"])
    print("Manual case: hashes, original quote, fixed commit and unknown boundaries verified")


if __name__ == "__main__":
    main()
