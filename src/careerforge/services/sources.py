from __future__ import annotations

import ast
import importlib
import re
from bisect import bisect_right
from importlib.metadata import PackageNotFoundError, version
from pathlib import PurePosixPath
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import ImportSourceInput
from ..db import Database
from ..errors import DomainError
from ..models import CodeSymbol, Evidence, Namespace, ObjectRow, SourceRead, SourceSet
from ..util import digest, fingerprint, manifest, safe_path, tokens, validate_files
from .dependencies import depend, register, require_object
from .privacy import assert_personal_access
from .security import accessible_namespaces, namespace_access

LICENSE_ALLOWLIST = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0-1.0", "CC-BY-4.0"}
READ_MAX_LINES = 70
READ_MAX_CHARACTERS = 2600
SOURCE_READ_CONTRACT = "whole-line-pages-1.6.3"
QUOTE_MAX_CANDIDATES = 256
QUOTE_MAX_FEEDBACK_BYTES = 64_000
QUOTE_LINE_CONTRACT = "lf-or-crlf-evidence-lines-1.6.4"
QUOTE_OFFSET_CONTRACT = (
    "zero-based Unicode code points; half-open offsets relative to read content; "
    "one-based lines/columns following splitlines(keepends=True) source windows"
)


def quote_occurrences(text: str, quote: str) -> list[int]:
    """Exact Unicode offsets, including overlaps; never select a preferred match.

    The old str.count check skipped overlapping occurrences ("aaaa" in "aaaaa").
    Revalidation intentionally rejects such historical ambiguous evidence rather
    than retaining an unsafe acceptance rule or rewriting any historical row.
    """
    if not quote:
        return []
    found, start = [], 0
    while (position := text.find(quote, start)) >= 0:
        found.append(position)
        start = position + 1
    return found


def quote_line_contract_supported(text: str) -> bool:
    """New quote preflight cannot publish the legacy LF counter's wrong lines.

    CR-only and the other separators recognized by splitlines require a future
    versioned persistent position witness. Preserve legacy rows and LF trailing
    newline semantics instead of silently reinterpreting existing evidence.
    """
    return re.search(r"\r(?!\n)|[\v\f\x1c-\x1e\x85\u2028\u2029]", text) is None


class SourceReadError(DomainError):
    """A correctable read request with bounded, non-evidentiary range information."""

    def __init__(self, code: str, message: str, status: int = 422, **details: Any):
        super().__init__(code, message, status)
        self.details = details


class FileInventoryItem(TypedDict):
    path: str
    language: str
    parser: str
    parse_status: str
    symbols: int
    lines: int
    file_hash: str


class SourceService:
    def __init__(self, db: Database):
        self.db = db

    def import_source(
        self, actor: str, value: ImportSourceInput, run: tuple[str, int, str] | None = None
    ) -> str:
        with self.db.transaction() as db:
            return self.import_source_in(db, actor, value, run)

    def import_source_in(
        self,
        db: Session,
        actor: str,
        value: ImportSourceInput,
        run: tuple[str, int, str] | None = None,
    ) -> str:
        """Import a fixed, non-executed snapshot in the caller's authority transaction."""
        validate_files(value.files)
        if run:
            from .runtime import leased

            active = leased(db, *run)
            if active.principal_id != actor or active.namespace_id != value.namespace_id:
                raise DomainError("SOURCE_AUTHORITY", "来源导入与执行身份或知识域不一致。", 403)
        ns = namespace_access(db, actor, value.namespace_id, True)
        if value.license_id not in LICENSE_ALLOWLIST:
            if ns.kind == "shared_public" or not value.rights_confirmed:
                raise DomainError(
                    "LICENSE_REVIEW", "来源许可未通过用途策略；不得自动共享或发送模型。", 403
                )
        identity = {
            "origin": value.origin_kind,
            "repository": value.repository_key,
            "commit": value.commit_sha,
            "files": manifest(value.files),
        }
        scope_hash = fingerprint(identity)
        old = db.scalar(
            select(SourceSet)
            .join(ObjectRow, ObjectRow.id == SourceSet.id)
            .where(
                ObjectRow.namespace_id == ns.id,
                SourceSet.scope_hash == scope_hash,
                SourceSet.repository_key == value.repository_key,
            )
        )
        if old:
            require_object(db, actor, old.id)
            return old.id
        obj = register(
            db,
            actor,
            ns.id,
            "source_set",
            {
                **identity,
                "license_id": value.license_id,
                "rights_confirmed": value.rights_confirmed,
                "synthetic": bool(value.source_metadata.get("synthetic")),
            },
        )
        source = SourceSet(
            id=obj.id,
            origin_kind=value.origin_kind,
            repository_key=value.repository_key,
            commit_sha=value.commit_sha,
            scope_hash=scope_hash,
            license_id=value.license_id,
            files=dict(value.files),
            metadata_json={
                **value.source_metadata,
                "file_manifest_hash": manifest(value.files),
                "rights_confirmed": value.rights_confirmed,
            },
        )
        db.add(source)
        db.flush()
        repo_map = self._index(db, source)
        source.metadata_json = {**source.metadata_json, "repo_map": repo_map}
        ns.revision += 1
        return source.id

    @staticmethod
    def get(db: Session, actor: str, source_id: str) -> SourceSet:
        obj = require_object(db, actor, source_id, "source_set")
        row = db.get(SourceSet, source_id)
        if row is None or row.metadata_json.get("file_manifest_hash") != manifest(row.files):
            raise DomainError("SOURCE_HASH", "来源文件哈希不一致。")
        identity = {
            "origin": row.origin_kind,
            "repository": row.repository_key,
            "commit": row.commit_sha,
            "files": manifest(row.files),
        }
        if (
            any(obj.payload.get(key) != value for key, value in identity.items())
            or row.scope_hash != fingerprint(identity)
            or ("license_id" in obj.payload and obj.payload["license_id"] != row.license_id)
            or (
                "rights_confirmed" in obj.payload
                and obj.payload["rights_confirmed"] != row.metadata_json.get("rights_confirmed")
            )
            or (
                "synthetic" in obj.payload
                and obj.payload["synthetic"] != bool(row.metadata_json.get("synthetic"))
            )
        ):
            raise DomainError("SOURCE_HASH", "来源身份或用途授权与固定记录不一致。")
        return row

    @staticmethod
    def validated_read(db: Session, actor: str, read_id: str) -> tuple[SourceRead, SourceSet]:
        """Read records are projections too: validate against the actual frozen source span."""
        read = db.get(SourceRead, read_id)
        if read is None:
            raise DomainError("EVIDENCE_HASH", "原文读取记录不存在。")
        source = SourceService.get(db, actor, read.source_id)
        lines = source.files.get(read.path, "").splitlines(keepends=True)
        if (
            read.path not in source.files
            or not 1 <= read.first_line <= read.last_line <= len(lines)
            or read.text != "".join(lines[read.first_line - 1 : read.last_line])
            or read.file_hash != digest(source.files[read.path])
            or read.quote_hash != digest(read.text)
        ):
            raise DomainError("EVIDENCE_HASH", "原文读取记录与固定来源不一致。")
        return read, source

    def _index(self, db: Session, source: SourceSet) -> dict[str, Any]:
        repo_map, symbols = self.static_index(source)
        for symbol in symbols:
            db.add(CodeSymbol(**symbol))
        return repo_map

    @classmethod
    def static_index(cls, source: SourceSet) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Recompute observations from immutable files without recording a model read.

        The import path persists the same index as before. Read projections use
        this pure path rather than trusting mutable cached metadata/symbol rows.
        """
        inventory: list[FileInventoryItem] = []
        indexed: list[dict[str, Any]] = []
        for path, body in sorted(source.files.items()):
            suffix = PurePosixPath(path).suffix.lower()
            symbols: list[dict[str, Any]] = []
            parser, status = "text-window", "text_only"
            if suffix == ".py":
                parser = "python-ast"
                try:
                    tree = ast.parse(body)

                    def visit(node: ast.AST, prefix: str, output: list[dict[str, Any]]) -> None:
                        for child in ast.iter_child_nodes(node):
                            if isinstance(
                                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                            ):
                                name = prefix + child.name
                                start = min(
                                    [child.lineno, *[d.lineno for d in child.decorator_list]]
                                )
                                output.append(
                                    {
                                        "name": name,
                                        "start": start,
                                        "end": child.end_lineno or start,
                                        "kind": type(child).__name__,
                                    }
                                )
                                visit(child, name + ".", output)
                            else:
                                visit(child, prefix, output)

                    visit(tree, "", symbols)
                    status = "parsed"
                except (SyntaxError, ValueError, RecursionError):
                    status = "parse_failed"
            elif suffix in {".ts", ".tsx", ".js", ".jsx", ".go"}:
                symbols, parser, status = cls._tree_sitter(suffix, body)
            for item in symbols[:6000]:
                indexed.append(
                    {
                        "source_id": source.id,
                        "path": path,
                        "name": item["name"][:500],
                        "kind": item["kind"],
                        "start_line": item["start"],
                        "end_line": item["end"],
                        "parser": parser,
                    }
                )
            inventory.append(
                {
                    "path": path,
                    "language": suffix,
                    "parser": parser,
                    "parse_status": status,
                    "symbols": len(symbols),
                    "lines": len(body.splitlines()),
                    "file_hash": digest(body),
                }
            )
        return {
            "files": inventory,
            "file_count": len(inventory),
            "executed": False,
            "relation_scope": "static_candidates_not_runtime_graph",
            "entry_candidates": [
                x["path"]
                for x in inventory
                if PurePosixPath(x["path"]).name
                in {
                    "main.py",
                    "app.py",
                    "main.go",
                    "index.ts",
                    "package.json",
                    "pyproject.toml",
                    "README.md",
                }
            ],
        }, indexed

    @staticmethod
    def _tree_sitter(suffix: str, body: str) -> tuple[list[dict[str, Any]], str, str]:
        try:
            from tree_sitter import Language, Parser

            package = (
                "tree_sitter_go"
                if suffix == ".go"
                else "tree_sitter_typescript"
                if suffix in {".ts", ".tsx"}
                else "tree_sitter_javascript"
            )
            module = importlib.import_module(package)
            lang = (
                module.language_tsx()
                if suffix == ".tsx"
                else module.language_typescript()
                if suffix == ".ts"
                else module.language()
            )
            parser = Parser(Language(lang))
            data = body.encode("utf-8")
            root = parser.parse(data).root_node
            symbols = []
            wanted = {
                "function_declaration",
                "function_definition",
                "method_definition",
                "method_declaration",
                "class_declaration",
                "type_spec",
                "interface_declaration",
                "type_alias_declaration",
                "enum_declaration",
            }
            queue = [root]
            while queue:
                node = queue.pop()
                # JS/TS arrow functions are named by their variable declarator;
                # Go named types are nested type_spec nodes, not type_declaration.
                value = (
                    node.child_by_field_name("value")
                    if node.type == "variable_declarator"
                    else None
                )
                named_function = value is not None and value.type in {
                    "arrow_function",
                    "function_expression",
                    "generator_function",
                }
                if node.type in wanted or named_function:
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        symbols.append(
                            {
                                "name": data[name_node.start_byte : name_node.end_byte].decode(),
                                "kind": node.type,
                                "start": node.start_point.row + 1,
                                "end": node.end_point.row + 1,
                            }
                        )
                queue.extend(reversed(node.children))
            return (
                symbols,
                "tree-sitter:" + package + ":" + version(package.replace("_", "-")),
                "partial" if root.has_error else "parsed",
            )
        except (ImportError, PackageNotFoundError):
            return [], "tree-sitter-unavailable", "text_only"
        except (ValueError, TypeError):
            return [], "tree-sitter", "parse_failed"

    def describe(self, actor: str, source_id: str) -> dict[str, Any]:
        with self.db.read() as db:
            source = self.get(db, actor, source_id)
            return {
                "source_id": source.id,
                "origin_kind": source.origin_kind,
                "repository": source.repository_key,
                "commit": source.commit_sha,
                "scope_hash": source.scope_hash,
                **source.metadata_json["repo_map"],
            }

    def search(
        self,
        actor: str,
        source_id: str,
        query: str,
        symbols_only: bool = False,
        *,
        exact_symbol: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query or len(query) > 200:
            raise DomainError("QUERY", "检索词为空或过长。", 422)
        with self.db.read() as db:
            source = self.get(db, actor, source_id)
            symbols = db.scalars(select(CodeSymbol).where(CodeSymbol.source_id == source.id)).all()
            results = [
                {
                    "path": r.path,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "symbol": r.name,
                    "code_read": False,
                }
                for r in symbols
                if (
                    r.name == query or r.name.rsplit(".", 1)[-1] == query
                    if exact_symbol
                    else query.lower() in r.name.lower()
                )
            ][:12]
            if not symbols_only and not exact_symbol:
                terms = query.lower().split()[:12]
                for path, text in sorted(source.files.items()):
                    for i, line in enumerate(text.splitlines()):
                        if any(term in line.lower() for term in terms):
                            results.append(
                                {
                                    "path": path,
                                    "start_line": i + 1,
                                    "end_line": min(i + 25, len(text.splitlines())),
                                    "snippet": line[:240],
                                    "code_read": False,
                                }
                            )
                            if len(results) >= 24:
                                return results
            return results

    def search_snapshots(
        self, actor: str, query: str, *, public_only: bool = False
    ) -> dict[str, Any]:
        """Bounded local-first discovery. Hits remain unreviewed leads, never facts."""
        from .taxonomy import expand_query, resolve_topics

        if not 2 <= len(query) <= 180:
            raise DomainError("QUERY", "检索词长度无效。", 422)
        repositories = set(re.findall(r"(?:^|\s)repo:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", query))
        plain = re.sub(r"\b[A-Za-z_]+:\S+", " ", query)
        taxonomy_actor = None if public_only else actor
        variants = expand_query(
            self.db, plain, resolve_topics(self.db, plain, taxonomy_actor), taxonomy_actor
        )
        terms = tokens(" ".join(variants))[:32]
        candidates: list[dict[str, Any]] = []
        leads: list[dict[str, Any]] = []
        remaining_characters = 250_000
        with self.db.read() as db:
            namespaces = (
                db.scalars(select(Namespace).where(Namespace.kind == "shared_public")).all()
                if public_only
                else accessible_namespaces(db, actor)
            )
            ns_ids = [namespace_access(db, actor, ns.id).id for ns in namespaces]
            generations = (
                {}
                if public_only
                else {
                    owner: assert_personal_access(db, owner)
                    for owner in sorted(
                        {actor, *(ns.owner_id for ns in namespaces if ns.kind != "shared_public")}
                    )
                }
            )
            # Fetch IDs, not fifty potentially 12-MB JSON snapshots at once.
            query_scope = (
                select(SourceSet.id, SourceSet.repository_key)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(ObjectRow.namespace_id.in_(ns_ids), SourceSet.origin_kind == "repository")
            )
            if repositories:
                query_scope = query_scope.where(SourceSet.repository_key.in_(repositories))
            rows = db.execute(query_scope.order_by(SourceSet.id).limit(50)).all()
            for identity in rows:
                if remaining_characters <= 0:
                    break
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity.repository_key):
                    continue
                try:
                    source = self.get(db, actor, identity.id)
                except DomainError:
                    continue  # Unavailable/tampered projections cannot be local authority.
                matches = []
                for path, body in sorted(source.files.items()):
                    if remaining_characters <= 0:
                        break
                    window = body[:remaining_characters]
                    remaining_characters -= len(window)
                    for line_no, line in enumerate(window.splitlines(), 1):
                        if terms and any(
                            term in line.lower() or term in path.lower() for term in terms
                        ):
                            matches.append(
                                {
                                    "channel": "local_snapshot",
                                    "source_id": source.id,
                                    "repository": source.repository_key,
                                    "commit": source.commit_sha,
                                    "path": path,
                                    "line": line_no,
                                    "excerpt": line[:240],
                                    "source_status": "local_snapshot_lead_not_reviewed_evidence",
                                }
                            )
                            break
                    if len(matches) >= 3:
                        break
                if not matches:
                    continue
                candidates.append(
                    {
                        "source_id": source.id,
                        "repository_id": source.repository_key.lower(),
                        "name": source.repository_key,
                        "url": "https://github.com/" + source.repository_key,
                        "commit": source.commit_sha,
                        "verified_implementation": False,
                    }
                )
                leads.extend(matches)
                if len(candidates) >= 5:
                    break
        return {
            "query": query,
            "channel": "local_snapshot",
            "candidates": candidates,
            "leads": leads,
            "incomplete_results": True,
            "local_only": True,
            "privacy_generations": generations,
            "privacy_owner_ids": sorted(generations),
            "stop_reason": "bounded_local_snapshots_not_exhaustive",
            "scanned_characters": 250_000 - remaining_characters,
            "scan_character_cap": 250_000,
            "synthetic": False,
        }

    @staticmethod
    def _read_window(
        source: SourceSet, path: str, start: int, end: int, paginate: bool
    ) -> tuple[int, str]:
        if not isinstance(path, str):
            raise DomainError("TOOL_ARGS", "path 必须是字符串。", 422)
        safe_path(path)
        if path not in source.files:
            raise DomainError("NOT_FOUND", "所选快照中无该文件。", 404)
        lines = source.files[path].splitlines(keepends=True)
        if type(start) is not int or type(end) is not int:
            raise DomainError("TOOL_ARGS", "start_line 和 end_line 必须是整数。", 422)
        if start < 1 or end < start or end > len(lines):
            raise SourceReadError(
                "READ_RANGE",
                "读取范围必须是文件内从1开始的闭区间。",
                file_lines=len(lines),
                max_page_lines=READ_MAX_LINES,
                max_page_characters=READ_MAX_CHARACTERS,
            )
        if paginate:
            page_end, characters = start - 1, 0
            for index in range(start - 1, min(end, start + READ_MAX_LINES - 1)):
                if characters + len(lines[index]) > READ_MAX_CHARACTERS:
                    break
                characters += len(lines[index])
                page_end = index + 1
            if page_end < start:
                raise SourceReadError(
                    "READ_LINE_LIMIT",
                    "首行超过2600字符；整行证据合同无法读取此行，未创建读取记录。",
                    413,
                    file_lines=len(lines),
                    line=start,
                    line_characters=len(lines[start - 1]),
                    max_page_characters=READ_MAX_CHARACTERS,
                    splittable=False,
                )
            end = page_end
        elif end - start + 1 > READ_MAX_LINES:
            raise DomainError("READ_RANGE", "源码读取最多70行，且必须在范围内。", 422)
        quote = "".join(lines[start - 1 : end])
        if len(quote) > READ_MAX_CHARACTERS:
            raise DomainError("READ_LIMIT", "该范围超过2600字符，请缩小范围。", 413)
        return end, quote

    @staticmethod
    def _matching_read(db, actor, source_id, path, start, end, role, session_key, run_id):
        return db.scalar(
            select(SourceRead).where(
                SourceRead.source_id == source_id,
                SourceRead.principal_id == actor,
                SourceRead.run_id == run_id,
                SourceRead.session_key == session_key,
                SourceRead.role == role,
                SourceRead.path == path,
                SourceRead.first_line == start,
                SourceRead.last_line == end,
            )
        )

    @staticmethod
    def _read_response(
        row: SourceRead, source: SourceSet, requested_end: int, paginate: bool
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "read_id": row.id,
            "source_id": source.id,
            "path": row.path,
            "start_line": row.first_line,
            "end_line": row.last_line,
            "content": row.text,
            "file_hash": row.file_hash,
            "quote_hash": row.quote_hash,
            "commit": source.commit_sha,
        }
        if paginate:
            result.update(
                read_contract=SOURCE_READ_CONTRACT,
                requested_end_line=requested_end,
                file_lines=len(source.files[row.path].splitlines()),
                truncated=row.last_line < requested_end,
                continuation=(
                    {"path": row.path, "start_line": row.last_line + 1, "end_line": requested_end}
                    if row.last_line < requested_end
                    else None
                ),
            )
        return result

    def recover_read_span(
        self,
        actor: str,
        source_id: str,
        path: str,
        start: int,
        end: int,
        role: str,
        session_key: str,
        run_id: str | None = None,
        *,
        paginate: bool = False,
    ) -> dict[str, Any] | None:
        """Inspect a possibly committed read without creating or repeating one."""
        with self.db.read() as db:
            source = self.get(db, actor, source_id)
            actual_end, _ = self._read_window(source, path, start, end, paginate)
            row = self._matching_read(
                db, actor, source_id, path, start, actual_end, role, session_key, run_id
            )
            if row is None:
                return None
            self.validated_read(db, actor, row.id)
            return self._read_response(row, source, end, paginate)

    def read_span(
        self,
        actor: str,
        source_id: str,
        path: str,
        start: int,
        end: int,
        role: str,
        session_key: str,
        run_id: str | None = None,
        *,
        paginate: bool = False,
    ) -> dict[str, Any]:
        """Read exact frozen text, optionally returning a bounded whole-line page.

        Legacy calls retain the all-or-error range contract. Opt-in pages never
        splice a line or add an ellipsis to evidence. Their continuation names
        the unread remainder of the originally requested range. A single line
        larger than the character cap cannot be represented by the current
        line-addressed receipt and is reported explicitly, without a receipt.
        """
        with self.db.transaction() as db:
            source = self.get(db, actor, source_id)
            requested_end = end
            end, quote = self._read_window(source, path, start, end, paginate)
            row = self._matching_read(
                db, actor, source_id, path, start, end, role, session_key, run_id
            )
            if row is None:
                row = SourceRead(
                    source_id=source.id,
                    principal_id=actor,
                    run_id=run_id,
                    role=role,
                    session_key=session_key,
                    path=path,
                    first_line=start,
                    last_line=end,
                    text=quote,
                    file_hash=digest(source.files[path]),
                    quote_hash=digest(quote),
                )
                db.add(row)
                db.flush()
            else:
                if row.quote_hash != digest(quote):
                    raise DomainError("SOURCE_REPLAY_CHANGED", "读取观察不能静默改写。")
                self.validated_read(db, actor, row.id)
            return self._read_response(row, source, requested_end, paginate)

    @staticmethod
    def inspect_quote_from_read(
        db: Session,
        actor: str,
        source_id: str,
        read_id: str,
        quote: str,
        role: str,
        session_key: str,
        run_id: str,
        *,
        report_missing: bool = False,
    ) -> dict[str, Any]:
        """No-write diagnostics over this role's existing authorized receipt only.

        Authorization/integrity failures and absent quotes raise generic errors:
        they are never converted into model feedback (which could leak invented
        text, another role's reads, or private task data). Every ambiguity exposes
        all exact candidates, not evidence IDs or a silently chosen location.
        """
        read = db.get(SourceRead, read_id)
        if (
            read is None
            or read.principal_id != actor
            or read.source_id != source_id
            or read.role != role
            or read.session_key != session_key
            or read.run_id != run_id
        ):
            raise DomainError("UNREAD_EVIDENCE", "引用不是本次角色实际读取的原文。", 422)
        read, source = SourceService.validated_read(db, actor, read_id)
        if not isinstance(quote, str) or not 4 <= len(quote) <= READ_MAX_CHARACTERS:
            raise DomainError("QUOTE_REFERENCE", "引用长度不符合原文合同。", 422)
        positions = quote_occurrences(read.text, quote)
        if not positions and not report_missing:
            raise DomainError("QUOTE_NOT_FOUND", "引用不在本次已读原文中。", 422)
        if len(positions) == 1:
            return {"match_count": 1}
        if len(positions) > QUOTE_MAX_CANDIDATES:
            raise DomainError(
                "QUOTE_FEEDBACK_LIMIT", "完整引用候选超过256个上限，未截断或自动选择。", 422
            )
        # Match the read-window line convention, including CR-only and Unicode
        # separators. Offsets remain exact code points, never an acceptance ID.
        line_offsets = [0]
        for line in read.text.splitlines(keepends=True):
            line_offsets.append(line_offsets[-1] + len(line))
        candidates = []
        for position in positions:
            last = position + len(quote) - 1
            first_index = bisect_right(line_offsets, position) - 1
            last_index = bisect_right(line_offsets, last) - 1
            candidates.append(
                {
                    "start_offset": position,
                    "end_offset": position + len(quote),
                    "start_line": read.first_line + first_index,
                    "end_line": read.first_line + last_index,
                    "start_column": position - line_offsets[first_index] + 1,
                    "end_column_exclusive": last - line_offsets[last_index] + 2,
                }
            )
        return {
            "match_count": len(candidates),
            "code": "AMBIGUOUS_QUOTE",
            "read_id": read.id,
            "quote": quote,
            "quote_hash": digest(quote),
            "candidates": candidates,
            "candidates_complete": True,
            "offset_contract": QUOTE_OFFSET_CONTRACT,
            "read": {
                "read_id": read.id,
                "source_id": source.id,
                "repository": source.repository_key,
                "path": read.path,
                "commit": source.commit_sha,
                "start_line": read.first_line,
                "end_line": read.last_line,
                "file_hash": read.file_hash,
                "read_hash": read.quote_hash,
                "content": read.text,
            },
        }

    @staticmethod
    def evidence_from_read(
        db: Session,
        actor: str,
        ns_id: str,
        source_id: str,
        read_id: str,
        quote: str,
        role: str,
        session_key: str,
        run_id: str | None = None,
    ) -> str:
        read = db.get(SourceRead, read_id)
        if (
            read is None
            or read.principal_id != actor
            or read.source_id != source_id
            or read.role != role
            or read.session_key != session_key
            or (run_id is not None and read.run_id != run_id)
        ):
            raise DomainError("UNREAD_EVIDENCE", "引用不是本次角色实际读取的原文。", 422)
        read, source = SourceService.validated_read(db, actor, read_id)
        positions = quote_occurrences(read.text, quote)
        if len(positions) != 1:
            raise DomainError("AMBIGUOUS_QUOTE", "引用须在已读范围内连续、精确且唯一。", 422)
        pos = positions[0]
        start = read.first_line + read.text[:pos].count("\n")
        end = start + quote.rstrip("\n").count("\n")
        obj = register(
            db,
            actor,
            ns_id,
            "evidence",
            {
                "source_id": source_id,
                "read_id": read_id,
                "path": read.path,
                "start_line": start,
                "end_line": end,
                "quote_hash": digest(quote),
            },
        )
        db.add(
            Evidence(
                id=obj.id,
                source_id=source_id,
                read_id=read_id,
                path=read.path,
                start_line=start,
                end_line=end,
                exact_quote=quote,
                file_hash=read.file_hash,
                quote_hash=digest(quote),
            )
        )
        depend(db, actor, obj.id, source_id, "grounded_in")
        return obj.id

    def data_classes(self, actor: str, source_id: str) -> list[str]:
        with self.db.read() as db:
            obj = require_object(db, actor, source_id, "source_set")
            ns = namespace_access(db, actor, obj.namespace_id)
            return ["public_source"] if ns.kind == "shared_public" else ["private_source"]
