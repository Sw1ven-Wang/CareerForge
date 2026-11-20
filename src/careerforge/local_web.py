"""Loopback-only learning desk. Credentials stay in the local private store.

The desk uses existing domain services and authenticated API contracts. Only runs
created here are executed; historical pending jobs are never drained implicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import create_app
from .application import Application
from .config import Settings
from .errors import DomainError
from .models import (
    AgentRun,
    CareerGoal,
    Conversation,
    GoalVersion,
    LearningPlan,
    ModelReceipt,
    ObjectRow,
    Publication,
    TeachingObservation,
    Turn,
)
from .services.privacy import assert_personal_access
from .services.security import accessible_namespaces, authenticate, private_namespace
from .util import fingerprint

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "web"
TERMINAL = {"succeeded", "failed", "partially_succeeded", "budget_stopped", "cancelled"}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=10000)
    conversation_id: str | None = None
    request_id: str = Field(min_length=1, max_length=100)
    use_model: bool = False
    agent: Literal["auto", "dialogue", "tutor"] = "auto"
    publication_ids: list[str] = Field(default_factory=list, max_length=20)
    confirm_version: str | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=2)


def create_local_web(service: Application | None = None) -> FastAPI:
    injected = service is not None
    service = service or Application(Settings.from_env())
    if service.settings.mode != "development" and not (
        injected and service.settings.mode == "test"
    ):
        raise RuntimeError("The local learning desk requires development mode.")
    native = create_app(service)
    state_dir = service.settings.state_dir
    journal_path = state_dir / "web-jobs.json"
    jobs = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
    tasks: set[asyncio.Task] = set()
    admission = asyncio.Lock()

    def persist():
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = journal_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(journal_path)

    def identity():
        config = json.loads((state_dir / "client.json").read_text(encoding="utf-8"))
        with service.db.read() as db:
            user = authenticate(db, config["token"])
            assert_personal_access(db, user.id)
            namespace = private_namespace(db, user.id)
            return {
                "id": user.id,
                "namespace_id": namespace.id,
                "token": config["token"],
                "name": user.name,
            }

    async def call(user, method, path, body=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=native), base_url="http://local"
        ) as client:
            response = await client.request(
                method, path, json=body, headers={"Authorization": "Bearer " + user["token"]}
            )
        if not response.is_success:
            value = response.json()
            raise HTTPException(
                response.status_code, detail=value.get("error", value.get("detail", "请求未完成"))
            )
        return response.json()

    async def execute(key):
        job = jobs[key]
        try:
            # Run the existing worker on its own event loop/thread so its SQLite
            # operations cannot block browser polling or UI requests.
            from .desk_workflow import execute_desk_job

            if job.get("agent") == "intent":
                phase = dict(job)
                await asyncio.to_thread(
                    lambda phase=phase: asyncio.run(execute_desk_job(service, phase))
                )
                job["run_refs"] = phase.get("run_refs", [])
                await execute_intent(job, phase["status"])
                if job["status"] == "running":
                    job["status"] = "completed"
            else:
                await asyncio.to_thread(
                    lambda: asyncio.run(
                        execute_desk_job(service, job, status_key="execution_status")
                    )
                )
                job["status"] = job.pop("execution_status")
            if job["status"] == "completed" and job.get("agent") != "dialogue":
                from .desk_gaps import supplement_desk_question

                job["status"] = "running"
                await asyncio.to_thread(
                    lambda: asyncio.run(supplement_desk_question(service, job, persist))
                )
                if job["status"] == "running":
                    job["status"] = "completed"
            if job.get("agent") == "dialogue" and job.get("run_id") and not job.get("routing"):
                result = service.runtime.stage(job["run_id"], "result")
                job["reply"] = (
                    result["reply"]
                    if result
                    else "对话协调 agent 本次回复未完成，请查看任务状态后重试；不会重复启动教学任务。"
                )
        except Exception as exc:
            job["status"] = "interrupted"
            job["error"] = getattr(exc, "code", type(exc).__name__)
            log.error("Desk run %s stopped: %s", job["run_id"], job["error"])
        finally:
            if job.get("funding_id"):
                service.runtime.revoke_funding(job["actor"], job["funding_id"])
                job["funding_revoked"] = True
            persist()

    def intent_context(user, cid, text):
        detail = conversation(cid, user)
        history = [
            {"question": a["question"], "answer": a.get("reply", "")[:1500]}
            for a in detail["actions"]
        ][-6:]
        # Include actual teaching turns, not only the coordination journal.
        teaching = []
        for turn in detail["turns"][-3:]:
            if turn.get("status") == "blocked":
                continue
            teaching.append({"question": turn["question"][:2000], "status": turn["status"]})
        flow = detail.get("flow", {})
        plan = flow.get("plan") or {}
        return {
            "question": text,
            "history": history,
            "teaching_history": teaching,
            "status_snapshot": flow.get("status", {}),
            "plan": {
                "plan_id": plan.get("plan_id"),
                "active_version_id": plan.get("active_version_id"),
            },
            "snapshot_at": datetime.now(UTC).isoformat(),
        }

    async def execute_intent(job, classification_status):
        from .contracts_conversation_actions_v1 import DialogueMessage
        from .desk_workflow import execute_desk_job

        decision = service.runtime.stage(job["run_id"], "result")
        job["routing"] = decision or {"intents": ["unknown"], "reasoning": "意图识别未完成"}
        if not decision or classification_status != "completed":
            job["status"] = classification_status
            job["reply"] = "意图识别未完成，尚未启动学习动作。请重试或选择对话对象。"
            return
        user = {"id": job["actor"], "namespace_id": job["namespace_id"]}
        cid = job["conversation_id"]
        with service.db.read() as db:
            route_run = db.get(AgentRun, job["run_id"])
            context = route_run.input_manifest["context"]
        intents = decision["intents"]
        job["agent"] = "dialogue"
        from .desk_projects import (
            complete_project_question,
            prepare_project_question,
            repository_question,
        )

        repository = repository_question(job["question"], context)
        if repository and intents in (
            ["explain"],
            ["reexplain"],
            ["unknown"],
            ["start"],
            ["discover"],
        ):
            try:
                async with admission:
                    if any(
                        j is not job
                        and j["actor"] == user["id"]
                        and j["status"] == "running"
                        and j.get("agent") not in {"dialogue", "intent"}
                        for j in jobs.values()
                    ):
                        job["reply"] = "项目学习任务仍在运行，未重复启动。你可以查询进度。"
                        return
                    prepared = prepare_project_question(service, job, context, repository)
                if prepared:
                    await asyncio.to_thread(
                        lambda: asyncio.run(complete_project_question(service, job, persist))
                    )
                    return
                if intents in (["unknown"], ["start"], ["discover"]):
                    intents = ["explain"]
                    job["routing"] = {
                        **decision,
                        "intents": intents,
                        "context_resolution": "bound_project_question",
                    }
            except DomainError as exc:
                job.update(status="completed", reply=exc.message, error=exc.code)
                return
        if intents == ["progress"]:
            from .desk_dialogue import progress_reply

            view = dialogue_status(user, cid)
            job["reply"] = progress_reply(view, bool(view["active_stages"]))
            return
        if intents == ["chat"]:
            job["status"] = "running"
            job["reply"] = "已识别为一般交流，交给对话协调 Agent。"
            manifest = {
                "conversation_id": cid,
                "model_profile": service.provider.profile_version,
                "data_classes": ["learning_state", "private_jd"],
                "access_dependencies": [],
                "context": context,
            }
            rid = service.runtime.create(
                user["id"],
                user["namespace_id"],
                job["funding_id"],
                "desk_dialogue",
                manifest,
                1,
                "intent-chat:" + job["run_id"],
            )
            job["dialogue_run_id"] = rid
            phase = dict(job)
            await asyncio.to_thread(
                lambda phase=phase: asyncio.run(execute_desk_job(service, phase))
            )
            job["run_refs"] = phase.get("run_refs", [])
            job["status"] = phase["status"]
            result = service.runtime.stage(rid, "result")
            job["reply"] = result["reply"] if result else "对话回复未完成，请查看任务状态。"
            return
        instructions = {
            "confirm_plan": "请查看当前计划，并点击「确认此版本并开始学习」。",
            "submit_answer": "请用「提交答案：你的完整答案」明确提交。",
            "advance": "请点击当前模块的阅读完成确认按钮。",
            "discover": "请发送「搜项目并入库」，以明确授权项目调研。",
            "change_goal": "请发送新的「JD：岗位要求」或「我想学习 具体主题」。",
            "pause": "请发送「暂停当前任务」。",
            "resume": "请发送「/resume」。",
            "cancel": "请发送「取消当前任务」。",
            "unknown": "暂无法确定你希望执行的操作。请说明想学习的项目或模块，以及希望讲解、练习还是查询进度，我会帮你匹配下一步；尚未启动新的学习动作。",
        }
        if any(i in instructions for i in intents):
            job["reply"] = "\n".join(instructions[i] for i in intents if i in instructions)
            return
        expected = context["status_snapshot"].get("focus_revision")
        frozen_workflow = context["status_snapshot"].get("workflow_id")
        replies = []
        job["steps"] = []
        for index, intent in enumerate(intents):
            async with admission:
                view = service.workflows.status(user["id"], cid)
                if view["focus_revision"] != expected or view.get("workflow_id") != frozen_workflow:
                    replies.append("识别期间会话焦点已改变，未继续操作；请重新提出请求。")
                    break
                if any(
                    j is not job
                    and j["actor"] == user["id"]
                    and j["status"] == "running"
                    and j.get("agent") not in {"dialogue", "intent"}
                    for j in jobs.values()
                ):
                    replies.append(
                        "教学任务仍在执行，未重复启动。你可以继续询问进度或与协调 Agent 交谈。"
                    )
                    break
                flow = flow_detail(user, cid)
                from .desk_dialogue import start_reply

                if intent == "start":
                    guidance = start_reply(flow, False)
                    if guidance:
                        replies.append(guidance)
                        break
                    intent = "reexplain"
                text = job["question"]
                actions = {
                    "practice": "start_practice",
                    "assessment": "assess_practice",
                    "plan": "propose_plan",
                }
                forced = [(actions[intent], {})] if intent in actions else None
                if intent in {"explain", "reexplain"}:
                    if (flow.get("plan") or {}).get("active_version_id"):
                        if intent == "reexplain":
                            text = "请基于当前模块的已审核源码，重新解释该模块已选事实的具体行为、成立条件与边界，用更简单的步骤表达，不增加未经审核的事实。"
                        text = "开始讲解，" + text
                        forced = [("start_tutor", {})]
                    elif intent == "reexplain" and context.get("teaching_history"):
                        text = (
                            "请基于已审核知识回答。原问题："
                            + context["teaching_history"][-1]["question"]
                            + "\n用户追问："
                            + text
                        )
                elif intent not in actions:
                    replies.append("此意图尚未支持自动执行，请使用页面操作。")
                    break
                try:
                    outcome = service.dialogue.dispatch(
                        user["id"],
                        cid,
                        DialogueMessage(
                            text=text,
                            expected_focus_revision=expected,
                            authorization_ref=job["funding_id"],
                            idempotency_key=f"intent:{route_run.id}:{index}",
                        ),
                        forced_actions=forced,
                        workflow_id=frozen_workflow,
                        expected_workflow_revision=(
                            context["status_snapshot"].get("workflow_revision")
                            if index == 0
                            else view.get("workflow_revision")
                        ),
                    )
                except DomainError as exc:
                    from .desk_capabilities import blocked_reply

                    replies.append(blocked_reply(exc.code, exc.message))
                    job["steps"].append({"intent": intent, "status": "blocked", "code": exc.code})
                    break
                replies.append(outcome.get("reply", "已交给教学工作流。"))
                job["steps"].append({"intent": intent, "status": "running", "result": outcome})
                job["run_refs"] = list(
                    dict.fromkeys([*job.get("run_refs", []), *outcome.get("run_refs", [])])
                )
                if outcome.get("status") in {"waiting_confirmation", "failed"} or any(
                    r.get("status") in {"failed", "rejected", "waiting_confirmation"}
                    for r in outcome.get("action_receipts", [])
                ):
                    job["steps"][-1]["status"] = "blocked"
                    break
                job["agent"] = "workflow"
                job["status"] = "running"
                job["reply"] = "\n".join(replies)
                persist()
            phase = dict(job)
            await asyncio.to_thread(
                lambda phase=phase: asyncio.run(execute_desk_job(service, phase))
            )
            job["run_refs"] = phase.get("run_refs", [])
            job["steps"][-1]["status"] = phase["status"]
            if phase["status"] != "completed":
                job["status"] = phase["status"]
                replies.append("前一步未完成，后续动作未执行。")
                break
            if (
                intent in {"explain", "reexplain"}
                and index + 1 < len(intents)
                and outcome.get("turn_id")
            ):
                events = service.answers.events(user["id"], outcome["turn_id"])
                if '"repository_fact"' not in json.dumps(events):
                    job["steps"][-1]["status"] = "blocked"
                    replies.append(
                        "本次讲解未返回已审核源码事实，暂不自动出题。请先明确学习材料或补充证据。"
                    )
                    break
            expected = service.workflows.status(user["id"], cid)["focus_revision"]
        job["reply"] = "\n".join(replies)

    @asynccontextmanager
    async def lifespan(_):
        # Do not silently repeat an uncertain paid attempt after restart.
        for job in jobs.values():
            if job.get("status") == "running":
                with service.db.read() as db:
                    run = db.get(AgentRun, job.get("run_id"))
                    job["status"] = (
                        "interrupted"
                        if job.get("workflow_id")
                        else (run.status if run and run.status in TERMINAL else "interrupted")
                    )
                if job.get("funding_id"):
                    service.runtime.revoke_funding(job["actor"], job["funding_id"])
                    job["funding_revoked"] = True
        if jobs:
            persist()
        yield
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.path.startswith("/desk/"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if (origin and origin != expected) or request.headers.get("x-careerforge-desk") != "1":
                return JSONResponse({"detail": "仅接受本机工作台请求。"}, status_code=403)
            if request.method not in {"GET", "HEAD"} and not request.headers.get(
                "content-type", ""
            ).startswith("application/json"):
                return JSONResponse({"detail": "需要 JSON 请求。"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    def public_job(job):
        return {k: v for k, v in job.items() if k not in {"actor", "funding_id"}}

    @app.get("/desk/overview")
    def overview(user=Depends(identity)):
        with service.db.read() as db:
            namespaces = [n.id for n in accessible_namespaces(db, user["id"])]
            ids = list(
                db.scalars(select(Publication.id).where(Publication.namespace_id.in_(namespaces)))
            )
            plans = list(
                db.scalars(
                    select(LearningPlan.id)
                    .where(
                        LearningPlan.principal_id == user["id"],
                        LearningPlan.namespace_id == user["namespace_id"],
                    )
                    .order_by(LearningPlan.created_at.desc())
                )
            )
            convs = list(
                db.scalars(
                    select(Conversation)
                    .where(
                        Conversation.principal_id == user["id"],
                        Conversation.namespace_id == user["namespace_id"],
                    )
                    .order_by(Conversation.created_at.desc())
                    .limit(60)
                )
            )
            conversations = []
            for conv in convs:
                first = db.scalar(
                    select(Turn)
                    .where(Turn.conversation_id == conv.id)
                    .order_by(Turn.created_at)
                    .limit(1)
                )
                local = next(
                    (
                        j
                        for j in jobs.values()
                        if j.get("conversation_id") == conv.id and j["actor"] == user["id"]
                    ),
                    None,
                )
                if first or local:
                    conversations.append(
                        {
                            "id": conv.id,
                            "title": local.get("question", "学习流程")[:70]
                            if local
                            else first.question[:70],
                            "created_at": conv.created_at.isoformat(),
                        }
                    )
            observations = db.scalars(
                select(TeachingObservation).where(TeachingObservation.principal_id == user["id"])
            ).all()
        publications, unavailable = [], 0
        for identifier in ids:
            try:
                with service.db.read() as db:
                    pack = service.knowledge.publication_pack(db, user["id"], identifier)
                publications.append(pack)
            except DomainError:
                unavailable += 1
        return {
            "name": user["name"],
            "model": service.settings.model_name,
            "model_configured": bool(
                service.settings.model_name
                and service.settings.model_api_key
                and service.settings.model_base_url
            ),
            "publications": publications,
            "unavailable_publications": unavailable,
            "plans": [service.learning.detail(user["id"], p) for p in plans],
            "conversations": conversations,
            "observations": [
                {
                    "module_id": o.module_id,
                    "response": o.response,
                    "created_at": o.created_at.isoformat(),
                    "competence_verified": o.competence_verified,
                }
                for o in observations
            ],
            "jobs": [public_job(j) for j in jobs.values() if j["actor"] == user["id"]],
        }

    def dialogue_status(user, cid):
        view = service.workflows.status(user["id"], cid)
        stages = list(view.get("active_stages", []))
        linked = set(view.get("run_refs", []))
        labels = {
            "chat": "教学 Agent：检索、讲解与证据核验",
            "desk_dialogue": "对话协调 Agent：组织回复",
            "desk_intent": "识别学习意图与上下文",
            "learning_plan": "生成学习计划",
        }
        labels.update(
            knowledge_build="按知识缺口补充固定源码",
            analysis="定向提取源码知识",
            review="独立审核新增知识",
        )
        with service.db.read() as db:
            for job in jobs.values():
                if (
                    job["actor"] != user["id"]
                    or job.get("conversation_id") != cid
                    or job["status"] != "running"
                ):
                    continue
                for rid in job.get("run_refs", []) or [job.get("run_id")]:
                    if not rid or rid in linked:
                        continue
                    run = db.get(AgentRun, rid)
                    if run and run.principal_id == user["id"] and run.status not in TERMINAL:
                        stages.append(
                            {
                                "run_id": rid,
                                "label": labels.get(run.workflow, run.workflow),
                                "status": run.status,
                            }
                        )
                        linked.add(rid)
        return {**view, "active_stages": stages}

    def flow_detail(user, cid):
        from .models_dialogue import WorkflowRunLink, WorkflowSession

        view = dialogue_status(user, cid)
        with service.db.read() as db:
            w = (
                db.get(WorkflowSession, view.get("workflow_id"))
                if view.get("workflow_id")
                else None
            )
            target = dict(w.target) if w else {}
        data = {"status": view}
        if view.get("goal_version_id"):
            with service.db.read() as db:
                from .services.research_coverage import read_goal_version

                data["goal"] = read_goal_version(db, user["id"], view["goal_version_id"])
        if view.get("goal_version_id"):
            from .services.project_pool import ProjectPoolService

            with service.db.read() as db:
                data["projects"] = ProjectPoolService(service).list_in(
                    db, user["id"], view["goal_version_id"], include_pending=False
                )
        proposal = target.get("proposal")
        if proposal:
            data["plan"] = service.learning.detail(user["id"], proposal["plan_id"])
        if target.get("activity_id"):
            data["activity"] = service.learning.activity_detail(user["id"], target["activity_id"])
        if target.get("attempt_id"):
            data["attempt"] = service.learning.attempt_detail(user["id"], target["attempt_id"])
        if target.get("last_tutor_turn_id"):
            try:
                binding = service.conversations.session_binding(
                    user["id"], target["last_tutor_turn_id"]
                )
                data["teaching"] = service.conversations.teaching_session(
                    user["id"], binding["learning_session_id"]
                )
            except DomainError as exc:
                data["teaching_error"] = {"code": exc.code, "message": exc.message}
        assessment_run_id = target.get("assessment_run_id")
        if not assessment_run_id and w:
            with service.db.read() as db:
                assessment_run_id = db.scalar(
                    select(WorkflowRunLink.run_id)
                    .where(
                        WorkflowRunLink.workflow_id == w.id,
                        WorkflowRunLink.milestone == "assessment",
                    )
                    .order_by(WorkflowRunLink.ordinal.desc())
                    .limit(1)
                )
        if assessment_run_id:
            with service.db.read() as db:
                run = db.get(AgentRun, assessment_run_id)
                result = service.runtime.stage(run.id, "result")
                data["assessment"] = {
                    "status": run.status,
                    "run_id": run.id,
                    "historical": not bool(target.get("assessment_run_id")),
                }
                if result:
                    data["assessment"]["result"] = service.assessments.validate_result(
                        db, user["id"], run, result
                    )
        if target.get("last_module_ack"):
            data["module_ack"] = target["last_module_ack"]
        return data

    @app.get("/desk/navigation/{block_id}")
    def navigation(
        block_id: str, citation_index: int = Query(0, ge=0, le=100), user=Depends(identity)
    ):
        # Native navigation rechecks ownership, evidence closure and snapshot integrity.
        target = service.learning.navigation_status(user["id"], block_id, citation_index)
        with service.db.read() as db:
            source = service.sources.get(db, user["id"], target["source_id"])
            target["repository"] = source.repository_key
        return target

    @app.get("/desk/conversations/{identifier}")
    def conversation(identifier: str, user=Depends(identity)):
        detail = service.conversations.detail(user["id"], identifier)
        with service.db.read() as db:
            turns = list(
                db.scalars(
                    select(Turn).where(Turn.conversation_id == identifier).order_by(Turn.created_at)
                )
            )
        rows = []
        for turn in turns:
            try:
                events = service.answers.events(user["id"], turn.id)
                rows.append(
                    {
                        "id": turn.id,
                        "question": turn.question
                        if events["status"] != "blocked"
                        else "此历史记录当前不可用",
                        "run_id": turn.run_id,
                        **events,
                    }
                )
            except DomainError as exc:
                rows.append(
                    {
                        "id": turn.id,
                        "question": "此历史记录当前不可用",
                        "status": "blocked",
                        "events": [],
                        "error": exc.code,
                    }
                )
        actions = [
            public_job(j)
            for j in jobs.values()
            if j["actor"] == user["id"]
            and j.get("conversation_id") == identifier
            and j.get("reply")
        ]
        try:
            flow = flow_detail(user, identifier)
        except DomainError as exc:
            flow = {"error": {"code": exc.code, "message": exc.message}}
        return {**detail, "turns": rows, "actions": actions, "flow": flow}

    async def start_job(user, key, operation, use_model, request_hash, call_cap=8, read_only=False):
        if key in jobs:
            if jobs[key]["actor"] != user["id"]:
                raise HTTPException(409, "请求身份冲突")
            if jobs[key].get("request_hash") != request_hash:
                raise HTTPException(409, "相同请求编号不能用于不同内容。")
            return public_job(jobs[key])
        if not read_only and any(
            j["actor"] == user["id"]
            and j["status"] == "running"
            and j.get("agent") not in {"dialogue", "intent"}
            for j in jobs.values()
        ):
            raise HTTPException(409, "当前任务正在执行，请等待完成后再提交。")
        if use_model and not (
            service.settings.model_name
            and service.settings.model_api_key
            and service.settings.model_base_url
        ):
            raise HTTPException(503, "尚未配置中转站模型，请检查启动环境。")
        funding_id = None
        if use_model:
            funding = await call(
                user,
                "POST",
                "/api/v1/funding",
                {
                    "namespace_id": user["namespace_id"],
                    "call_cap": call_cap,
                    "hours": 2 if call_cap >= 40 else 1,
                    "allowed_roles": [
                        "coordinator",
                        "planner",
                        "tutor",
                        "answer_checker",
                        "query_rewriter",
                        "coverage",
                        "evaluation",
                        "extract",
                        "reflector",
                        "learning_planner",
                        "analysis",
                        "reviewer",
                    ],
                    "data_classes": [
                        "public_source",
                        "private_source",
                        "private_jd",
                        "learning_state",
                    ],
                    "confirm": "AUTHORIZE",
                },
            )
            funding_id = funding["funding_id"]
        try:
            result = await operation(funding_id)
        except Exception:
            if funding_id:
                service.runtime.revoke_funding(user["id"], funding_id)
            raise
        if result.get("conversation_id"):
            detail = service.conversations.detail(user["id"], result["conversation_id"])
            if detail["preferences"].get("renewal_notice"):
                result["renewal_notice"] = detail["preferences"]["renewal_notice"]
                result["predecessor_conversation_id"] = detail["preferences"][
                    "predecessor_conversation_id"
                ]
        job = {
            **result,
            "actor": user["id"],
            "funding_id": funding_id,
            "request_hash": request_hash,
            "status": "running" if result.get("run_id") or result.get("run_refs") else "completed",
            "created_at": datetime.now(UTC).isoformat(),
        }
        jobs[key] = job
        persist()
        if job.get("run_id") or job.get("run_refs"):
            task = asyncio.create_task(execute(key))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        elif funding_id:
            service.runtime.revoke_funding(user["id"], funding_id)
        return public_job(job)

    @app.post("/desk/messages", status_code=202)
    async def message(body: Message, user=Depends(identity)):
        if not body.question.strip():
            raise HTTPException(422, "请输入学习问题。")
        async with admission:
            from . import desk_dialogue
            from .contracts_conversation_actions_v1 import DialogueMessage
            from .services.dialogue_routing import explicit_actions

            key = "message:" + body.request_id
            request_hash = fingerprint(body.model_dump())
            if key in jobs:
                if (
                    jobs[key]["actor"] != user["id"]
                    or jobs[key].get("request_hash") != request_hash
                ):
                    raise HTTPException(409, "相同请求编号不能用于不同内容。")
                return public_job(jobs[key])
            cid = body.conversation_id
            if cid is None:
                cid = (await call(user, "POST", "/api/v1/conversations", {"auto_build": False}))[
                    "conversation_id"
                ]
            from .desk_continuation import renew_if_stale

            cid = renew_if_stale(
                service,
                user["id"],
                cid,
                busy=any(
                    j["actor"] == user["id"]
                    and j.get("conversation_id") == cid
                    and j["status"] == "running"
                    for j in jobs.values()
                ),
            )
            conv = await call(user, "GET", "/api/v1/conversations/" + cid)
            text = body.question.strip()
            typed = bool(
                body.publication_ids
                or body.source_ids
                or body.confirm_version
                or re.match(
                    r"(?:提交答案|这是我的练习答案|我的答案)[：:]|(?:接受材料缺口并)?确认阅读完成",
                    text,
                )
            )
            known = explicit_actions(
                DialogueMessage(
                    text=text, expected_focus_revision=conv["revision"], idempotency_key="route"
                )
            )
            if (
                not typed
                and not (known and known[0][0] == "import_jd")
                and re.search(r"不要|不想|别给|然后|并且|同时", text)
            ):
                known = None
            running = any(
                j["actor"] == user["id"]
                and j.get("conversation_id") == cid
                and j["status"] == "running"
                for j in jobs.values()
            )
            reply = None
            override = None
            coordinator = False
            from .desk_capabilities import HELP_REPLY, intake_reply

            guidance = (
                intake_reply(text, dialogue_status(user, cid)) if not typed and not known else None
            )
            if not typed and body.agent != "tutor":
                if guidance:
                    reply = guidance
                elif desk_dialogue.START.fullmatch(text):
                    reply = desk_dialogue.start_reply(flow_detail(user, cid), running)
                    if reply is None:
                        override = "开始讲解"
                elif (known == [("query_progress", {})]) or re.fullmatch(
                    r"(?:现在|当前)?(?:在做什么|还在运行吗|还需要多久)[？?]?", text
                ):
                    reply = desk_dialogue.progress_reply(dialogue_status(user, cid), running)
                elif desk_dialogue.HELP.fullmatch(text):
                    reply = HELP_REPLY
                elif not known and body.agent == "dialogue":
                    coordinator = True
                elif not known and body.use_model:

                    async def recognize(funding_id):
                        from .services.learning_ext import _no_independent_assistance

                        with service.db.read() as db:
                            _no_independent_assistance(db, user["id"])
                        context = intent_context(user, cid, text)
                        rid = service.runtime.create(
                            user["id"],
                            user["namespace_id"],
                            funding_id,
                            "desk_intent",
                            {
                                "conversation_id": cid,
                                "model_profile": service.provider.profile_version,
                                "data_classes": ["learning_state", "private_jd"],
                                "access_dependencies": [],
                                "context": context,
                            },
                            1,
                            "desk-intent:" + body.request_id,
                        )
                        return {
                            "conversation_id": cid,
                            "namespace_id": user["namespace_id"],
                            "question": body.question,
                            "agent": "intent",
                            "run_id": rid,
                            "reply": "正在结合对话历史与学习状态识别意图…",
                        }

                    return await start_job(
                        user, key, recognize, True, request_hash, call_cap=80, read_only=True
                    )
                elif not known and not desk_dialogue.TEACH.search(text):
                    reply = "暂无法在关闭 AI 时理解这条自由表达。请开启 AI 并说明项目与学习目标，我会帮你识别下一步；你也可以直接发送「开始学习」「查询进度」「开始练习」。"
            if reply is not None or coordinator:

                async def coordinate(funding_id):
                    result = {
                        "conversation_id": cid,
                        "question": body.question,
                        "agent": "dialogue",
                        "reply": reply or "对话协调 agent 正在回复；后台教学任务独立执行。",
                    }
                    if coordinator and funding_id:
                        from .services.learning_ext import _no_independent_assistance

                        with service.db.read() as db:
                            _no_independent_assistance(db, user["id"])
                        history = [
                            {"question": j["question"], "reply": j.get("reply", "")[:2000]}
                            for j in jobs.values()
                            if j["actor"] == user["id"] and j.get("conversation_id") == cid
                        ][-8:]
                        result["run_id"] = service.runtime.create(
                            user["id"],
                            user["namespace_id"],
                            funding_id,
                            "desk_dialogue",
                            {
                                "conversation_id": cid,
                                "model_profile": service.provider.profile_version,
                                "data_classes": ["learning_state", "private_jd"],
                                "access_dependencies": [],
                                "context": {
                                    "question": text,
                                    "history": history,
                                    "status_snapshot": dialogue_status(user, cid),
                                    "snapshot_at": datetime.now(UTC).isoformat(),
                                },
                            },
                            1,
                            "desk-dialogue:" + body.request_id,
                        )
                    elif coordinator:
                        result["reply"] = (
                            "自由对话需要开启 AI。你仍可询问「当前进度」或发送「开始学习」，这些由协调层直接读取后台状态回答。"
                        )
                    return result

                return await start_job(
                    user,
                    key,
                    coordinate,
                    coordinator and body.use_model,
                    request_hash,
                    call_cap=1,
                    read_only=True,
                )

            async def operation(funding_id):
                conv = await call(user, "GET", "/api/v1/conversations/" + cid)
                text = override or body.question.strip()
                payload = {
                    "text": text,
                    "expected_focus_revision": conv["revision"],
                    "authorization_ref": funding_id,
                    "idempotency_key": "web:" + body.request_id,
                    "publication_ids": body.publication_ids,
                    "source_ids": body.source_ids,
                }
                from .services.dialogue_routing import _ASSESS_SUFFIX, _MATERIAL_PLAN

                selection = _MATERIAL_PLAN.fullmatch(text)
                if selection and not body.publication_ids:
                    repository = selection.group("repo").casefold()
                    focus = re.search(r"先学习(.+?)(?:[，,]|再学习|[。]|$)", text)
                    keywords = re.findall(r"[A-Za-z0-9_]+", focus.group(1)) if focus else []
                    materials = []
                    with service.db.read() as db:
                        ids = db.scalars(
                            select(Publication.id).where(
                                Publication.namespace_id == user["namespace_id"]
                            )
                        )
                        for pid in ids:
                            try:
                                pack = service.knowledge.publication_pack(db, user["id"], pid)
                            except DomainError:
                                continue
                            if (pack.get("repository") or "").casefold() != repository:
                                continue
                            content = json.dumps(
                                [pack["title"], pack["facts"], pack["citations"], pack["facets"]],
                                ensure_ascii=False,
                            ).casefold()
                            if all(word.casefold() in content for word in keywords):
                                materials.append(pid)
                    if not materials or len(materials) > 20:
                        raise HTTPException(409, "请从该项目选择 1 至 20 份已审核材料。")
                    payload["publication_ids"] = materials
                if body.confirm_version:
                    current = flow_detail(user, cid).get("plan")
                    if not current or current["versions"][-1]["version_id"] != body.confirm_version:
                        raise HTTPException(409, "计划版本已变化，请刷新后确认。")
                answer = re.fullmatch(r"(?:提交答案|这是我的练习答案|我的答案)[：:]([\s\S]+)", text)
                if answer:
                    payload["submission"] = {
                        "response": _ASSESS_SUFFIX.sub("", answer.group(1)).strip(),
                        "external_help": "unknown",
                    }
                if text in {
                    "确认阅读完成，进入下一模块",
                    "接受材料缺口并确认阅读完成，进入下一模块",
                }:
                    flow = flow_detail(user, cid)
                    teaching = flow.get("teaching")
                    if not teaching or not teaching.get("delivery"):
                        raise HTTPException(409, "请先完成当前模块的源码讲解。")
                    m, d = teaching["module"], teaching["delivery"]
                    payload["module_ack"] = {
                        "learning_session_id": teaching["learning_session_id"],
                        "turn_id": teaching["turn_id"],
                        "namespace_id": user["namespace_id"],
                        "plan_id": m["plan_id"],
                        "version_id": m["version_id"],
                        "module_id": m["module_id"],
                        "expected_plan_revision": m["plan_revision"],
                        "expected_profile_revision": teaching["current_profile_revision"],
                        "binding_snapshot_hash": teaching["session_tutor_binding"]["snapshot_hash"],
                        "completion_assessment_hash": d["completion_assessment_hash"],
                        "delivery_hash": d["delivery_hash"],
                        "confirm": "ACKNOWLEDGE_MODULE_AND_ADVANCE",
                        "accept_incomplete_material": text.startswith("接受材料缺口"),
                        "attempt_id": flow.get("attempt", {}).get("attempt_id"),
                        "idempotency_key": "web-ack:" + body.request_id,
                    }
                result = await call(user, "POST", f"/api/v1/conversations/{cid}/messages", payload)
                return {
                    **result,
                    "conversation_id": cid,
                    "question": body.question,
                    "agent": "workflow" if known or override or typed else "tutor",
                    "run_id": result.get("run_id") or next(iter(result.get("run_refs", [])), None),
                }

            return await start_job(
                user,
                "message:" + body.request_id,
                operation,
                body.use_model,
                fingerprint(body.model_dump()),
                call_cap=32
                if re.fullmatch(
                    r"确认目标|确认岗位要求|开始研究这份岗位要求|/build|搜项目并入库|搜索项目并入库|/projects|补证重试",
                    body.question.strip(),
                )
                else 40,
            )

    @app.get("/desk/runs")
    async def runs(user=Depends(identity)):
        result = await call(user, "GET", "/api/v1/runs")
        for row in result:
            job = next(
                (
                    j
                    for j in jobs.values()
                    if j.get("run_id") == row["id"] and j["actor"] == user["id"]
                ),
                None,
            )
            row["from_desk"] = job is not None
            if job:
                row["desk_status"] = job["status"]
            with service.db.read() as db:
                receipts = db.scalars(
                    select(ModelReceipt).where(ModelReceipt.run_id == row["id"])
                ).all()
                row["calls"] = len(receipts)
                row["roles"] = [{"role": r.role, "status": r.status} for r in receipts]
        return result

    @app.get("/desk/runs/{identifier}")
    async def run_detail(identifier: str, user=Depends(identity)):
        return await call(user, "GET", "/api/v1/runs/" + identifier)

    @app.post("/desk/plans")
    async def propose(body: dict, user=Depends(identity)):
        return await call(user, "POST", "/api/v1/plans", body)

    @app.post("/desk/plans/{identifier}/accept")
    async def accept(identifier: str, body: dict, user=Depends(identity)):
        return await call(user, "POST", f"/api/v1/plans/{identifier}/accept", body)

    @app.post("/desk/observations")
    async def observe(body: dict, user=Depends(identity)):
        return await call(user, "POST", "/api/v1/learning/observations", body)

    @app.get("/desk/goals")
    def goals(user=Depends(identity)):
        with service.db.read() as db:
            ids = list(
                db.scalars(
                    select(ObjectRow.id).where(
                        ObjectRow.kind == "jd_revision",
                        ObjectRow.namespace_id == user["namespace_id"],
                        ObjectRow.state == "active",
                    )
                )
            )
            versions = []
            for goal in db.scalars(
                select(CareerGoal).where(
                    CareerGoal.principal_id == user["id"],
                    CareerGoal.namespace_id == user["namespace_id"],
                )
            ):
                for version in db.scalars(
                    select(GoalVersion)
                    .where(GoalVersion.goal_id == goal.id)
                    .order_by(GoalVersion.version.desc())
                    .limit(1)
                ):
                    versions.append(
                        {
                            "title": goal.title,
                            "version_id": version.id,
                            "requirements": version.requirements,
                        }
                    )
        return {"documents": [service.jd.read(user["id"], i) for i in ids], "goals": versions}

    @app.post("/desk/goals")
    async def import_goal(body: dict, user=Depends(identity)):
        return await call(
            user,
            "POST",
            "/api/v1/jds",
            {
                "namespace_id": user["namespace_id"],
                "jd_id": body.get("request_id", str(uuid4())),
                "job_title": body.get("title"),
                "raw_text": body.get("text", ""),
            },
        )

    @app.post("/desk/goals/{identifier}/extract", status_code=202)
    async def extract(identifier: str, body: dict, user=Depends(identity)):
        if body.get("confirm") != "AUTHORIZE":
            raise HTTPException(422, "请确认使用模型提取岗位要求。")
        async with admission:

            async def operation(funding_id):
                return await call(
                    user,
                    "POST",
                    "/api/v1/jds/extractions",
                    {
                        "namespace_id": user["namespace_id"],
                        "funding_id": funding_id,
                        "jd_revision_id": identifier,
                        "idempotency_key": "web-extract:" + body["request_id"],
                        "max_attempts": 3,
                    },
                )

            return await start_job(
                user,
                "extract:" + body["request_id"],
                operation,
                True,
                fingerprint({"jd": identifier, **body}),
            )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app
