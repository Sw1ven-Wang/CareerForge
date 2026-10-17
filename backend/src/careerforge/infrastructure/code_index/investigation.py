"""Bounded, deterministic Python navigation. Candidates never imply execution or support."""

import ast
import hashlib
import re
from bisect import bisect_left
from pathlib import PurePosixPath

from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.code_index.source_windows import CODE_SUFFIXES, excerpts
from careerforge.infrastructure.repository.knowledge_reader import validate_submitted
from careerforge.schemas.investigation import FinishArgs, QueryArgs, SpanArgs, TargetArgs, TreeArgs
from pydantic import ValidationError

VERSION = "code-investigation-1"
TOOL_LIMIT = 12
READ_LIMIT = 6
LINE_LIMIT = 70
CHAR_LIMIT = 2600
RESULT_LIMIT = 12
SYMBOL_LIMIT = 6000
CALL_LIMIT = 20000
SEARCH_LIMIT = 256
RELATION_SCOPE = "static_name_candidate; not a resolved or runtime call graph"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def words(query: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-zA-Z0-9_.]+|[\u4e00-\u9fff]+", query.lower())))[:24]


def is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in {"test", "tests", "testing"} for part in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
    )


class CodeInvestigation:
    def __init__(self, files: dict[str, str], requirements: list[Json]) -> None:
        self.files = validate_submitted(files)
        self.requirements = requirements
        self.file_hashes = {p: digest(c) for p, c in self.files.items()}
        self.archive_hash = digest("\n".join(f"{p}:{h}" for p, h in self.file_hashes.items()))
        self.targets: dict[str, Json] = {}
        self.discovered: set[str] = set()
        self.symbols: list[Json] = []
        self.calls: list[tuple[str, int, str]] = []
        self.parse_failures: list[str] = []
        self.index_truncated = False
        self.reads: list[Json] = []
        self.trace: list[Json] = []
        self.observations: list[Json] = []
        self.tool_count = 0
        self.stop_reason: str | None = None
        self.stop_explanation = ""
        self.decisions = 0
        for path, content in self.files.items():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(content)
            except (SyntaxError, ValueError, RecursionError):
                self.parse_failures.append(path)
                continue
            try:
                self._index(tree, path)
            except RecursionError:
                self.parse_failures.append(path)
                self.index_truncated = True
        self.baseline = excerpts(self.files, requirements)

    def _target(
        self, path: str, first: int, last: int, symbol: str = "", kind: str = "span"
    ) -> Json:
        identifier = "target-" + digest(f"{self.archive_hash}:{path}:{first}:{last}:{symbol}")[:24]
        value = {
            "target_id": identifier,
            "path": path,
            "start_line": first,
            "end_line": last,
            "symbol": symbol,
            "kind": kind,
            "is_test": is_test(path),
            "file_hash": self.file_hashes[path],
            "code_read": False,
            "support_scope": RELATION_SCOPE,
        }
        self.targets[identifier] = value
        return value

    def _index(self, tree: ast.AST, path: str, prefix: str = "") -> None:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                first = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                if len(self.symbols) >= SYMBOL_LIMIT:
                    self.index_truncated = True
                    continue
                self.symbols.append(
                    self._target(
                        path, first, node.end_lineno or node.lineno, name, type(node).__name__
                    )
                )
                self._index(node, path, name + ".")
            else:
                if isinstance(node, ast.Call):
                    name = (
                        node.func.id
                        if isinstance(node.func, ast.Name)
                        else node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else ""
                    )
                    if name and len(self.calls) < CALL_LIMIT:
                        self.calls.append((path, node.lineno, name))
                    elif name:
                        self.index_truncated = True
                self._index(node, path, prefix)

    def _found(self, items: list[Json]) -> Json:
        selected = items[:RESULT_LIMIT]
        self.discovered.update(i["target_id"] for i in selected)
        return {
            "status": "found",
            "results": selected,
            "truncated": len(items) > len(selected),
            "support_scope": RELATION_SCOPE,
        }

    def _resolve(self, identifier: str) -> Json:
        if identifier not in self.discovered:
            raise ValueError("Target must first be discovered in this investigation")
        return self.targets[identifier]

    def _query(self, query: str, symbols_only: bool) -> Json:
        terms = words(query)
        if not terms:
            return self._found([])
        ranked: list[tuple[int, str, int, Json]] = []
        if symbols_only:
            for item in self.symbols:
                name = item["symbol"].lower()
                score = sum(4 * (t in name) + (t in item["path"].lower()) for t in terms)
                if score:
                    ranked.append((-score, item["path"], item["start_line"], item))
        else:
            for path, content in self.files.items():
                if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
                    continue
                lines = content.splitlines()
                for index, line in enumerate(lines, 1):
                    score = sum(t in line.lower() for t in terms)
                    if score:
                        first = max(1, index - 8)
                        last = min(len(lines), first + LINE_LIMIT - 1)
                        target = self._target(path, first, last, kind="text_hit")
                        ranked.append((-score, path, index, target))
                        if len(ranked) >= SEARCH_LIMIT:
                            break
                if len(ranked) >= SEARCH_LIMIT:
                    break
        ranked.sort(key=lambda row: (row[0], row[1], row[2]))
        unique: dict[str, Json] = {}
        for row in ranked:
            unique.setdefault(row[3]["target_id"], row[3])
        result = self._found(list(unique.values()))
        result["truncated"] |= not symbols_only and len(ranked) >= SEARCH_LIMIT
        return result

    def _references(self, item: Json) -> Json:
        name = item["symbol"].split(".")[-1]
        path, first, last = item["path"], item["start_line"], item["end_line"]
        invoked = {n for p, line, n in self.calls if p == path and first <= line <= last}
        positions: dict[str, list[int]] = {}
        for p, line, n in self.calls:
            if n == name:
                positions.setdefault(p, []).append(line)
        for lines in positions.values():
            lines.sort()
        found: dict[str, Json] = {}
        for candidate in self.symbols:
            if candidate["target_id"] == item["target_id"]:
                continue
            locations = positions.get(candidate["path"], [])
            index = bisect_left(locations, candidate["start_line"])
            incoming = (
                bool(name) and index < len(locations) and locations[index] <= candidate["end_line"]
            )
            outgoing = candidate["symbol"].split(".")[-1] in invoked
            if incoming or outgoing:
                found[candidate["target_id"]] = {
                    **candidate,
                    "relation": "caller_candidate" if incoming else "callee_candidate",
                }
        return self._found(
            sorted(
                found.values(),
                key=lambda r: (
                    r["is_test"],
                    r["relation"] == "caller_candidate",
                    r["path"],
                    r["start_line"],
                ),
            )
        )

    def _tests(self, item: Json) -> Json:
        terms = words(item["symbol"].split(".")[-1] + " " + PurePosixPath(item["path"]).stem)
        found = []
        for path, content in self.files.items():
            if not is_test(path) or PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
                continue
            scores = [
                (sum(t in line.lower() for t in terms), i)
                for i, line in enumerate(content.splitlines(), 1)
            ]
            best = min(scores, key=lambda v: (-v[0], v[1])) if scores else (0, 1)
            if not best[0]:
                continue
            containing = [
                s
                for s in self.symbols
                if s["path"] == path and s["start_line"] <= best[1] <= s["end_line"]
            ]
            target = (
                min(containing, key=lambda s: s["end_line"] - s["start_line"])
                if containing
                else self._target(
                    path,
                    max(1, best[1] - 8),
                    min(len(content.splitlines()), best[1] + 61),
                    kind="test_candidate",
                )
            )
            found.append((-best[0], path, target))
        found.sort(key=lambda r: (r[0], r[1]))
        return self._found([r[2] for r in found])

    def _read(self, args: SpanArgs) -> Json:
        item = self._resolve(args.target_id)
        first = args.start_line if args.start_line is not None else item["start_line"]
        last = (
            args.end_line
            if args.end_line is not None
            else min(item["end_line"], first + LINE_LIMIT - 1)
        )
        if (
            not item["start_line"] <= first <= last <= item["end_line"]
            or last - first >= LINE_LIMIT
        ):
            raise ValueError("Read must stay inside the discovered target and 70 line budget")
        if any(
            r["path"] == item["path"] and r["start_line"] <= first and last <= r["end_line"]
            for r in self.reads
        ):
            return {"status": "already_read", "target_id": args.target_id}
        if len(self.reads) >= READ_LIMIT:
            return {"status": "read_budget", "results": []}
        selected: list[str] = []
        for line in self.files[item["path"]].splitlines(keepends=True)[first - 1 : last]:
            if len("".join([*selected, line])) > CHAR_LIMIT:
                break
            selected.append(line)
        if not selected:
            return {"status": "line_too_large", "results": []}
        text = "".join(selected)
        source = {
            **item,
            "start_line": first,
            "end_line": first + len(selected) - 1,
            "content": text,
            "content_hash": digest(text),
            "code_read": True,
            "truncated": first + len(selected) - 1 < item["end_line"] or first > item["start_line"],
        }
        self.reads.append(source)
        return {"status": "read", "source": source}

    def tool(self, name: str, arguments: Json) -> Json:
        if self.stop_reason:
            return {"status": "stopped", "reason": self.stop_reason}
        if self.tool_count >= TOOL_LIMIT:
            self.stop("tool_budget")
            return {"status": "tool_budget"}
        self.tool_count += 1
        try:
            if name == "list_tree":
                prefix = TreeArgs.model_validate(arguments).prefix
                paths = [p for p in self.files if p.startswith(prefix)]
                listed = [s for s in self.symbols if s["path"].startswith(prefix)]
                value: Json = {
                    **self._found(listed),
                    "status": "listed",
                    "paths": paths[:80],
                    "truncated": len(paths) > 80,
                    "python_symbols": len(self.symbols),
                    "code_read": False,
                }
            elif name in {"find_symbol", "search_text"}:
                value = self._query(
                    QueryArgs.model_validate(arguments).query, name == "find_symbol"
                )
            elif name in {"references_of", "find_tests"}:
                target = self._resolve(TargetArgs.model_validate(arguments).target_id)
                value = self._references(target) if name == "references_of" else self._tests(target)
            elif name == "read_span":
                value = self._read(SpanArgs.model_validate(arguments))
            elif name == "finish_investigation":
                finish = FinishArgs.model_validate(arguments)
                self.stop(finish.reason, finish.explanation)
                value = {"status": "stopped", "reason": self.stop_reason}
            else:
                raise ValueError("Unknown read-only tool")
        except (ValidationError, ValueError) as error:
            value = {"status": "invalid_arguments", "message": str(error)[:250]}
        self.trace.append(
            {
                "tool": name,
                "arguments": arguments,
                "status": value["status"],
                "result_ids": [i["target_id"] for i in value.get("results", [])],
                "paths": value.get("paths", [])[:20],
                "read": {
                    k: value["source"][k]
                    for k in ("path", "start_line", "end_line", "content_hash")
                }
                if value.get("source")
                else None,
            }
        )
        self.observations.append(
            {
                "tool": name,
                "status": value["status"],
                "results": value.get("results", []),
                "paths": value.get("paths", [])[:20],
            }
        )
        return value

    def bootstrap(self) -> None:
        self.tool("list_tree", {})
        query = " ".join(k for r in self.requirements for k in r["keywords"])[:200]
        if query.strip():
            self.tool("find_symbol", {"query": query})
        seeds = []
        for window in self.baseline["code"][:2]:
            containing = [
                s
                for s in self.symbols
                if s["path"] == window["path"]
                and s["start_line"] <= window["start_line"] <= s["end_line"]
            ]
            target = (
                min(containing, key=lambda s: s["end_line"] - s["start_line"])
                if containing
                else self._target(
                    window["path"], window["start_line"], window["end_line"], kind="body_anchor"
                )
            )
            self.discovered.add(target["target_id"])
            seeds.append(target)
            self.tool(
                "read_span",
                {
                    "target_id": target["target_id"],
                    "start_line": window["start_line"],
                    "end_line": window["end_line"],
                },
            )
        if seeds:
            related = self.tool("references_of", {"target_id": seeds[0]["target_id"]})
            tests = self.tool("find_tests", {"target_id": seeds[0]["target_id"]})
            for result in (related, tests):
                for target in result.get("results", []):
                    if not any(
                        r["path"] == target["path"]
                        and r["start_line"] <= target["start_line"] <= r["end_line"]
                        for r in self.reads
                    ):
                        self.tool("read_span", {"target_id": target["target_id"]})
                        break

    def stop(self, reason: str, explanation: str = "") -> None:
        if not self.stop_reason:
            self.stop_reason, self.stop_explanation = reason, explanation

    def context(self) -> Json:
        return {
            "mechanisms": [
                {k: r[k] for k in ("key", "technology", "keywords")} for r in self.requirements
            ],
            "repo_map": [self.targets[i] for i in sorted(self.discovered)][:24],
            "reading": {"code": self.reads, "documents": self.baseline["documents"]},
            "trace": self.trace[-3:],
            "observations": self.observations[-3:],
            "tree": {"paths": list(self.files)[:40], "truncated": len(self.files) > 40},
            "remaining_tools": TOOL_LIMIT - self.tool_count,
            "remaining_reads": READ_LIMIT - len(self.reads),
            "archive_hash": self.archive_hash,
        }

    def result(self) -> Json:
        return {
            "code": self.reads,
            "documents": self.baseline["documents"],
            "repo_map": [self.targets[i] for i in sorted(self.discovered)][:24],
            "coverage": "至多6次源码读取，每次70行/2600字符；Python 静态候选不证明调用或运行行为。",
            "investigation": {
                "version": VERSION,
                "archive_hash": self.archive_hash,
                "stop_reason": self.stop_reason,
                "stop_explanation": self.stop_explanation,
                "stop_assessment": "model_proposed"
                if self.stop_reason in {"evidence_sufficient", "unsupported"}
                else "system_limit",
                "tool_count": self.tool_count,
                "decision_calls": self.decisions,
                "read_count": len(self.reads),
                "test_reads": sum(r["is_test"] for r in self.reads),
                "budgets": {
                    "tools": TOOL_LIMIT,
                    "reads": READ_LIMIT,
                    "lines_per_read": LINE_LIMIT,
                    "chars_per_read": CHAR_LIMIT,
                },
                "reads": [
                    {
                        k: r[k]
                        for k in (
                            "path",
                            "symbol",
                            "start_line",
                            "end_line",
                            "content_hash",
                            "file_hash",
                            "is_test",
                            "truncated",
                        )
                    }
                    for r in self.reads
                ],
                "trace": self.trace,
                "parse_failures": self.parse_failures[:20],
                "index_truncated": self.index_truncated,
                "support_scope": RELATION_SCOPE,
                "executed": False,
            },
        }
