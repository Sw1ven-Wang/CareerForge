"""Bind explicit project learning requests before attempting source-grounded teaching."""

import re

from .contracts import KnowledgeBuildInput, TurnInput
from .errors import DomainError
from .models import AgentRun, RunEvent

REPOSITORY = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)(?![\w/-])", re.I)
LEARN = re.compile(r"学|讲解|解释|分析|架构|源码|实现|architecture", re.I)


def repository_question(text, context):
    # Never interpret quoted examples, negations or unrelated executable actions.
    if re.search(r"不要|不想|别|字符串|引用示例|举例|例如|部署|修改|删除|执行命令", text):
        return None
    urls = REPOSITORY.findall(text)
    if urls:
        if len(set(urls)) != 1 or not LEARN.search(REPOSITORY.sub("", text)):
            return None
        return "https://github.com/" + urls[0].removesuffix(".git")
    if not LEARN.search(text):
        return None
    target = context.get("status_snapshot", {}).get("target") or ""
    candidates = [target] if REPOSITORY.fullmatch(target) else []
    if not candidates:
        # Recover previously unbound URLs only from caller messages, never agent replies.
        for item in reversed(context.get("history", [])):
            previous = item.get("question", "")
            if REPOSITORY.search(previous):
                selected = repository_question(previous, {})
                candidates = [selected] if selected else []
                break
    if len(candidates) != 1:
        return None
    repo = candidates[0]
    name = repo.rsplit("/", 1)[-1]
    if re.search(r"这个|该项目|它|^agent\s*架构(?:能力)?$", text, re.I) or re.search(
        rf"(?<![\w]){re.escape(name)}(?![\w])", text, re.I
    ):
        return repo
    return None


def prepare_project_question(app, job, context, repository):
    """Native owner/revision/budget admission. Returns False for an already bound source."""
    actor, cid = job["actor"], job["conversation_id"]
    with app.db.transaction() as db:
        conv = app.workflows.conversation(db, actor, cid, lock=True)
        expected = context["status_snapshot"].get("focus_revision")
        if conv.revision != expected:
            raise DomainError("REVISION_CONFLICT", "学习目标已变化，请重新提出项目问题。", 409)
        previous = app.workflows.owned_in(db, conv)
        if previous and previous.control_state != "active":
            raise DomainError("WORKFLOW_NOT_ACTIVE", "请先恢复当前工作流，再开始项目学习。", 409)
        if (
            previous
            and previous.target.get("target") == repository
            and previous.target.get("source_ids")
        ):
            return False
        fixed_source = None
        if previous and previous.target.get("target") == repository:
            from sqlalchemy import select

            from .models_dialogue import WorkflowRunLink

            prior_builds = db.scalars(
                select(AgentRun)
                .join(WorkflowRunLink, WorkflowRunLink.run_id == AgentRun.id)
                .where(
                    WorkflowRunLink.workflow_id == previous.id,
                    AgentRun.workflow == "knowledge_build",
                )
            )
            for prior in prior_builds:
                if prior.status not in {"failed", "partially_succeeded", "budget_stopped"}:
                    continue
                saved = app.runtime.stage(prior.id, "sources") or {}
                if len(saved.get("source_ids", [])) == 1:
                    candidate = app.sources.get(db, actor, saved["source_ids"][0])
                    if "https://github.com/" + candidate.repository_key == repository:
                        fixed_source = candidate.id
        workflow = app.workflows.create_in(
            db,
            conv,
            parent=previous,
            target={
                "entry": "set_repository",
                "target": repository,
                "desk_project_question": job["question"],
                "funding_ref": job["funding_id"],
            },
        )
        # This question requests explanation, not a new plan or external project search.
        run_id = app.build.start(
            actor,
            KnowledgeBuildInput(
                namespace_id=conv.namespace_id,
                funding_id=job["funding_id"],
                repository_url=None if fixed_source else repository,
                source_id=fixed_source,
                question=job["question"],
                research_scope="focused",
                call_cap=34,
                auto_evidence_retry=True,
                max_projects=1,
                max_units_per_project=1,
                required_dimensions=["project_structure", "module_responsibilities", "call_flow"],
                duration_minutes=90,
                planning_call_seconds=120,
                idempotency_key="desk-project:" + job["run_id"],
            ),
            db=db,
        )
        app.workflows.link_in(db, workflow, db.get(AgentRun, run_id), "build")
        workflow.milestone = "building"
        job.update(
            agent="workflow",
            workflow_id=workflow.id,
            status="running",
            project_question={
                "repository": repository,
                "question": job["question"],
                "build_run_id": run_id,
                "status": "building",
            },
            reply="已绑定指定项目，正在固定源码版本、提取并独立审核；完成后回答你的原问题。",
        )
        job["run_refs"] = [*job.get("run_refs", []), run_id]
        job["project_question"]["focus_revision"] = conv.revision
    return True


async def complete_project_question(app, job, persist):
    from .desk_workflow import execute_desk_job

    persist()
    await execute_desk_job(app, job, status_key="project_execution_status")
    info = job["project_question"]
    with app.db.transaction() as db:
        conv = app.workflows.conversation(db, job["actor"], job["conversation_id"], lock=True)
        workflow = app.workflows.owned_in(db, conv, job["workflow_id"])
        if (
            conv.focus.get("active_workflow_id") != workflow.id
            or conv.revision != info["focus_revision"]
            or workflow.control_state != "active"
        ):
            job.update(
                status="completed",
                reply="项目知识任务已结束，但会话目标或状态已改变，未向新目标自动追加回答。",
            )
            info["status"] = "focus_changed"
            return
        publications = app.continuation.publications(
            db, job["actor"], workflow.target.get("publication_ids", [])
        )
        if not publications:
            info["status"] = "needs_evidence"
            from sqlalchemy import select

            failures = db.scalars(
                select(RunEvent)
                .join(AgentRun, RunEvent.run_id == AgentRun.id)
                .where(
                    AgentRun.funding_id == job["funding_id"], RunEvent.event_type == "run.finished"
                )
            )
            codes = list(
                dict.fromkeys(e.payload["code"] for e in failures if e.payload.get("code"))
            )
            info["failure_codes"] = codes
            relay_hint = (
                "中转站模型请求失败，源码已保留。该次调用结果未知，系统没有自动重放。"
                "请先核对中转站的失败记录；恢复后可重新提交同一问题，复用固定源码重新构建，可能产生新的模型费用。"
                if "MODEL_HTTP_ERROR" in codes
                else ""
            )
            job.update(
                status=job.get("project_execution_status", "failed"),
                reply=(
                    "已收到项目和学习目标，但本次尚未获得可用的已审核知识，未完成架构讲解。"
                    "请查看构建任务状态；这不代表项目没有该架构，无需重复提供相同仓库。"
                    + ("本次阻塞原因：" + "、".join(codes) + "。" if codes else "")
                    + relay_hint
                ),
            )
            return
        sources = list(
            dict.fromkeys(
                app.knowledge.publication_pack(db, job["actor"], p)["source_id"]
                for p in publications
            )
        )
        workflow.target = {**workflow.target, "source_ids": sources}
        app.workflows.focus_in(db, conv, workflow)
        revision = conv.revision
    outcome = app.conversations.submit(
        job["actor"],
        job["conversation_id"],
        TurnInput(
            question=info["question"],
            expected_revision=revision,
            funding_id=job["funding_id"],
            idempotency_key="desk-project-answer:" + info["build_run_id"],
        ),
    )
    info.update(
        status="answering",
        source_ids=sources,
        publication_ids=publications,
        answer_turn_id=outcome["turn_id"],
    )
    job.update(status="running", reply="指定项目的知识已通过审核发布，正在依据源码回答原问题。")
    persist()
    await execute_desk_job(app, job, status_key="project_answer_status")
    events = app.answers.events(job["actor"], outcome["turn_id"])
    answered = any(
        e.get("type") == "answer.completed"
        and e.get("payload", {}).get("task_satisfied") is True
        and e.get("payload", {}).get("answer_coverage_status") == "complete"
        for e in events["events"]
    )
    info["status"] = "answered" if answered else "needs_evidence"
    job["status"] = job["project_answer_status"]
    job["reply"] = (
        "已完成指定项目的知识提取、独立审核和原问题讲解，请查看回答及源码引用。"
        if answered
        else "项目已有审核材料，但本次回答尚未完整覆盖原问题，尚不能视为讲解完成；无需重复提供项目地址。"
    )
