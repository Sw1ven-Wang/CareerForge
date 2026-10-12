import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from careerforge.domain.errors import BusinessError

MAX_FILE = 100000
MAX_TOTAL = 500000
ROOT = Path(__file__).resolve().parents[2] / "data/curated"


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or ":" in value
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise BusinessError(422, "UNSAFE_REPOSITORY_PATH", "路径超出仓库文本范围。")
    return str(path)


def catalog() -> dict[str, Any]:
    value: dict[str, Any] = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    return {
        "github_url": value["repository"],
        "requested_ref": value["commit"],
        "coverage": value["coverage"],
        "mode": "offline_previously_captured",
        "files": value["files"],
    }


def read_curated(url: str, ref: str) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    manifest = catalog()
    if url != manifest["github_url"] or ref != manifest["requested_ref"]:
        raise BusinessError(
            422,
            "OFFLINE_SNAPSHOT_UNAVAILABLE",
            "离线模式无此固定版本；请选已保存候选。不会将 main 冒充当前已解析版本。",
        )
    rows = []
    total = 0
    root = (ROOT / "files").resolve()
    for item in manifest["files"]:
        path = safe_path(item["path"])
        target = root / path
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise BusinessError(422, "UNSAFE_REPOSITORY_PATH", "不读取符号链接。")
        raw = target.read_bytes()
        total += len(raw)
        if len(raw) > MAX_FILE or total > MAX_TOTAL:
            raise BusinessError(413, "REPOSITORY_BUDGET_EXCEEDED", "代码读取范围超过预算。")
        sha = hashlib.sha256(raw).hexdigest()
        if sha != item["sha256"]:
            raise BusinessError(409, "REPOSITORY_HASH_MISMATCH", "保存文件与固定版本清单不一致。")
        rows.append((path, raw.decode("utf-8"), sha))
    return manifest, rows
