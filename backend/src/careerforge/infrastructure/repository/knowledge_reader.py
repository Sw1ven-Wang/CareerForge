"""Bounded public GitHub archives or explicitly submitted text. Never executes code."""

import io
import re
import stat
import time
import zipfile
from pathlib import PurePosixPath

import httpx

from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.repository.snapshot_reader import safe_path

MAX_FILE = 100_000
MAX_TOTAL = 12_000_000
MAX_FILES = 1000
MAX_ARCHIVE = 20_000_000
EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".cs",
    ".md",
    ".rst",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".html",
    ".css",
    ".xml",
}
BLOCKED = {
    ".git",
    ".env",
    ".aws",
    ".ssh",
    ".codex",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "vendor",
    ".idea",
}


def permitted(name):
    name = safe_path(name)
    parts = PurePosixPath(name).parts
    if len(name) > 1000 or any(ord(c) < 32 for c in name):
        return False
    if any(p.lower() in BLOCKED or p.lower().startswith(".env.") for p in parts):
        return False
    base = parts[-1].lower()
    return PurePosixPath(name).suffix.lower() in EXTENSIONS or base in {
        "license",
        "licence",
        "notice",
        "dockerfile",
        "makefile",
    }


def validate_submitted(files):
    if not files or len(files) > MAX_FILES:
        raise BusinessError(422, "KNOWLEDGE_FILE_LIMIT", "请提供 1–1000 个文本文件。")
    clean = {}
    size = 0
    for name, text in sorted(files.items()):
        normalized = safe_path(name)
        if normalized != name or not permitted(name) or name in clean:
            raise BusinessError(
                422, "KNOWLEDGE_UNSAFE_FILE", "包含不支持、私密或不规范的文件路径。"
            )
        encoded = text.encode()
        size += len(encoded)
        if not text.strip() or "\x00" in text or len(encoded) > MAX_FILE or size > MAX_TOTAL:
            raise BusinessError(
                413, "KNOWLEDGE_SIZE_LIMIT", "单文件最多 100 KB、总计 12 MB，且必须为非空文本。"
            )
        if any(len(line.encode()) > 25000 for line in text.splitlines()):
            raise BusinessError(
                413, "KNOWLEDGE_LINE_LIMIT", "单行超过 25 KB，无法提供可靠行号引用。"
            )
        clean[name] = text
    return clean


def unpack(data):
    files, skipped, total = {}, {}, 0

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(i.file_size for i in entries) > 100_000_000:
                raise BusinessError(
                    413, "KNOWLEDGE_ARCHIVE_LIMIT", "仓库归档过大，请导入选定的源码目录。"
                )
            roots = {i.filename.split("/")[0] for i in entries}
            if len(roots) != 1:
                raise BusinessError(422, "KNOWLEDGE_ARCHIVE_FORMAT", "归档必须有唯一项目根目录。")
            for info in sorted(entries, key=lambda i: i.filename):
                if info.is_dir():
                    continue
                name = info.filename.partition("/")[2]
                if not name:
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    skip("symlink")
                    continue
                if not permitted(name):
                    skip("excluded_type_or_directory")
                    continue
                if (
                    info.file_size > MAX_FILE
                    or len(files) >= MAX_FILES
                    or total + info.file_size > MAX_TOTAL
                ):
                    skip("size_or_count_limit")
                    continue
                raw = archive.read(info)
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    skip("non_utf8")
                    continue
                if "\x00" in text or not text.strip():
                    skip("binary_or_empty")
                    continue
                if name in files:
                    raise BusinessError(422, "KNOWLEDGE_DUPLICATE_PATH", "归档包含重复路径。")
                if any(len(line.encode()) > 25000 for line in text.splitlines()):
                    skip("oversized_line")
                    continue
                files[name] = text
                total += len(raw)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise BusinessError(422, "KNOWLEDGE_ARCHIVE_FORMAT", "无法读取 GitHub 归档。") from exc
    if not files:
        raise BusinessError(422, "KNOWLEDGE_EMPTY", "未找到可导入的源码或文档。")
    return files, skipped


def fetch_github(github_url, commit):
    # Fixed host and validated path; redirects disabled, no cookies or model secrets.
    match = re.fullmatch(r"https://github.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", github_url)
    if not match or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BusinessError(422, "KNOWLEDGE_INVALID_REPOSITORY", "仓库与 commit 无效。")
    url = f"https://codeload.github.com/{match[1]}/{match[2]}/zip/{commit}"
    data = bytearray()
    started = time.monotonic()
    try:
        with (
            httpx.Client(
                timeout=httpx.Timeout(20, connect=8), follow_redirects=False, trust_env=False
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise BusinessError(
                    422,
                    "KNOWLEDGE_DOWNLOAD_REJECTED",
                    "GitHub 无法提供该公开 commit，请检查地址或改用目录导入。",
                )
            for part in response.iter_bytes(65536):
                data.extend(part)
                if len(data) > MAX_ARCHIVE or time.monotonic() - started > 35:
                    raise BusinessError(
                        413, "KNOWLEDGE_DOWNLOAD_LIMIT", "下载超过 20 MB 或 35 秒，请导入选定目录。"
                    )
    except httpx.HTTPError as exc:
        raise BusinessError(
            502, "KNOWLEDGE_DOWNLOAD_FAILED", "GitHub 下载暂不可用，可改用本地目录副本导入。"
        ) from exc
    return unpack(bytes(data))
