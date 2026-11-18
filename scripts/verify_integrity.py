"""Verify the packaged source payload against MANIFEST.sha256 (not a signature)."""

from __future__ import annotations

import hashlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = root / "MANIFEST.sha256"
if not manifest.is_file():
    raise SystemExit("MANIFEST.sha256 is missing; this script expects the delivery archive.")
failed = []
count = 0
for line in manifest.read_text(encoding="utf-8").splitlines():
    expected, name = line.split("  ", 1)
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        failed.append(name)
        continue
    count += 1
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        failed.append(name)
print(f"Checked {count} files; mismatches: {len(failed)}")
for name in failed:
    print(name)
raise SystemExit(1 if failed else 0)
