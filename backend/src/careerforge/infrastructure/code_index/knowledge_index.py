"""Deterministic structure/text index; no embeddings or inferred runtime edges."""

import ast
import re
from pathlib import PurePosixPath

from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.code_index.ast_index import symbols

VERSION = "code-knowledge-lexical-1.0"
TOPICS = {
    "工具调用": ["tool", "tools", "function", "dispatch", "execute"],
    "状态恢复": ["checkpoint", "restore", "resume", "state"],
    "持久化": ["persist", "storage", "checkpoint", "postgres", "sqlite"],
    "重试": ["retry", "backoff", "attempt"],
    "工作流": ["workflow", "graph", "node", "edge"],
    "检索": ["retrieve", "retrieval", "search", "embedding", "index"],
    "记忆": ["memory", "history", "context"],
    "流式": ["stream", "streaming", "yield"],
    "测试": ["test", "assert", "fixture"],
    "异步": ["async", "await", "asyncio"],
    "鉴权": ["auth", "token", "permission"],
    "多智能体": ["agent", "handoff", "transfer", "swarm"],
}


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    values = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text.lower())
    result: list[str] = []
    for value in values:
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            result.extend(value[i : i + 2] for i in range(max(1, len(value) - 1)))
        else:
            result.append(value)
    return result


def query_terms(query: str) -> tuple[list[str], list[str]]:
    result = tokens(query)
    aliases = []
    for topic, names in TOPICS.items():
        if topic in query:
            result.extend(names)
            aliases.append(topic)
    return list(dict.fromkeys(result))[:64], aliases


def chunks(path: str, content: str) -> list[Json]:
    lines = content.splitlines(keepends=True)
    count = len(lines)
    if not count:
        return []
    spans: list[tuple[int, int, str, str, str]] = []
    parsed: Json = (
        symbols(content) if path.endswith(".py") else {"status": "text_only", "symbols": []}
    )
    decorator_lines: dict[int, int] = {}
    if parsed["status"] == "parsed":
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                decorator_lines[node.lineno] = min(
                    [node.lineno, *[d.lineno for d in node.decorator_list]]
                )
    for item in parsed["symbols"]:
        first = decorator_lines.get(item["start_line"], item["start_line"])
        last = item["end_line"]
        if item["kind"] == "ClassDef":
            last = min(last, first + 39)
        spans.append(
            (max(1, first), last, item["qualified_name"], item["kind"], item.get("docstring") or "")
        )
    # File windows keep imports, module-level statements and unparsed languages searchable.
    spans.extend(
        (start, min(count, start + 99), path, "file_window", "")
        for start in range(1, count + 1, 80)
    )
    found = []
    for first, last, name, kind, doc in spans:
        start = first
        while start <= last:
            end = min(last, start + 159)
            text = "".join(lines[start - 1 : end])
            while len(text.encode()) > 28000 and end > start:
                end -= 1
                text = "".join(lines[start - 1 : end])
            if len(text.encode()) > 28000:
                start = end + 1
                continue
            references = []
            if path.endswith(".py") and kind != "file_window":
                try:
                    import textwrap

                    tree = ast.parse(textwrap.dedent(text))
                    references = sorted(
                        {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
                    )[:80]
                except (SyntaxError, RecursionError):
                    pass
            found.append(
                {
                    "path": path,
                    "symbol": name[:1000],
                    "kind": kind,
                    "start_line": start,
                    "end_line": end,
                    "content": text,
                    "payload": {
                        "language": PurePosixPath(path).suffix.lstrip("."),
                        "parse_status": parsed["status"],
                        "docstring": doc[:2000],
                        "references": references,
                        "relation_scope": "静态名称候选，不证明运行时调用",
                    },
                }
            )
            start = end + 1
    return found
