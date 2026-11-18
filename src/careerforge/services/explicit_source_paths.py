"""Question-derived navigation constraints, never inferred from a source catalog.

This is intentionally a bounded grammar, not another intent classifier. A file
mention alone grants no scope. Unclear navigation targets require clarification;
exact targets use the existing canonical relative-path rules without repair,
basename substitution, URL decoding, case folding or filesystem access.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..errors import DomainError
from ..util import safe_path
from .sources import SourceService

SOURCE_PATH_CONTRACT = "explicit-source-path-1.6.5"
LOCATE_PATH_GUARD = "locate-path-guard-1.6.5.2"

_QUOTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|(?<!\w)\'[^\'\n]*\'(?!\w)|'
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|^\s*>[^\n]*",
    re.M,
)
_TOKEN = re.compile(r"(?<![A-Za-z0-9_.%~:@+?#=\\/-])[A-Za-z0-9_.%~:@+?#=\\/-]+")
_NAVIGATE = re.compile(
    r"\b(?:locate|find|show|open|read)\b|\bwhere\s+(?:is|are)\b|"
    r"定位|查找|打开|阅读|查看|在哪里|在哪",
    re.I,
)
_NEGATED = re.compile(
    r"\b(?:not|never|without|avoid|except|instead|neither|nor)\b|\b\w+n[’']t\b|"
    r"不|勿|别|避免|无需|无须|并非|从未|没有",
    re.I,
)
_CONTEXT = re.compile(
    r"\b(?:example|e\.g|mention|mentioned|context|comparison|compare|compared|versus|vs|keyword|word|term)\b|"
    r"\brather than\b|\binstead of\b|例如|比如|示例|提到|提及|上下文|比较|对比|相比|而不是|而非",
    re.I,
)
_FILE_INTRO = re.compile(r"(?:\b(?:file|path)\s*|文件(?:路径)?\s*|路径\s*)$", re.I)
_LOCATION_INTRO = re.compile(
    r"(?:\b(?:in|within|from|at)\s+(?:(?:the\s+)?(?:file|path)\s+)?|在\s*)$", re.I
)
_BOUNDARY = re.compile(r"[，,；;。!！？\n]|[.?](?=\s|$)")


def requested_source_path(question: str) -> dict[str, str] | None:
    """Recognize an explicit navigation object, not arbitrary filename mentions.

    Slash-qualified dotted filenames are unambiguous path syntax. Bare dotted
    identifiers also name symbols, so they need a file/path or location marker.
    Extensionless files need a file/path marker. Quoted sentences/code samples
    are data; a quote containing only a path or identifier remains a token.
    Multiple requested paths/alternatives and malformed targets fail closed.
    """
    chars = list(question)
    unsupported_quoted_target = False
    for match in _QUOTED.finditer(question):
        quoted = match.group(0)
        inner = quoted[1:-1]
        if (
            not quoted.startswith(("```", ">"))
            and _TOKEN.fullmatch(inner)
            and (any(c in inner for c in "/\\.") or _FILE_INTRO.search(question[: match.start()]))
        ):
            chars[match.start()] = chars[match.end() - 1] = " "
        else:
            prefix = question[: match.start()]
            if (
                not quoted.startswith(("```", ">"))
                and "/" in inner
                and not _NAVIGATE.search(inner)
                and (
                    _LOCATION_INTRO.search(prefix)
                    or _FILE_INTRO.search(prefix)
                    or re.search(
                        r"(?:\b(?:locate|find|show|read|open)(?:\s+(?:me|us))?|定位|查找|打开|查看)\s*$",
                        prefix,
                        re.I,
                    )
                )
            ):
                unsupported_quoted_target = True
            chars[match.start() : match.end()] = " " * len(quoted)
    plain = "".join(chars)
    if unsupported_quoted_target and _NAVIGATE.search(plain):
        return {"status": "ambiguous", "reason": "requested_source_path_ambiguous"}
    if (
        _NAVIGATE.search(plain)
        and "/" in plain
        and any(ord(c) < 32 and c not in "\n\r\t" for c in plain)
    ):
        return {"status": "invalid", "reason": "requested_source_path_invalid"}
    targets: list[str] = []
    unclear = False
    for segment in _BOUNDARY.split(plain):
        actions = list(_NAVIGATE.finditer(segment))
        if not actions:
            continue
        for token in _TOKEN.finditer(segment):
            value = token.group(0)
            before, after = segment[: token.start()], segment[token.end() :]
            file_marker = bool(_FILE_INTRO.search(before))
            location_marker = bool(_LOCATION_INTRO.search(before))
            has_separator = "/" in value or "\\" in value
            leaf = re.split(r"[/\\]", value)[-1]
            dotted_file = bool(re.fullmatch(r"[^.]+(?:\.[^.]+)+", leaf))
            if has_separator and not dotted_file and not file_marker:
                previous = [action for action in actions if action.end() <= token.start()]
                direct_bare = bool(
                    previous
                    and re.fullmatch(
                        r"\s*(?:(?:me|us)\s*)?", segment[previous[-1].end() : token.start()], re.I
                    )
                )
                if (location_marker or direct_bare) and not (
                    value.startswith(("/", "./", "../")) or "\\" in value or ":" in value
                ):
                    # A bare repository reference followed by 的 + symbol is
                    # existing repository navigation, not an inferred file.
                    # Other direct path fragments may contain unsupported
                    # spaces/punctuation; never drop their constraint.
                    if not re.match(r"\s*的\s*[A-Za-z_]", after):
                        unclear = True
                    continue
            if not (
                (
                    has_separator
                    and (
                        dotted_file
                        or value.startswith(("/", "./", "../"))
                        or ":" in value
                        or "\\" in value
                        or "/.." in value
                    )
                )
                or "%" in value
                or file_marker
                or (location_marker and dotted_file)
            ):
                continue
            preceding_actions = [action for action in actions if action.end() <= token.start()]
            following_actions = [action for action in actions if action.start() >= token.end()]
            if preceding_actions:
                action = preceding_actions[-1]
                between = segment[action.end() : token.start()]
                # The path must be the object or an explicit location, not a
                # stray filename later in prose about the requested operation.
                direct = bool(re.fullmatch(r"\s*(?:(?:me|us)\s*)?", between, re.I)) or file_marker
                if _CONTEXT.search(before):
                    continue
                if not (direct or location_marker):
                    # A navigation clause with path-like data that this narrow
                    # grammar cannot attach is not an unrestricted lookup.
                    unclear = True
                    continue
                if _NEGATED.search(before):
                    unclear = True
                    continue
            elif following_actions:
                # "In src/a.py locate X" / "src/a.py 中的 X 在哪里".
                if not (location_marker or re.match(r"\s*(?:中|内|里的|中的)", after)):
                    continue
                if _CONTEXT.search(segment) or _NEGATED.search(segment):
                    unclear = True
                    continue
            else:
                continue
            if value.endswith(".") or (after and not after[0].isspace() and ord(after[0]) < 128):
                # Unsupported attached syntax is not sentence punctuation we
                # can safely discard. In particular, never trim ellipses.
                unclear = True
                continue
            try:
                if any(char in value for char in "%?#"):
                    raise DomainError("UNSAFE_PATH", "文件路径无效。", 422)
                safe_path(value)
            except DomainError:
                return {"status": "invalid", "reason": "requested_source_path_invalid"}
            targets.append(value)
            # A second alternative is not permission to pick whichever exists.
            if re.match(r"\s*(?:\b(?:or|and)\b\s+|(?:或|和|与|以及)\s*)", after, re.I):
                tail = after.lstrip()
                if any(
                    "/" in item.group(0) or "\\" in item.group(0) for item in _TOKEN.finditer(tail)
                ):
                    unclear = True
    unique = list(dict.fromkeys(targets))
    if len(unique) == 1:
        # Commas separate prose clauses, but an immediate second path is an
        # explicit list, not permission to silently choose its first member.
        for occurrence in re.finditer(re.escape(unique[0]), plain):
            tail = plain[occurrence.end() :]
            additional = re.match(
                r"\s*[,，、;；\n]\s*(?:(?:and|or)\s+|(?:或|和|与|以及))?"
                r"([A-Za-z0-9_.%~:@+?#=\\/-]+)",
                tail,
                re.I,
            )
            if additional and any(c in additional.group(1) for c in "/\\."):
                unclear = True
    if unclear or len(unique) > 1:
        return {"status": "ambiguous", "reason": "requested_source_path_ambiguous"}
    return {"status": "exact", "path": unique[0]} if unique else None


def navigation_path_request(
    question: str,
    *,
    contract: str | None,
    guard: str | None,
    locate: bool,
) -> dict[str, str] | None:
    """Keep unsupported path syntax from becoming an unrestricted LOCATE.

    The old positive grammar is unchanged for immutable pre-guard turns. The
    new guard never infers an exact target or authorizes a source: a path-like
    candidate the grammar cannot attach requires clarification. Routing only
    enables this conservative check; the model supplies neither path nor scope.
    """
    if contract is None:
        return None
    if contract != SOURCE_PATH_CONTRACT or guard not in {None, LOCATE_PATH_GUARD}:
        raise DomainError("SOURCE_PATH_CONTRACT", "未知的源码路径合同版本。", 422)
    request = requested_source_path(question)
    if request is not None or guard is None or not locate:
        return request

    # Whole quoted sentences, fenced code and quoted action words remain data.
    # A quote consisting only of a path token can still be a literal object.
    chars = list(question)
    for match in _QUOTED.finditer(question):
        quoted = match.group(0)
        inner = quoted[1:-1]
        literal_path = not quoted.startswith(("```", ">")) and (
            (bool(_TOKEN.fullmatch(inner)) and any(c in inner for c in "/\\."))
            or bool(re.fullmatch(r"(?:[^\s/\\]+[/\\])+[^/\\\n;!?。！？；]+\.[A-Za-z0-9_-]+", inner))
        )
        if literal_path:
            chars[match.start()] = chars[match.end() - 1] = " "
        else:
            chars[match.start() : match.end()] = " " * len(quoted)
    plain = "".join(chars)
    for segment in _BOUNDARY.split(plain):
        for token in _TOKEN.finditer(segment):
            before, value = (
                segment[: token.start()],
                token.group(0),
            )
            if _CONTEXT.search(before):
                continue
            separator = "/" in value or "\\" in value
            # An explicit repository label is not a file target. Never infer
            # this distinction from a verb, slash shape or available files.
            repository_context = bool(
                re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
                and re.search(
                    r"(?:\b(?:repository|repo|project)\s+|(?:仓库|项目)\s*)$", before, re.I
                )
            )
            if repository_context:
                continue
            candidate = (
                separator
                or bool(_FILE_INTRO.search(before))
                or (bool(_LOCATION_INTRO.search(before)) and "." in value)
                or "%" in value
            )
            if candidate:
                return {
                    "status": "ambiguous",
                    "reason": "requested_source_path_attachment_unconfirmed",
                }
            if (
                "." in value
                and value.strip(".")
                and not re.search(
                    r"(?:\b(?:symbol|function|method)\s+|(?:符号|函数|方法)\s*)$", before, re.I
                )
            ):
                # A bare dotted token may be a file OR a qualified symbol.
                # No extension/case list or source availability guesses its
                # type; unsupported attachment can only ask for clarification.
                return {"status": "ambiguous", "reason": "requested_source_target_ambiguous"}
    return None


def resolve_source_path(
    db: Session,
    actor: str,
    source_ids: list[str],
    request: dict[str, str] | None,
) -> dict[str, str] | None:
    """Resolve only a caller-selected, authorized, hash-checked snapshot.

    No source catalog, excluded-file metadata, nearby paths, counts or private
    existence information participates in this response. Absence here is only
    absence from the selected imported snapshot, never from the repository.
    """
    if request is None:
        return None
    if request["status"] != "exact":
        return {"status": "clarification_required", "reason": request["reason"]}
    if len(set(source_ids)) != 1:
        return {"status": "clarification_required", "reason": "requested_source_scope_required"}
    source = SourceService.get(db, actor, source_ids[0])
    if request["path"] not in source.files:
        return {
            "status": "missing_from_selected_snapshot",
            "reason": "requested_source_path_missing_from_snapshot",
            "path": request["path"],
        }
    return {"status": "available", "path": request["path"]}


def source_path_gap(resolution: dict[str, str], *, guard: str | None = None) -> dict[str, Any]:
    """A non-model, non-success response with no unauthorized source metadata."""
    missing = resolution["status"] == "missing_from_selected_snapshot"
    return {
        "kind": "evidence_gap",
        "text": (
            "指定路径不在当前所选且有权访问的已导入快照中，无法完成本次源码定位。"
            "这不表示该文件在整个仓库中不存在；请确认路径或提供已授权的相应快照。"
            if missing
            else "尚不能确定本次定位中的标识是文件路径还是符号。请明确目标类型、路径或符号名称以及已授权的固定快照；未进行替代文件定位。"
            if resolution.get("reason") == "requested_source_target_ambiguous"
            else "尚不能确定本次源码定位的明确路径和快照范围。请确认一个源码相对路径及已授权的固定快照；未进行替代文件定位。"
        ),
        "details": [resolution["reason"]],
        "gap_category": "evidence" if missing else "scope",
        "checked": "server_template",
        "publication_ids": [],
        "fact_atom_ids": [],
        "citations": [],
        "source_path_contract": SOURCE_PATH_CONTRACT,
        **({"source_path_guard": guard} if guard else {}),
        "source_path_resolution": resolution,
        "task_satisfied": False,
    }
