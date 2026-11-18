"""Thin typed control-plane commands; all decisions remain server-authorized.

JSON request files are deliberate: they expose funding, revisions and explicit
confirmations instead of silently manufacturing consent in a convenience CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import typer


def load_request(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 8_000_000:
        raise typer.BadParameter("请求文件超过8MB。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise typer.BadParameter("请求必须为UTF-8 JSON对象。") from None
    if not isinstance(value, dict):
        raise typer.BadParameter("请求必须为JSON对象。")
    return value


def save_private_json(path: Path, value: Any) -> None:
    # Never replace an existing export or follow a pre-created symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def register_cli(
    app: typer.Typer,
    request: Callable[..., Any],
    show: Callable[[Any], None],
    config: Callable[[], dict[str, Any]],
    save_config: Callable[[dict[str, Any]], None],
) -> None:
    taxonomy = typer.Typer(help="分类提议、审批、冻结版本与只读历史映射")
    maintenance = typer.Typer(help="固定来源增量复核与显式授权的周期任务")
    shared = typer.Typer(help="系统资金公共构建与不转移费用的个人订阅")
    privacy = typer.Typer(help="个人资料导出、两阶段确认擦除与状态")
    index = typer.Typer(help="明确授权的批量索引任务")
    for name, group in [
        ("taxonomy", taxonomy),
        ("maintenance", maintenance),
        ("shared", shared),
        ("privacy", privacy),
        ("index", index),
    ]:
        app.add_typer(group, name=name)

    def send(method: str, path: str, body: Path | None = None):
        result = request(method, path, load_request(body) if body else None)
        show(result)
        return result

    def identifier(value: str) -> str:
        return quote(value, safe="")

    @taxonomy.command("status")
    def taxonomy_status(namespace: str):
        send("GET", "/api/v1/taxonomy/status?" + urlencode({"namespace_id": namespace}))

    @taxonomy.command("propose")
    def taxonomy_propose(body: Path):
        send("POST", "/api/v1/taxonomy/proposals", body)

    @taxonomy.command("proposal")
    def taxonomy_proposal(proposal: str):
        send("GET", "/api/v1/taxonomy/proposals/" + identifier(proposal))

    @taxonomy.command("decide")
    def taxonomy_decide(proposal: str, body: Path):
        send("POST", "/api/v1/taxonomy/proposals/" + identifier(proposal) + "/decisions", body)

    @taxonomy.command("release")
    def taxonomy_release(release: str):
        send("GET", "/api/v1/taxonomy/releases/" + identifier(release))

    @taxonomy.command("mapping")
    def taxonomy_mapping(publication: str, topic: str, schema_version: int | None = None):
        query: dict[str, str | int] = {"topic_key": topic}
        if schema_version is not None:
            query["schema_version"] = schema_version
        send("GET", "/api/v1/taxonomy/mapping/" + identifier(publication) + "?" + urlencode(query))

    @maintenance.command("plan")
    def maintenance_plan(body: Path):
        send("POST", "/api/v1/maintenance/plans", body)

    @maintenance.command("detail")
    def maintenance_detail(plan: str):
        send("GET", "/api/v1/maintenance/plans/" + identifier(plan))

    @maintenance.command("execute")
    def maintenance_execute(plan: str, body: Path):
        send("POST", "/api/v1/maintenance/plans/" + identifier(plan) + "/execute", body)

    @maintenance.command("schedule")
    def maintenance_schedule(body: Path):
        send("POST", "/api/v1/maintenance/schedules", body)

    @maintenance.command("toggle")
    def maintenance_toggle(schedule: str, body: Path):
        send("PATCH", "/api/v1/maintenance/schedules/" + identifier(schedule), body)

    @maintenance.command("tick")
    def maintenance_tick(schedule: str):
        send("POST", "/api/v1/maintenance/schedules/" + identifier(schedule) + "/tick")

    @shared.command("create")
    def shared_create(body: Path):
        send("POST", "/api/v1/shared-builds", body)

    @shared.command("detail")
    def shared_detail(run: str):
        send("GET", "/api/v1/shared-builds/" + identifier(run))

    @shared.command("subscribe")
    def shared_subscribe(run: str, body: Path):
        send("POST", "/api/v1/shared-builds/" + identifier(run) + "/subscriptions", body)

    @shared.command("unsubscribe")
    def shared_unsubscribe(run: str, body: Path):
        send("POST", "/api/v1/shared-builds/" + identifier(run) + "/unsubscribe", body)

    @index.command("create")
    def index_create(body: Path):
        send("POST", "/api/v1/retrieval/index-jobs", body)

    @privacy.command("policy")
    def privacy_policy():
        send("GET", "/api/v1/privacy/policy")

    @privacy.command("export")
    def privacy_export(output: Path):
        value = request("GET", "/api/v1/privacy/export")
        try:
            save_private_json(output, value)
        except OSError:
            raise typer.BadParameter("无法创建私有导出文件；不会覆盖已有路径。") from None
        show({"saved_to": str(output), "permissions": "0600", "contains_personal_data": True})

    @privacy.command("propose")
    def privacy_propose():
        show(
            request("POST", "/api/v1/privacy/deletion-proposals", {"scope": "all_personal_content"})
        )

    @privacy.command("status")
    def privacy_status(proposal: str):
        send("GET", "/api/v1/privacy/deletion-proposals/" + identifier(proposal))

    @privacy.command("confirm")
    def privacy_confirm(proposal: str, body: Path):
        """只转发显式nonce/hash/不可逆确认；完成后清空本机旧会话和预算指针。"""
        result = send(
            "POST", "/api/v1/privacy/deletion-proposals/" + identifier(proposal) + "/confirm", body
        )
        new_namespace = result.get("result", {}).get("new_namespace_id")
        if result.get("state") == "completed" and isinstance(new_namespace, str):
            state = config()
            for key in (
                "conversation_id",
                "funding_id",
                "last_turn_id",
                "last_event_id",
                "last_turn_status",
            ):
                state.pop(key, None)
            state["namespace_id"] = new_namespace
            save_config(state)
