"""One bounded, source-pinned supplement per desk question, using native gates."""

from sqlalchemy import select

from .errors import DomainError
from .models import AgentRun, Conversation, FundingAgreement, KnowledgeGap, SourceRead, Turn
from .services.gap_scheduling import process_auto_gap


async def supplement_desk_question(service, job, persist=lambda: None):
    if not job.get("funding_id") or not job.get("conversation_id"):
        return
    with service.db.read() as db:
        candidates = list(
            db.execute(
                select(KnowledgeGap, Turn)
                .join(Turn, KnowledgeGap.turn_id == Turn.id)
                .join(AgentRun, Turn.run_id == AgentRun.id)
                .where(
                    AgentRun.funding_id == job["funding_id"],
                    AgentRun.principal_id == job["actor"],
                    Turn.conversation_id == job["conversation_id"],
                    Turn.status == "completed",
                )
                .order_by(Turn.created_at.desc())
            )
        )
        candidates = [(g, t) for g, t in candidates if not t.focus.get("gap_reanswer")]
        if not candidates:
            return
        gap, turn = candidates[0]
        conv = db.get(Conversation, turn.conversation_id)
        if conv.revision != turn.focus_revision + 1:
            job["gap_supplement"] = {"status": "blocked", "reason": "focus_changed"}
            return
        if len(gap.source_ids) != 1:
            job["gap_supplement"] = {"status": "needs_source", "gap_id": gap.id}
            job["reply"] = (
                "暂无法确定本次补充的项目范围。请明确一个固定版本的项目来源，我会帮你按原问题补充提取和审核；目前不能据此判断项目没有该实现。"
            )
            return
        source = service.sources.get(db, job["actor"], gap.source_ids[0])
        scope = {
            "source_id": source.id,
            "repository": source.repository_key,
            "commit": source.commit_sha,
            "available_files": sorted(source.files),
        }
        gap_id = gap.id
        funding = db.get(FundingAgreement, job["funding_id"])
        # Reserve the full 24-call source explanation, including its completion check.
        available = funding.call_cap - funding.calls_consumed - funding.calls_held if funding else 0
        existing_build = db.get(AgentRun, gap.build_run_id) if gap.build_run_id else None
        supplement_cap = existing_build.call_cap if existing_build else min(34, available - 24)
        if supplement_cap < 15:
            job["gap_supplement"] = {
                "status": "blocked",
                "reason": "insufficient_retry_budget",
                "gap_id": gap_id,
            }
            job["reply"] = (
                "当前模型额度不足以完成独立补证和复答；已保留已审核内容及缺口，尚未启动新的补证。"
            )
            return
    job["status"] = "running"
    job["gap_supplement"] = {"gap_id": gap_id, "status": "extracting", **scope}
    job["reply"] = (
        "现有已审核知识未覆盖你的问题，正在固定源码版本中定向提取并独立审核，随后自动回答原问题。"
    )
    persist()
    try:
        result = service.lifecycle.schedule_gap(
            job["actor"],
            gap_id,
            job["funding_id"],
            supplement_cap,
            "desk-gap:" + gap_id,
            source_id=scope["source_id"],
            max_units_per_project=1,
            duration_minutes=90,
            planning_call_seconds=120,
            auto_evidence_retry=True,
            auto_reanswer=True,
        )
        from .desk_workflow import execute_desk_job

        await execute_desk_job(service, job, status_key="gap_execution_status")
        if job.get("gap_execution_status") == "interrupted":
            job["status"] = "interrupted"
            job["reply"] = "源码补充任务尚未结束，请查看后台状态；当前不能判断该实现不存在。"
            persist()
            return
        process_auto_gap(service.lifecycle, result["run_id"])
        job["status"] = "running"
        job["gap_supplement"]["status"] = "reanswering"
        job["reply"] = "定向补充任务已结束，正在核验审核结果并尝试复答原问题。"
        persist()
        await execute_desk_job(service, job, status_key="gap_execution_status")
        job["status"] = job.get("gap_execution_status", "completed")
        reconciled = service.lifecycle.reconcile_gap(job["actor"], gap_id)
        job["gap_supplement"].update(reconciled)
        with service.db.read() as db:
            reads = list(
                db.scalars(
                    select(SourceRead)
                    .join(AgentRun, SourceRead.run_id == AgentRun.id)
                    .where(
                        AgentRun.funding_id == job["funding_id"],
                        AgentRun.principal_id == job["actor"],
                        AgentRun.workflow.in_(["analysis", "review"]),
                        SourceRead.source_id == scope["source_id"],
                    )
                )
            )
        job["gap_supplement"]["source_reads"] = [
            {"path": r.path, "first_line": r.first_line, "last_line": r.last_line, "role": r.role}
            for r in reads
        ]
        if reconciled.get("resolved"):
            job["reply"] = (
                "已完成定向提取、独立审核和原问题复答。请查看新增回答及可点击的源码引用。"
            )
        else:
            job["reply"] = (
                "已尝试按原问题补充，但证据仍未充分覆盖问题。"
                f"核查范围：{scope['repository']} @ {scope['commit']}，"
                f"固定快照可用文件 {len(scope['available_files'])} 个。"
                "这不代表整个项目没有该实现；未纳入快照的文件尚未核查，审核或执行失败也不作为不存在的依据。"
                "本次实际读取："
                + ("、".join(sorted({r.path for r in reads})) or "尚未成功读取源码")
                + "。"
                "原因："
                + ", ".join(reconciled.get("reason_codes", []))
                + "。已保留已审核内容和未覆盖范围；本轮受限补证已结束，无需重复提供同一仓库。后续应依据具体审核缺口继续调查，只有需要其他版本或当前快照之外的来源时才需要补充来源。"
            )
    except DomainError as exc:
        job["gap_supplement"].update(status="blocked", reason=exc.code)
        job["reply"] = (
            "暂无法完成本次定向补充（" + exc.code + "），不能据此判断项目没有该实现。"
            "请查看任务状态并确认来源与可用额度，我会帮你核对下一步；本次未宣称知识入库完成。"
        )
        job["status"] = "completed"
    persist()
