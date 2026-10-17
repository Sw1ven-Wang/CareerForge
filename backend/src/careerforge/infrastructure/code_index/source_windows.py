"""Budgeted static windows: body chooses location, paths rank files, ties prefer earlier lines."""

import ast
from pathlib import PurePosixPath
from typing import Any

CODE_SUFFIXES = {
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
    ".sql",
}


def _symbols(path: str, content: str) -> list[tuple[int, int, str]]:
    if PurePosixPath(path).suffix.lower() != ".py":
        return []
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return []
    return sorted(
        (n.lineno, n.end_lineno or n.lineno, n.name)
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    )


def _window(
    lines: list[str],
    anchor: int,
    symbols: list[tuple[int, int, str]],
) -> dict[str, Any] | None:
    nodes = [(a, b) for a, b, _ in symbols if a <= anchor + 1 <= b]
    node = min(nodes, key=lambda span: (span[1] - span[0], span[0])) if nodes else None
    start = node[0] - 1 if node and node[1] - node[0] < 70 else max(0, anchor - 8)
    stop = min(len(lines), start + 70, node[1] if node else len(lines))
    quote, end = "", start
    for i in range(start, stop):
        value = lines[i] + "\n"
        if len(quote) + len(value) > 2600:
            break
        quote += value
        end = i + 1
    if not quote or not start <= anchor < end:
        return None
    return {"start_line": start + 1, "end_line": end, "content": quote.rstrip("\n")}


def excerpts(files: dict[str, str], requirements: list[dict[str, Any]]) -> dict[str, Any]:
    terms = list(
        dict.fromkeys(
            k.strip().lower()
            for r in requirements
            for k in [r["technology"], *r["keywords"]]
            if k.strip()
        )
    )
    ranked: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    repo_map: list[dict[str, Any]] = []
    for path, content in sorted(files.items()):
        if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
            continue
        lines = content.splitlines()
        symbols = _symbols(path, content)
        path_score = sum(t in path.lower() for t in terms)
        scores = [(sum(t in line.lower() for t in terms), i) for i, line in enumerate(lines)]
        anchors = sorted((s, i) for s, i in scores if s > 0)
        anchors.sort(key=lambda pair: (-pair[0], pair[1]))
        if not anchors and path_score and len(repo_map) < 12:
            repo_map.append(
                {
                    "path": path,
                    "reason": "path_only_match",
                    "code_read": False,
                    "symbols": [
                        {"name": n, "start_line": a, "end_line": b} for a, b, n in symbols[:8]
                    ],
                }
            )
        windows: list[dict[str, Any]] = []
        for score, anchor in anchors:
            if any(w["start_line"] <= anchor + 1 <= w["end_line"] for w in windows):
                continue
            window = _window(lines, anchor, symbols)
            if not window or any(
                window["start_line"] <= w["end_line"] and w["start_line"] <= window["end_line"]
                for w in windows
            ):
                continue
            windows.append(
                {
                    "path": path,
                    **window,
                    "content_score": score,
                    "path_score": path_score,
                    "selection": "body_anchor_or_python_symbol",
                }
            )
            if len(windows) == 2:
                break
        if windows:
            ranked.append((anchors[0][0], path_score, path, windows))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    # Give different files a first window before using second, disjoint windows.
    code = [row[3][slot] for slot in range(2) for row in ranked if len(row[3]) > slot][:6]
    docs = [
        {"path": p, "content": c[:5000], "truncated": len(c) > 5000}
        for p, c in sorted(files.items())
        if PurePosixPath(p).name.lower().startswith("readme")
    ][:2]
    return {
        "code": code,
        "documents": docs,
        "repo_map": repo_map,
        "selection_version": "body-symbol-windows-1",
        "coverage": "正文/符号定位至多6个静态窗口，每窗口最多70行/2600字符；不是完整审计。",
    }
