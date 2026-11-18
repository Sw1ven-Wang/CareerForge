"""Opt-in 1.6.6 inputs and a real, display-before-receipt source viewer.

This client never opens a path or URL supplied by the server. Source windows come
from the owned navigation endpoint, not a working tree or a model-generated quote.
A receipt reports completed client output, never that a human saw or understood it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from ..contracts_assessment_v166 import AssessmentInput
from ..contracts_jd_v166 import JDExtractionInput, JDImportInput, JDSource
from ..contracts_learning_events_v166 import NavigationAckInput
from .learning_ext import load_contract

_MAX_JD_BYTES = 200_000  # The contract permits at most 50,000 Unicode code points.
_MAX_DISPLAY_BYTES = 1_000_000
_TARGET_FIELDS = ("source_id", "commit", "path", "start_line", "end_line")


def _identifier(value: str, *, limit: int = 180) -> str:
    # IDs occupy one URL segment. Do not interpret a user argument as a URL,
    # query string, path traversal or shell command.
    if len(value) > limit or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise typer.BadParameter("需要服务器返回的单个对象ID；未发送请求。")
    return value


def _literal_jd(file: Path) -> bytes:
    try:
        if not file.is_file():
            raise OSError
        with file.open("rb") as handle:
            body = handle.read(_MAX_JD_BYTES + 1)
        if not body or len(body) > _MAX_JD_BYTES:
            raise ValueError
        # No read_text/universal-newline conversion, trim, or normalization.
        body.decode("utf-8")
        return body
    except (OSError, UnicodeError, ValueError):
        raise typer.BadParameter("需要非空、UTF-8编码且不超过200KB的JD文件；未发送请求。") from None


def _display_target(
    response: Any, block_id: str, citation_index: int, ack_key: str | None
) -> tuple[NavigationAckInput, list[str]]:
    """Validate the envelope before emitting any selected source or receipt."""
    try:
        if (
            not isinstance(response, dict)
            or response.get("server_location_valid") is not True
            or response.get("answer_block_id") != block_id
            or type(response.get("citation_index")) is not int
            or response["citation_index"] != citation_index
            or any(type(response.get(key)) is not int for key in ("start_line", "end_line"))
        ):
            raise ValueError
        target = NavigationAckInput(
            answer_block_id=block_id,
            citation_index=citation_index,
            **{key: response[key] for key in _TARGET_FIELDS},
            client_event="source_highlight_ack",
            idempotency_key=ack_key or "display-only-not-sent",
        )
        window = response["display"]
        if (
            not isinstance(window, dict)
            or window.get("content_complete") is not True
            or type(window.get("start_line")) is not int
            or type(window.get("end_line")) is not int
            or window["start_line"] != target.start_line
            or window["end_line"] != target.end_line
            or not isinstance(window.get("text"), str)
            or not isinstance(window.get("file_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", window["file_sha256"])
        ):
            raise ValueError
        raw = window["text"].encode("utf-8")
        if (
            not raw
            or len(raw) > _MAX_DISPLAY_BYTES
            or hashlib.sha256(raw).hexdigest() != window.get("text_sha256")
        ):
            raise ValueError
        lines = window["text"].splitlines(keepends=True)
        if len(lines) != target.end_line - target.start_line + 1:
            raise ValueError
        return target, lines
    except (KeyError, TypeError, ValueError, UnicodeError, ValidationError):
        # Never echo private source or schema input in an error diagnostic.
        raise typer.BadParameter(
            "源码窗口不完整或与已验证引用不匹配；未显示源码、未发送ACK。"
        ) from None


def render_source(
    console: Console,
    clean: Callable[[str], str],
    target: NavigationAckInput,
    lines: list[str],
) -> None:
    """Write the complete selected range, then flush before the caller can ACK."""
    console.print(
        clean(f"{target.path}:{target.start_line}-{target.end_line} @ {target.commit}"),
        markup=False,
        highlight=False,
    )
    console.print("已验证固定快照；以下 > 标记完整选中行（终端控制字符已过滤）。")
    width = len(str(target.end_line))
    for number, line in enumerate(lines, target.start_line):
        # Text is literal: source cannot create Rich markup, ANSI, OSC links,
        # clipboard writes or bidirectional terminal spoofing. Number each
        # original source line even when safety filtering removes characters.
        row = Text(f"> {number:>{width}} | ", style="bold cyan")
        row.append(clean(line).rstrip("\n"), style="")
        console.print(row, highlight=False, soft_wrap=True)
    console.print("显示回执只代表客户端已输出，不代表真人阅读、运行成功或掌握。")
    # A returned HTTP response or successful print call alone is not a receipt.
    # Failed/interrupting writes and failed flushes leave the server unacknowledged.
    console.file.flush()


def register_cli(
    app: typer.Typer,
    request: Callable[..., Any],
    show: Callable[[Any], None],
    get_console: Callable[[], Console],
    clean: Callable[[str], str],
) -> None:
    jd = typer.Typer(no_args_is_help=True, help="逐字导入JD和显式预算的需求提取")
    assessment = typer.Typer(no_args_is_help=True, help="已提交尝试的模型评价候选，不代表掌握")
    app.add_typer(jd, name="jd")
    app.add_typer(assessment, name="assessment")

    @jd.command("import")
    def import_jd(
        file: Path,
        namespace: str = typer.Option(..., help="明确的本人私人namespace ID"),
        jd_id: str = typer.Option(..., help="明确的岗位描述身份，不从文件名推断"),
        job_title: str | None = None,
        source_kind: str | None = None,
        source_label: str | None = None,
        source_url: str | None = None,
        source_version: str | None = None,
    ):
        """保留UTF-8原文、CRLF和空白；未知来源字段为null，不访问来源URL。"""
        body = _literal_jd(file)
        try:
            source = None
            if any(value is not None for value in (source_kind, source_label, source_url)):
                source = JDSource(kind=source_kind, source_label=source_label, url=source_url)
            value = JDImportInput(
                namespace_id=namespace,
                jd_id=jd_id,
                raw_text=body.decode("utf-8"),
                job_title=job_title,
                source=source,
                source_version=source_version,
                raw_text_sha256=hashlib.sha256(body).hexdigest(),
                raw_text_bytes=len(body),
            )
        except ValidationError:
            raise typer.BadParameter("JD原文或元数据不符合导入合同；未发送请求。") from None
        show(request("POST", "/api/v1/jds", value.model_dump(mode="json")))

    @jd.command("show")
    def show_jd(revision: str):
        """读取本人不可变JD版本；不提取或授予模型额度。"""
        show(request("GET", f"/api/v1/jds/{_identifier(revision)}"))

    @jd.command("extract")
    def extract_jd(file: Path):
        """JSON须指定namespace、funding、jd_revision和idempotency_key。"""
        body = load_contract(file, JDExtractionInput)
        if body.get("max_attempts") is None:
            body.pop("max_attempts", None)
        show(request("POST", "/api/v1/jds/extractions", body))

    @assessment.command("request")
    def request_assessment(file: Path):
        """显式申请有预算的待复核评价；不写真人观察、运行结果或掌握状态。"""
        show(request("POST", "/api/v1/learning/assessments", load_contract(file, AssessmentInput)))

    @app.command("project-analysis")
    def project_analysis(run_id: str):
        """只读投影固定构建的结构、已审命题和未知范围；不追加模型调用。"""
        show(request("GET", f"/api/v1/knowledge/builds/{_identifier(run_id)}/project-analysis"))

    @app.command("source-targets")
    def source_targets(turn_id: str):
        """列出回答中的block与citation编号；不显示源码，不发送ACK。"""
        identifier = _identifier(turn_id, limit=100)
        packet = request("GET", f"/api/v1/turns/{identifier}")
        try:
            invalidated = {
                event["payload"]["block_id"]
                for event in packet["events"]
                if event["type"] == "answer.evidence_invalidated"
            }
            targets = []
            for event in packet["events"]:
                if event["type"] != "answer.block.validated":
                    continue
                payload = event["payload"]
                if payload["block_id"] in invalidated:
                    continue
                for index, citation in enumerate(payload["content"].get("citations", [])):
                    if all(citation.get(key) is not None for key in _TARGET_FIELDS):
                        targets.append(
                            {
                                "answer_block_id": payload["block_id"],
                                "citation_index": index,
                                **{key: citation[key] for key in _TARGET_FIELDS},
                            }
                        )
            result = {
                "turn_id": identifier,
                "status": packet["status"],
                "targets": targets,
                "ack_sent": False,
            }
        except (KeyError, TypeError, AttributeError):
            raise typer.BadParameter("回答引用列表结构无效；未发送ACK。") from None
        show(result)

    @app.command("source-show")
    def source_show(
        block_id: str,
        citation_index: int = typer.Option(0, min=0, max=100),
        ack_key: str | None = typer.Option(
            None, help="显式记录本次客户端显示回执的幂等键；省略则只读，不发送ACK"
        ),
    ):
        """显示验证过的完整选中行；仅在显示并flush成功后可提交匹配的ACK。"""
        identifier = _identifier(block_id, limit=100)
        if ack_key is not None and not 1 <= len(ack_key) <= 150:
            raise typer.BadParameter("ACK幂等键必须为1至150个字符；未发送请求。")
        response = request(
            "GET", f"/api/v1/learning/navigation/{identifier}?citation_index={citation_index}"
        )
        target, lines = _display_target(response, identifier, citation_index, ack_key)
        try:
            render_source(get_console(), clean, target, lines)
        except (OSError, UnicodeError):
            # Do not retry output or POST when receipt delivery is uncertain.
            raise typer.Exit(1) from None
        if ack_key is not None:
            receipt = request(
                "POST", "/api/v1/learning/navigation-acks", target.model_dump(mode="json")
            )
            show(receipt)
        else:
            get_console().print("仅显示，未发送ACK。")
