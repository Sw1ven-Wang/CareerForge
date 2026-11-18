"""Persistent question tree. Branches are logical nodes, not unbounded nested agents."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import QueryPlan
from ..errors import DomainError
from ..models import AgentRun, ExternalRequest, ResearchNode, SearchObservation
from ..util import fingerprint, uid
from .dependencies import check_closure
from .privacy import assert_personal_access
from .research_planning import (
    PLANNING_TASK_INSTRUCTIONS,
    planning_identity,
    planning_task_in,
)
from .research_query import (
    QUERY_CONTRACT_INSTRUCTIONS,
    query_contract_context,
    query_plan_type,
    validate_public_search_query,
)
from .runtime import Runtime, event, leased
from .security import namespace_access
from .sources import SourceService
from .taxonomy import topic_object_id, visible_topics


class ResearchService:
    def __init__(self, runtime: Runtime, gateway, repositories):
        self.runtime, self.gateway, self.repositories = runtime, gateway, repositories

    @staticmethod
    def _node(db: Session, node_id: str, run_id: str) -> ResearchNode:
        node = db.get(ResearchNode, node_id)
        if node is None or node.run_id != run_id:
            raise DomainError("RESEARCH_NODE", "研究节点缺失或不属于当前任务。", 409)
        return node

    async def run(
        self,
        actor: str,
        run_id: str,
        epoch: int,
        worker: str,
        question: str,
        topics: list[str],
        max_nodes: int,
        max_depth: int,
        model_cap: int,
        request_cap: int,
        policy: dict[str, Any] | None = None,
        *,
        research_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .policies import validate_policy
        from .research_policy import frontier_key

        policy = validate_policy("research_heuristic", policy or {})
        self.runtime.validate(run_id, epoch, worker)
        input_dependencies = self._topic_dependencies(actor, topics, run_id)
        with self.runtime.db.read() as db:
            active = db.get(AgentRun, run_id)
            if active is None:
                raise DomainError("NOT_FOUND", "研究任务不存在。", 404)
            task = planning_task_in(db, actor, active)
            query_context = query_contract_context(active.input_manifest)
            plan_type = query_plan_type(active.input_manifest)
            discovery = active.input_manifest.get("research_discovery_contract")
            if discovery not in (None, "repository-first-v1"):
                raise DomainError("RESEARCH_DISCOVERY_CONTRACT", "未知仓库发现合同。", 409)
        if task != research_task or (task is not None and task["original_question"] != question):
            raise DomainError(
                "RESEARCH_PLANNING_CONTRACT", "检索输入与已授权的固定研究任务不一致。", 409
            )
        task_identity = planning_identity(task) if task is not None else {}
        if query_context:
            task_identity["research_query_contract"] = query_context["query_contract"]["version"]
        prompt_context = (
            {**task_identity, "research_task": task, **query_context} if task is not None else {}
        )
        batch = active.input_manifest.get("jd_research_batch")
        if batch:
            prompt_context["learning_language_policy"] = batch["language_profile"]
            prompt_context["language_scope_rule"] = (
                "架构与编排原理资料不限语言；具体实现遵循 learning_language_policy，AI/Agent 必学 Python，后端使用 backend_languages。"
                "只有检索具体实现时使用对应语言过滤，不能把原理资料一律限制为某语言。"
            )
        task_classes = []
        if task is not None and task.get("goal_version_id"):
            input_dependencies = list(dict.fromkeys([*input_dependencies, task["goal_version_id"]]))
            task_classes = ["private_jd"]
        saved = self.runtime.stage(run_id, "research.result")
        if saved:
            if task_identity and any(
                saved.get(key) != value for key, value in task_identity.items()
            ):
                raise DomainError("RESEARCH_PLANNING_CONTRACT", "检索结果不是当前固定任务。", 409)
            with self.runtime.db.transaction() as db:
                active = leased(db, run_id, epoch, worker)
                if active.principal_id != actor:
                    raise DomainError("RESEARCH_AUTHORITY", "研究身份与任务不一致。", 403)
                self._check_observation(db, actor, saved, input_dependencies)
                return saved
        if model_cap < 1:
            raise DomainError("RESEARCH_BUDGET", "必须为搜索规划与后续审核保留额度。")
        with self.runtime.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            if run.principal_id != actor:
                raise DomainError("RESEARCH_AUTHORITY", "研究身份与任务不一致。", 403)
            check_closure(db, actor, input_dependencies)
            root = db.scalar(
                select(ResearchNode).where(
                    ResearchNode.run_id == run_id, ResearchNode.parent_id.is_(None)
                )
            )
            if (
                root
                and task_identity
                and (
                    any(root.payload.get(key) != value for key, value in task_identity.items())
                    or root.node_key
                    != fingerprint({"question": question, "topics": topics, **task_identity})
                )
            ):
                raise DomainError("RESEARCH_PLANNING_CONTRACT", "检索树与固定任务不一致。", 409)
            if not root:
                root = ResearchNode(
                    run_id=run_id,
                    parent_id=None,
                    depth=0,
                    question=question,
                    node_key=fingerprint({"question": question, "topics": topics, **task_identity}),
                    payload={"topics": topics, **task_identity},
                )
                db.add(root)
                db.flush()
            root_id = root.id
        # One shared, fixed budget for planning AND reflection; tree nodes never mint calls.
        plan_classes, plan_dependencies = self.gateway.policy_egress_inputs(
            run_id,
            "planner",
            data_classes=[*self._data_classes(run_id), *task_classes],
            access_dependencies=input_dependencies,
        )
        plan = await self.gateway.call(
            run_id,
            epoch,
            worker,
            "research.initial_plan",
            "planner",
            "将问题分解为至多3个针对GitHub项目的不同检索问题。不要编造仓库。"
            "查询关注实现线索，不把README宣传当作源码事实。channel可为repository/code/documentation/issues/pull_requests/releases；代码类搜索需鉴权。空queries表示没有合理问题。"
            + (PLANNING_TASK_INSTRUCTIONS if task is not None else "")
            + (QUERY_CONTRACT_INSTRUCTIONS if query_context else "")
            + (
                "\n冷启动必须先发现仓库。queries 非空时第一项 channel 必须为 repository。"
                "用户明确仓库名时，用 repo:owner/name 查找元数据，不能在仓库发现查询里堆叠尚未读取的实现细节。"
                "其余项才可在已授权通道中定位代码。仓库不存在或检索无结果须如实保留。"
                if discovery
                else ""
            ),
            {
                "question": question,
                "topic_keys": topics,
                "remaining_nodes": max_nodes - 1,
                "research_heuristic": policy,
                **prompt_context,
            },
            plan_type,
            data_classes=plan_classes,
            access_dependencies=plan_dependencies,
        )
        initial = plan_type.model_validate(plan.structured)
        if discovery and initial.queries and initial.queries[0].channel != "repository":
            raise DomainError(
                "RESEARCH_DISCOVERY_CONTRACT", "冷启动首个分支必须发现公开仓库。", 422
            )
        self._expand(
            run_id,
            epoch,
            worker,
            root_id,
            initial,
            max_nodes,
            max_depth,
            [],
            access_dependencies=input_dependencies,
        )
        while True:
            self.runtime.validate(run_id, epoch, worker)
            with self.runtime.db.read() as db:
                nodes = db.scalars(
                    select(ResearchNode)
                    .where(
                        ResearchNode.run_id == run_id,
                        ResearchNode.status.in_(["pending", "running", "observed"]),
                    )
                    .order_by(ResearchNode.depth, ResearchNode.created_at, ResearchNode.id)
                ).all()
                observed = db.scalars(
                    select(SearchObservation)
                    .join(ResearchNode)
                    .where(ResearchNode.run_id == run_id)
                ).all()
                covered = set()
                for observation_row in observed:
                    if observation_row.payload.get("candidates"):
                        observed_node = self._node(db, observation_row.node_id, run_id)
                        covered.update(observed_node.payload.get("topics", []))
                pending = [n for n in nodes if n.parent_id]
                node = (
                    min(
                        pending,
                        key=lambda n: frontier_key(
                            {
                                "id": n.id,
                                "depth": n.depth,
                                "created_at": n.created_at.isoformat(),
                                "topics": n.payload.get("topics", []),
                            },
                            policy,
                            covered,
                        ),
                    )
                    if pending
                    else None
                )
                if node is None:
                    break
                nid, depth, payload, node_question = (
                    node.id,
                    node.depth,
                    dict(node.payload),
                    node.question,
                )
            if query_context:
                # Recheck persisted node inputs too, before local search or a
                # saved observation can conceal transport-incompatible syntax.
                validate_public_search_query(payload["query"], payload.get("channel", "repository"))
            observation = self.runtime.stage(run_id, "search:" + nid)
            if observation is None:
                with self.runtime.db.transaction() as db:
                    run = leased(db, run_id, epoch, worker)
                    self._node(db, nid, run_id).status = "running"
                    event(db, run, "research.node.started", {"node_id": nid, "depth": depth})
                with self.runtime.db.read() as db:
                    request_ids_before = set(
                        db.scalars(
                            select(ExternalRequest.id).where(ExternalRequest.run_id == run_id)
                        )
                    )
                try:
                    with self.runtime.db.read() as db:
                        public_only = (
                            namespace_access(db, actor, run.namespace_id).kind == "shared_public"
                        )
                    local = (
                        SourceService(self.runtime.db).search_snapshots(
                            actor, payload["query"], public_only=public_only
                        )
                        if payload.get("channel", "repository")
                        in {"repository", "code", "documentation"}
                        else None
                    )
                    if local and local["candidates"]:
                        observation = local
                    else:
                        from ..adapters.github import GitHubProvider

                        # The production adapter binds external receipts/cache to actual input scope.
                        # Synthetic provider protocols do not pretend to perform external I/O.
                        if isinstance(self.repositories, GitHubProvider):
                            observation = await self.repositories.search(
                                payload["query"],
                                run_id,
                                request_cap,
                                channel=payload.get("channel", "repository"),
                                access_dependencies=input_dependencies,
                            )
                        else:
                            observation = await self.repositories.search(
                                payload["query"],
                                run_id,
                                request_cap,
                                channel=payload.get("channel", "repository"),
                            )
                except DomainError as exc:
                    # Stop rather than inventing successes or retrying without limit.
                    observation = {
                        "candidates": [],
                        "incomplete_results": True,
                        "error_code": exc.code,
                        "stop_reason": "source_unavailable",
                    }
                with self.runtime.db.read() as db:
                    request_ids_after = set(
                        db.scalars(
                            select(ExternalRequest.id).where(ExternalRequest.run_id == run_id)
                        )
                    )
                external_ids = sorted(request_ids_after - request_ids_before)
                observation = {
                    **observation,
                    "external_request_ids": external_ids,
                    "external_request_count": len(external_ids),
                }
                observation["access_dependencies"] = list(
                    dict.fromkeys(
                        [
                            *input_dependencies,
                            *(
                                candidate["source_id"]
                                for candidate in observation.get("candidates", [])
                                if isinstance(candidate.get("source_id"), str)
                            ),
                        ]
                    )
                )
                with self.runtime.db.transaction() as db:
                    active = leased(db, run_id, epoch, worker)
                    self._check_observation(db, actor, observation, input_dependencies)
                    self.runtime.save_stage_in(db, active, "search:" + nid, observation)
            with self.runtime.db.transaction() as db:
                run = leased(db, run_id, epoch, worker)
                self._check_observation(db, actor, observation, input_dependencies)
                current = self._node(db, nid, run_id)
                current.status = "observed"
                if not db.scalar(select(SearchObservation).where(SearchObservation.node_id == nid)):
                    db.add(
                        SearchObservation(
                            node_id=nid,
                            query_hash=fingerprint(payload["query"]),
                            payload=observation,
                        )
                    )
                event(
                    db,
                    run,
                    "research.node.observed",
                    {
                        "node_id": nid,
                        "candidates": len(observation["candidates"]),
                        "incomplete": observation.get("incomplete_results", False),
                    },
                )
            with self.runtime.db.read() as db:
                reflections = db.scalars(
                    select(ResearchNode).where(ResearchNode.run_id == run_id)
                ).all()
                used_reflections = sum(bool(n.payload.get("reflected")) for n in reflections)
                count = len(reflections)
                known_repos = {
                    str(c.get("repository_id", c.get("name")))
                    for o in db.scalars(
                        select(SearchObservation)
                        .join(ResearchNode)
                        .where(ResearchNode.run_id == run_id)
                    )
                    for c in o.payload.get("candidates", [])
                }
            # Failed/empty searches are observations, not invitations to recursive paid guessing.
            can_expand = (
                observation["candidates"]
                and not observation.get("error_code")
                and depth < max_depth
                and count < max_nodes
                and 1 + used_reflections < model_cap
            )
            if can_expand:
                reflection_classes, reflection_dependencies = self.gateway.policy_egress_inputs(
                    run_id,
                    "reflector",
                    data_classes=[*self._data_classes(run_id), *task_classes],
                    access_dependencies=list(
                        dict.fromkeys(
                            [
                                *input_dependencies,
                                *(
                                    candidate["source_id"]
                                    for candidate in observation.get("candidates", [])
                                    if isinstance(candidate.get("source_id"), str)
                                ),
                            ]
                        )
                    ),
                )
                reflection = await self.gateway.call(
                    run_id,
                    epoch,
                    worker,
                    "reflect:" + nid,
                    "reflector",
                    "基于实际检索观察寻找尚未回答的具体问题。只提出有新线索的检索，最多3个。"
                    "不要重复原问题；无需扩展返回空queries及停止理由。不生成项目技术结论。"
                    + (PLANNING_TASK_INSTRUCTIONS if task is not None else "")
                    + (QUERY_CONTRACT_INSTRUCTIONS if query_context else ""),
                    {
                        "root_question": question,
                        "branch_question": node_question,
                        "observations": observation,
                        "known_repository_count": len(known_repos),
                        "topic_keys": topics,
                        "remaining_nodes": max_nodes - count,
                        "research_heuristic": policy,
                        **prompt_context,
                    },
                    plan_type,
                    data_classes=reflection_classes,
                    access_dependencies=reflection_dependencies,
                )
                self._expand(
                    run_id,
                    epoch,
                    worker,
                    nid,
                    plan_type.model_validate(reflection.structured),
                    max_nodes,
                    max_depth,
                    [nid],
                    access_dependencies=observation.get("access_dependencies", input_dependencies),
                )
            else:
                with self.runtime.db.transaction() as db:
                    leased(db, run_id, epoch, worker)
                    n = self._node(db, nid, run_id)
                    n.status = "closed"
                    n.payload = {
                        **n.payload,
                        "stop_reason": observation.get("error_code")
                        or "bounded_or_no_new_observation",
                    }
        with self.runtime.db.read() as db:
            observations = db.scalars(
                select(SearchObservation).join(ResearchNode).where(ResearchNode.run_id == run_id)
            ).all()
            candidates: dict[str, dict[str, Any]] = {}
            for o in observations:
                for candidate in o.payload.get("candidates", []):
                    identity = str(candidate.get("repository_id", candidate.get("name")))
                    if identity not in candidates:
                        candidates[identity] = {
                            **candidate,
                            "discovery_nodes": [],
                            "verified_implementation": False,
                        }
                    candidates[identity]["discovery_nodes"].append(o.node_id)
            tree = [
                {
                    "id": n.id,
                    "parent_id": n.parent_id,
                    "depth": n.depth,
                    "question": n.question,
                    "status": n.status,
                    "payload": n.payload,
                }
                for n in db.scalars(
                    select(ResearchNode)
                    .where(ResearchNode.run_id == run_id)
                    .order_by(ResearchNode.depth, ResearchNode.id)
                )
            ]
        result = {
            **task_identity,
            "candidates": list(candidates.values()),
            "tree": tree,
            "incomplete": any(o.payload.get("incomplete_results") for o in observations),
            "policy": policy,
            "stop_reason": "frontier_exhausted_or_budget_boundary",
            "synthetic": self.repositories.synthetic,
            "access_dependencies": sorted(
                set(input_dependencies).union(
                    *(set(o.payload.get("access_dependencies", [])) for o in observations)
                )
            ),
        }
        with self.runtime.db.transaction() as db:
            active = leased(db, run_id, epoch, worker)
            for observation_row in observations:
                self._check_observation(db, actor, observation_row.payload, input_dependencies)
            return self.runtime.save_stage_in(db, active, "research.result", result)

    def _expand(
        self,
        run_id: str,
        epoch: int,
        worker: str,
        parent_id: str,
        plan: QueryPlan,
        max_nodes: int,
        max_depth: int,
        derived: list[str],
        access_dependencies: list[str] | None = None,
    ) -> None:
        with self.runtime.db.transaction() as db:
            run = leased(db, run_id, epoch, worker)
            check_closure(db, run.principal_id, access_dependencies or [])
            if query_contract_context(run.input_manifest):
                # The paid response remains intact in its receipt. Reject before
                # adding/dispatching any invalid branch; no rewrite or extra call.
                for query in plan.queries:
                    validate_public_search_query(query.query, query.channel)
            parent = self._node(db, parent_id, run_id)
            if parent.status in {"expanded", "closed"}:
                return
            count = len(
                db.scalars(select(ResearchNode.id).where(ResearchNode.run_id == run_id)).all()
            )
            added = 0
            if parent.depth < max_depth:
                for query in plan.queries:
                    key = fingerprint(
                        {
                            "q": query.query.lower().strip(),
                            "topics": sorted(query.topic_keys),
                            "channel": query.channel,
                        }
                    )
                    existing = db.scalar(
                        select(ResearchNode).where(
                            ResearchNode.run_id == run_id, ResearchNode.node_key == key
                        )
                    )
                    if existing or count >= max_nodes:
                        continue
                    db.add(
                        ResearchNode(
                            id=uid(),
                            run_id=run_id,
                            parent_id=parent_id,
                            depth=parent.depth + 1,
                            question=query.question,
                            node_key=key,
                            payload={
                                "query": query.query,
                                "topics": query.topic_keys,
                                "channel": query.channel,
                                "derived_observations": derived,
                            },
                        )
                    )
                    count += 1
                    added += 1
            parent.status = "expanded" if added else "closed"
            parent.payload = {
                **parent.payload,
                "reflected": bool(parent.parent_id),
                "stop_reason": plan.stop_reason or ("expanded" if added else "no_new_information"),
            }
            event(db, run, "research.node.expanded", {"node_id": parent_id, "added": added})

    @staticmethod
    def _check_observation(
        db: Session, actor: str, observation: dict[str, Any], input_dependencies: list[str]
    ) -> None:
        dependencies = list(
            dict.fromkeys(
                [
                    *input_dependencies,
                    *observation.get("access_dependencies", []),
                    *(
                        candidate["source_id"]
                        for candidate in observation.get("candidates", [])
                        if isinstance(candidate.get("source_id"), str)
                    ),
                ]
            )
        )
        check_closure(db, actor, dependencies)
        for owner, generation in observation.get("privacy_generations", {}).items():
            assert_personal_access(db, owner, expected_generation=generation)

    def _topic_dependencies(self, actor: str, keys: list[str], run_id: str) -> list[str]:
        public_only = self._data_classes(run_id) == ["public_source"]
        with self.runtime.db.read() as db:
            return [
                topic_object_id(topic.key, topic.version)
                for topic in visible_topics(db, None if public_only else actor)
                if topic.key in keys
            ]

    def _data_classes(self, run_id: str) -> list[str]:
        from ..models import Namespace

        with self.runtime.db.read() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise DomainError("NOT_FOUND", "研究任务不存在。", 404)
            namespace = db.get(Namespace, run.namespace_id)
            if namespace is None:
                raise DomainError("NOT_FOUND", "研究任务知识域不存在。", 404)
            return (
                ["public_source"]
                if namespace.kind == "shared_public"
                else ["learning_state", "private_source"]
            )
