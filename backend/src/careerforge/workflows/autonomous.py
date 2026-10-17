"""Replayable model-led delegation on the existing outbox/lease/receipt kernel."""

import asyncio
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from careerforge.application import autonomous_service as service
from careerforge.application import delegation_service as delegation
from careerforge.application import repository_service as repositories
from careerforge.application import study_service as study
from careerforge.application.common import fingerprint
from careerforge.application.job_agent_service import strict_schema
from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.domain.runs import TERMINAL, LeaseLost
from careerforge.infrastructure.db.models import AgentRun, RepositoryFile, StageArtifact
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.llm.provider import ModelError
from careerforge.schemas.delegation import DelegateArgs, DelegationOutput, TaskOutput
from careerforge.schemas.model_io import ModelResult
from careerforge.workflows.code_investigation import investigate
from careerforge.workflows.study import RULES, Runner

PARENT_RULES = """你是有界任务协调 Agent。围绕用户问题自主判断是否需要子任务。
选择角色、缩小机制范围并分配调用。
调查决策阶段使用 delegate_task，单轮最多两个、总共最多两个、一层；简单问题不强制委派。
phase=final 时禁止工具，只输出符合 schema 的合法 JSON 对象，不能包含 Markdown 或解释前后缀。
角色：requirement_analysis 分析岗位原文；code_investigation 调查单个固定仓库；
evidence_review 查找反证/独立复核；learning_design 给出有来源的学习设计建议。
只选择授权的 requirement_keys、candidate_id、allowed_roles。需求分析不授予代码访问。
总预算包括你的决策、子任务及最终汇总；必须留下最后一次汇总额度。子任务1次为有界初读+报告，2–3次可自主补读。
子任务结果是未独立核验的模型解释；只引用 evidence_id 对应原文。
不把摘要、工具错误或失败提升成事实。
最终只给 DelegationOutput：claims 引用已返回的 evidence_id。
summary 用于组织结论，未知与反证明确列出。
不能执行命令、生成或应用补丁、批准付费重试、改变计划/人工验收、宣称用户已掌握或项目已入库。
"""
TASK_RULES = {
    "requirement_analysis": "分析给出的岗位原文与技术目标；显式要求与推断分开，无个人能力推断。",
    "code_investigation": "查固定源码的机制、符号、相关实现与测试；静态关系不证明运行行为。",
    "evidence_review": "独立复核原文支持和边界，主动寻找反例、依赖提及或不支持的目标；保留未知。",
    "learning_design": "据目标和已读实现建议学习、迁移与验收步骤；不修改正式计划。",
}
TASK_RULE = """你是隔离的只读子任务 Agent。只围绕当前合同目标，不接收父或兄弟任务的完整对话。
最终只输出符合 TaskOutput schema 的合法 JSON 对象，不含 Markdown 或解释前后缀。
每条 finding 必须引用本次 reading 中实际出现的 ref_id 与连续唯一 quote；
目录、符号摘要、历史模型解释不能作原文。没有依据则 findings 留空并写 unresolved。
反证放 counterevidence；停止原因保留边界。引用位置成立不代表语义支持或运行效果已核验。
不能调用 delegate_task，也不能请求新权限、执行命令、写文件、修改计划或确认用户能力。
"""


def tools(max_child_calls: int) -> list[Json]:
    parameters = strict_schema(DelegateArgs.model_json_schema())
    parameters["properties"]["call_limit"]["maximum"] = max_child_calls
    return [
        {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": "提议授权范围内的只读子任务，服务端统一预留额度。",
                "parameters": parameters,
            },
        }
    ]


class DelegationRunner(Runner):
    def __init__(self, run_id: UUID, epoch: int, worker: str, manifest: Json) -> None:
        super().__init__(run_id, epoch, worker, manifest)
        self.version = (
            service.CHILD_VERSION if manifest["role"] == "delegated_task" else service.VERSION
        )

    def run(self, db: Session) -> AgentRun:
        run = leased(db, self.id, self.epoch, self.worker)
        if run.workflow_version != self.version:
            raise BusinessError(409, "WORKFLOW_VERSION_CHANGED", "委派工作流版本不一致。")
        service.validate_run(db, run)
        return run

    def instructions(self) -> str:
        return RULES + (
            PARENT_RULES
            if self.m["role"] == "delegate"
            else TASK_RULE + TASK_RULES[self.m["task_role"]]
        )

    async def ask(
        self,
        key: str,
        context: Json,
        schema: type[BaseModel] | None = None,
        tools: list[Json] | None = None,
    ) -> object:
        with self.factory.begin() as db:
            run = self.run(db)
            root = db.get(AgentRun, run.parent_run_id) if run.parent_run_id else run
            if root is None:
                raise LeaseLost()
            service.safe_to_continue(db, root)
            prior = artifact(db, run, "context:" + key, self.version)
            if prior:
                # Resume the exact frozen request; recheck live authority separately above.
                context = prior.payload["context"]
                if tools is not None:
                    tools = prior.payload.get("tools", tools)
            else:
                context = {
                    **context,
                    "context_manifest": {
                        "version": service.VERSION,
                        "task_id": str(run.id),
                        "scope_hash": run.input_manifest_hash,
                        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
                        "contract": self.m["delegation_scope"],
                        "context_policy": "scoped_evidence_handoff; no_chat_history",
                    },
                }
        return await super().ask(key, context, schema, tools)

    def pending(self, db: Session, root: AgentRun) -> bool:
        children = db.scalars(select(AgentRun).where(AgentRun.parent_run_id == root.id)).all()
        if any(c.status not in TERMINAL for c in children):
            transition(db, root, "waiting_children")
            return True
        return False

    def context(self, db: Session, root: AgentRun) -> Json:
        m = root.input_manifest
        return {
            "objective": m["objective"],
            "requirements": m["requirements"],
            "source_readings": m["source_readings"],
            "allowed_roles": m["delegation_scope"]["allowed_roles"],
            "allowed_projects": [
                {
                    "candidate_id": c["candidate_id"],
                    "name": c["candidate"]["full_name"],
                    "commit": c["candidate"]["commit"],
                }
                for c in m["candidates"]
            ],
            "budget": delegation.family_state(db, root),
            "final_call_must_remain": 1,
            "handoffs": service.handoffs(db, root),
            "decisions": [
                s.payload
                for s in db.scalars(
                    select(StageArtifact)
                    .where(
                        StageArtifact.run_id == root.id,
                        StageArtifact.stage_key.like("decision_applied:%"),
                    )
                    .order_by(StageArtifact.stage_key)
                ).all()
            ],
        }

    async def parent(self) -> None:
        for number in range(self.m["parent_call_limit"] - 1):
            with self.factory.begin() as db:
                root = self.run(db)
                previous = artifact(db, root, f"decision_applied:{number}", self.version)
                if previous:
                    if self.pending(db, root):
                        return
                    if previous.payload["finish"]:
                        break
                    continue
                ledger = delegation.family_state(db, root)
                prior_request = artifact(db, root, f"context:decision:{number}", self.version)
                if not prior_request and (
                    ledger["available_calls"] <= 2 or len(ledger["children"]) >= 2
                ):
                    break
                context = self.context(db, root)
                max_calls = max(1, min(3, ledger["available_calls"] - 2))
                context["phase"] = "decide"
                context["delegation_capacity"] = {
                    "max_child_calls_after_this_decision": max_calls,
                    "total_child_calls_after_this_decision": max(0, ledger["available_calls"] - 2),
                    "slots_remaining": 2 - len(ledger["children"]),
                    "accounting": "current_decision_and_one_final_call_already_subtracted",
                }
            response = ModelResult.model_validate(
                await self.ask(f"decision:{number}", context, tools=tools(max_calls))
            )
            if len(response.tool_calls) > 2:
                raise BusinessError(422, "DELEGATION_BATCH_LIMIT", "每轮最多两个委派提议，未执行。")
            with self.factory.begin() as db:
                root = self.run(db)
                observations = []
                for tool in response.tool_calls:
                    try:
                        if tool.name != "delegate_task":
                            raise BusinessError(422, "TOOL_FORBIDDEN", "不允许该工具。")
                        proposal = DelegateArgs.model_validate(tool.arguments)
                        manifest = service.child_manifest(root, proposal)
                        child = delegation.create_child(
                            db,
                            root,
                            proposal.task_key,
                            manifest,
                            proposal.call_limit,
                            manifest["max_output_tokens"],
                        )
                        observations.append(
                            {
                                "task_key": proposal.task_key,
                                "run_id": str(child.id),
                                "status": "accepted",
                            }
                        )
                    except (BusinessError, ValidationError) as error:
                        observations.append(
                            {
                                "tool_call_id": tool.id,
                                "status": "denied",
                                "error": getattr(error, "code", "INVALID_TASK_ARGUMENTS"),
                            }
                        )
                        event(db, root, "task.proposal_denied", observations[-1])
                commit_stage(
                    db,
                    root.id,
                    self.epoch,
                    self.worker,
                    f"decision_applied:{number}",
                    self.version,
                    {"results": observations, "finish": not response.tool_calls},
                )
                if self.pending(db, root):
                    return
            if not response.tool_calls:
                break
        with self.factory.begin() as db:
            root = self.run(db)
            context = self.context(db, root)
        context["phase"] = "final"
        output = DelegationOutput.model_validate(await self.ask("final", context, DelegationOutput))
        with self.factory.begin() as db:
            root = self.run(db)
            row = study.track(db, root.owner_id, UUID(self.m["track_id"]), True)
            results = service.handoffs(db, root)
            evidence = {e["evidence_id"]: e for result in results for e in result["evidence"]}
            refs = {ref for claim in output.claims for ref in claim.evidence_ids}
            if not refs <= evidence.keys():
                raise ValueError("Parent claims referenced unknown evidence")
            service.recheck_evidence(db, root, list(evidence.values()))
            for point in evidence.values():
                if point["kind"] == "code":
                    citation = repositories.code_citation(
                        db,
                        root.owner_id,
                        UUID(point["repository_snapshot_id"]),
                        point["path"],
                        point["start_line"],
                        point["end_line"],
                    )
                    point["citation_id"] = str(citation.id)
            value = {
                **output.model_dump(mode="json"),
                "requirements_id": self.m["requirements_id"],
                "objective": self.m["objective"],
                "evidence": list(evidence.values()),
                "handoffs": results,
                "unresolved": list(
                    dict.fromkeys(
                        [*output.unresolved, *[q for r in results for q in r["unresolved"]]]
                    )
                ),
                "counterevidence": list(
                    dict.fromkeys(
                        [
                            *output.counterevidence,
                            *[q for r in results for q in r["counterevidence"]],
                        ]
                    )
                ),
                "budget": delegation.family_state(db, root),
                "synthetic": self.synthetic or any(r.get("synthetic") for r in results),
                "verification": "location_checked; semantics_unverified; not_executed",
                "competence_verified": False,
            }
            item = study.put(db, row, "delegation", value, root.id)
            row.revision += 1
            commit_stage(
                db,
                root.id,
                self.epoch,
                self.worker,
                "result",
                self.version,
                {"study_artifact_id": str(item.id), "kind": "delegation"},
            )
            event(
                db, root, "task.joined", {"artifact_id": str(item.id), "child_count": len(results)}
            )
            transition(
                db,
                root,
                "succeeded"
                if output.claims and all(r["status"] == "succeeded" for r in results)
                else "partially_succeeded",
            )

    async def child(self) -> None:
        m = self.m
        readings = list(m["source_readings"])
        investigation = None
        candidate = m["candidate"]
        if candidate:
            with self.factory.begin() as db:
                run = self.run(db)
                files = {
                    f.path: f.content
                    for f in db.scalars(
                        select(RepositoryFile).where(
                            RepositoryFile.snapshot_id == UUID(candidate["repository_snapshot_id"])
                        )
                    )
                }

            async def decide(key: str, context: Json, definitions: list[Json]) -> ModelResult:
                names = set(m["delegation_scope"]["allowed_tools"])
                if any(t["function"]["name"] not in names for t in definitions):
                    raise BusinessError(422, "TOOL_FORBIDDEN", "子任务工具超出合同。")
                return ModelResult.model_validate(
                    await self.ask(key, {**context, "objective": m["objective"]}, tools=definitions)
                )

            investigation = await investigate(
                files, m["requirements"], m["model_call_limit"], decide
            )
            for span in investigation["code"]:
                readings.append(
                    {
                        **span,
                        "kind": "code",
                        "repository_snapshot_id": candidate["repository_snapshot_id"],
                        "commit": candidate["commit"],
                        "ref_id": "code-"
                        + fingerprint(
                            {
                                "span": [
                                    candidate["repository_snapshot_id"],
                                    span["path"],
                                    span["start_line"],
                                    span["end_line"],
                                    span["content_hash"],
                                ]
                            }
                        )[:24],
                    }
                )
        self.save_stage("reading", {"sources": readings, "investigation": investigation})
        output = TaskOutput.model_validate(
            await self.ask(
                "task_final",
                {
                    "objective": m["objective"],
                    "requirements": m["requirements"],
                    "reading": readings,
                    "investigation_stop": investigation["investigation"]["stop_reason"]
                    if investigation
                    else "no_code_tools",
                },
                TaskOutput,
            )
        )
        with self.factory.begin() as db:
            run = self.run(db)
            value = service.checked_output(run, output, readings)
            value.update(
                {
                    "synthetic": self.synthetic,
                    "system_stop_reason": investigation["investigation"]["stop_reason"]
                    if investigation
                    else "single_report",
                    "reading_stage": "reading",
                }
            )
            commit_stage(db, run.id, self.epoch, self.worker, "result", self.version, value)
            event(
                db,
                run,
                "task.result_ready",
                {"published": False, "findings": len(value["findings"])},
            )
            transition(db, run, "succeeded" if value["findings"] else "partially_succeeded")


def execute(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
        row = db.get(AgentRun, run_id)
        manifest = row.input_manifest if epoch is not None and row is not None else None
    if epoch is None or manifest is None:
        return
    runner = DelegationRunner(run_id, epoch, worker, manifest)
    try:
        asyncio.run(runner.child() if manifest["role"] == "delegated_task" else runner.parent())
    except LeaseLost:
        return
    except (BusinessError, ModelError, ValueError, KeyError) as error:
        with factory.begin() as db:
            try:
                run = leased(db, run_id, epoch, worker)
            except LeaseLost:
                return
            event(
                db,
                run,
                "stage.failed",
                {
                    "error": getattr(error, "code", "INVALID_DELEGATION_RESULT"),
                    "automatic_retry": False,
                },
            )
            transition(db, run, "failed")
