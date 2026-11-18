from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from .errors import DomainError


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def fingerprint(value: Any) -> str:
    return digest(canonical(value))


BLOCKED_PARTS = {
    ".git",
    ".env",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".codex",
    ".agents",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "vendor",
}


def safe_path(value: str) -> str:
    if (
        not value
        or len(value) > 1000
        or "\\" in value
        or ":" in value
        or PurePosixPath(value).is_absolute()
        or any(ord(c) < 32 for c in value)
    ):
        raise DomainError("UNSAFE_PATH", "文件路径无效。", 422)
    parts = value.split("/")
    if any(
        p in {"", ".", ".."} or p.lower() in BLOCKED_PARTS or p.lower().startswith(".env.")
        for p in parts
    ):
        raise DomainError("UNSAFE_PATH", "文件路径越界或属于私密/依赖目录。", 422)
    return value


# Imports and archive/CLI ingestion share a bounded whole-file contract.
# Large source modules remain readable through the existing 70-line/2600-char
# evidence pages; raising this import bound never expands a model read page.
MAX_SOURCE_FILE_BYTES = 1_000_000
MAX_SOURCE_TOTAL_BYTES = 12_000_000
MAX_SOURCE_FILES = 1000


def validate_files(
    files: dict[str, str],
    max_files: int = MAX_SOURCE_FILES,
    max_bytes: int = MAX_SOURCE_TOTAL_BYTES,
) -> None:
    if not files or len(files) > max_files:
        raise DomainError("FILE_LIMIT", "文件数量超过合同限制。", 413)
    total = 0
    for path, body in files.items():
        safe_path(path)
        data = body.encode("utf-8")
        total += len(data)
        if b"\x00" in data or len(data) > MAX_SOURCE_FILE_BYTES or total > max_bytes:
            raise DomainError("SOURCE_SIZE", "只能导入有界 UTF-8 文本。", 413)
        if any(len(line.encode()) > 25000 for line in body.splitlines()):
            raise DomainError("LINE_SIZE", "单行过长。", 413)


def manifest(files: dict[str, str]) -> str:
    return fingerprint({p: digest(c) for p, c in sorted(files.items())})


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    result: list[str] = []
    for word in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            result += [word[i : i + 2] for i in range(max(1, len(word) - 1))]
        else:
            result.append(word)
    return list(dict.fromkeys(result))
