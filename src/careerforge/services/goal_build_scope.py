"""Server-frozen evidence task for one user objective, never a new JD requirement."""

from copy import deepcopy
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ..contracts import Contract
from ..contracts_goal_amendment_v1 import Hash, Identifier, ObjectiveMapping
from ..contracts_research_v163 import ResearchDimension, ResearchTaskContract
from ..errors import DomainError
from ..models_dialogue import WorkflowExecution, WorkflowSession
from ..util import fingerprint
from .sources import SourceService


class ScopeSource(Contract):
    source_id: Identifier
    scope_hash: Hash
    commit_sha: str | None
    repository_key: str


class GoalBuildScope(Contract):
    contract: Literal["goal-build-scope-v1"] = "goal-build-scope-v1"
    scope_mode: Literal["user_objective"] = "user_objective"
    workflow_id: Identifier
    goal_version_id: Identifier
    version_hash: Hash
    user_objective_id: str = Field(pattern=r"^uobj-[0-9a-f]{24}$")
    employer_requirement_ids: list[str] = Field(default_factory=list, max_length=100)
    unresolved_requirement_ids: list[str] = Field(default_factory=list, max_length=100)
    topic_refs: list[ObjectiveMapping] = Field(min_length=1, max_length=1)
    source_selection_mode: Literal["fixed", "repository", "search"]
    source_ids: list[Identifier] = Field(default_factory=list, max_length=2)
    source_restrictions: list[ScopeSource] = Field(default_factory=list, max_length=2)
    repository_url: str | None = Field(default=None, max_length=500)
    original_question: str = Field(min_length=1, max_length=6000)
    question_hash: Hash
    research_scope: Literal["focused", "project", "concept"] = "focused"
    required_dimensions: list[ResearchDimension] = Field(default_factory=list, max_length=20)
    scope_hash: Hash

    @model_validator(mode="after")
    def consistent(self):
        if any(
            len(items) != len(set(items))
            for items in (
                self.employer_requirement_ids,
                self.unresolved_requirement_ids,
                self.source_ids,
                self.required_dimensions,
            )
        ):
            raise ValueError("Duplicate immutable selectors")
        expected_mode = (
            "fixed" if self.source_ids else "repository" if self.repository_url else "search"
        )
        if self.source_selection_mode != expected_mode or (self.source_ids and self.repository_url):
            raise ValueError("Inconsistent source selection")
        if [item.source_id for item in self.source_restrictions] != self.source_ids:
            raise ValueError("Source restrictions omitted")
        if self.repository_url and not self.repository_url.startswith("https://github.com/"):
            raise ValueError("Repository admission requires GitHub URL")
        if (
            fingerprint(self.original_question) != self.question_hash
            or fingerprint(self.model_dump(mode="json", exclude={"scope_hash"})) != self.scope_hash
        ):
            raise ValueError("Scope or original question changed")
        return self


def _fail(message="用户新增学习目标的构建范围已变化或不完整。"):
    raise DomainError("GOAL_BUILD_SCOPE", message, 409)


def _source(db, actor, source_id):
    source = SourceService.get(db, actor, source_id)
    return {
        "source_id": source_id,
        "scope_hash": source.scope_hash,
        "commit_sha": source.commit_sha,
        "repository_key": source.repository_key,
    }


def _goal(db, actor, workflow):
    from .research_coverage import read_goal_version

    if workflow is None or workflow.principal_id != actor or not workflow.goal_version_id:
        _fail()
    snapshot = read_goal_version(db, actor, workflow.goal_version_id)
    if (
        snapshot.get("origin_contract") != "command-goal-v1"
        or snapshot["namespace_id"] != workflow.namespace_id
        or snapshot["goal_addendum_id"] != workflow.target.get("goal_addendum_id")
    ):
        _fail()
    return snapshot


def freeze_goal_build_scope_in(
    db,
    actor,
    workflow,
    *,
    objective_id=None,
    employer_requirement_ids=(),
    source_ids=(),
    repository_url=None,
    original_question=None,
    research_scope="focused",
    required_dimensions=None,
):
    from .research_coverage import DEPTH_DIMENSIONS, requirement_identity

    snapshot = _goal(db, actor, workflow)
    objectives = snapshot["user_learning_objectives"]
    objective_id = objective_id or objectives[-1]["objective_id"]
    matches = [
        item
        for item in objectives
        if item["objective_id"] == objective_id and item["origin"] == "user_learning_addition"
    ]
    if len(matches) != 1:
        _fail()
    objective = matches[0]
    if objective["mapping"]["status"] != "mapped":
        raise DomainError("GOAL_MAPPING_PENDING", "新增目标尚未映射到可访问的明确技术主题。", 409)
    requirements = {requirement_identity(item): item for item in snapshot["requirements"]}
    if any(key not in requirements for key in employer_requirement_ids):
        _fail()
    dimensions = list(
        dict.fromkeys(
            [
                *DEPTH_DIMENSIONS.get(objective["target_depth"] or "", ()),
                *(required_dimensions or []),
            ]
        )
    )
    question = original_question or (
        "用户新增学习目标："
        + objective["technology"]
        + (
            "；期望深度：" + objective["target_depth"]
            if objective["target_depth"]
            else "；期望深度未指定"
        )
        + "。仅研究此学习目标的已审核材料，不代表岗位要求或已经掌握。"
    )
    if objective["technology"].casefold() not in question.casefold():
        _fail("研究问题必须明确包含所选用户学习目标。")
    value = {
        "contract": "goal-build-scope-v1",
        "scope_mode": "user_objective",
        "workflow_id": workflow.id,
        "goal_version_id": snapshot["goal_version_id"],
        "version_hash": snapshot["version_hash"],
        "user_objective_id": objective_id,
        "employer_requirement_ids": list(employer_requirement_ids),
        "unresolved_requirement_ids": [],
        "topic_refs": [deepcopy(objective["mapping"])],
        "source_selection_mode": "fixed"
        if source_ids
        else "repository"
        if repository_url
        else "search",
        "source_ids": list(source_ids),
        "source_restrictions": [_source(db, actor, item) for item in source_ids],
        "repository_url": repository_url,
        "original_question": question,
        "question_hash": fingerprint(question),
        "research_scope": research_scope,
        "required_dimensions": dimensions,
    }
    value["scope_hash"] = fingerprint(value)
    return validate_goal_build_scope_in(db, actor, value, goal_snapshot=snapshot)


def validate_goal_build_scope_in(db, actor, scope, *, goal_snapshot=None):
    from .research_coverage import DEPTH_DIMENSIONS, requirement_identity

    try:
        typed = GoalBuildScope.model_validate(scope)
        value = typed.model_dump(mode="json")
        if scope != value:
            _fail()
    except (ValidationError, TypeError, ValueError) as exc:
        raise DomainError("GOAL_BUILD_SCOPE", "新增目标构建范围合同无效。", 409) from exc
    workflow = db.get(WorkflowSession, typed.workflow_id)
    snapshot = _goal(db, actor, workflow)
    if workflow.current_execution_id:
        execution = db.get(WorkflowExecution, workflow.current_execution_id)
        if (
            execution is None
            or execution.workflow_id != workflow.id
            or execution.principal_id != actor
            or execution.goal_version_id != snapshot["goal_version_id"]
            or execution.goal_binding_hash != fingerprint(execution.binding_payload)
            or execution.binding_payload.get("admitted_scope", {}).get("goal_build_scope") != value
        ):
            _fail("当前执行与目标构建范围不一致。")
    if goal_snapshot is not None and goal_snapshot != snapshot:
        _fail()
    matches = [
        item
        for item in snapshot["user_learning_objectives"]
        if item["objective_id"] == typed.user_objective_id
        and item["origin"] == "user_learning_addition"
    ]
    if len(matches) != 1 or (typed.goal_version_id, typed.version_hash) != (
        snapshot["goal_version_id"],
        snapshot["version_hash"],
    ):
        _fail()
    objective = matches[0]
    if (
        objective["mapping"]["status"] != "mapped"
        or value["topic_refs"] != [objective["mapping"]]
        or objective["technology"].casefold() not in typed.original_question.casefold()
    ):
        _fail()
    if not set(DEPTH_DIMENSIONS.get(objective["target_depth"] or "", ())) <= set(
        typed.required_dimensions
    ):
        _fail("构建范围不能丢失用户明确要求的学习维度。")
    requirements = {requirement_identity(item): item for item in snapshot["requirements"]}
    if (
        any(key not in requirements for key in typed.employer_requirement_ids)
        or typed.unresolved_requirement_ids
    ):
        _fail()
    if value["source_restrictions"] != [_source(db, actor, item) for item in typed.source_ids]:
        _fail()
    return value


def scope_requirements(snapshot, scope):
    from .research_coverage import requirement_identity

    by_id = {requirement_identity(item): item for item in snapshot["requirements"]}
    return [by_id[key] for key in scope["employer_requirement_ids"]]


def scoped_research_task_in(db, actor, scope, snapshot):
    from .research_coverage import freeze_research_task

    scope = validate_goal_build_scope_in(db, actor, scope, goal_snapshot=snapshot)
    task = freeze_research_task(
        scope["original_question"],
        scope=scope["research_scope"],
        required_dimensions=scope["required_dimensions"],
        goal_version_id=snapshot["goal_version_id"],
        requirements=scope_requirements(snapshot, scope),
        unresolved_requirement_ids=scope["unresolved_requirement_ids"],
    )
    # Only this database-validated, actual user-objective selection can have an
    # intentionally empty employer subset. The generic legacy helper is unchanged.
    if not scope["employer_requirement_ids"] and not scope["unresolved_requirement_ids"]:
        task = ResearchTaskContract.model_validate(
            {**task.model_dump(mode="json"), "unresolved_requirement_ids": []}
        )
    return task


def validate_scope_manifest_in(db, actor, scope, manifest, *, root=False):
    scope = validate_goal_build_scope_in(
        db, actor, scope, goal_snapshot=manifest.get("goal_snapshot")
    )
    workflow = db.get(WorkflowSession, scope["workflow_id"])
    if workflow is None or workflow.target.get("goal_build_scope") != scope:
        _fail("目标构建范围没有在所属会话中确认。")
    if manifest.get("goal_build_scope") != scope:
        _fail()
    if root:
        if (
            manifest.get("goal_version_id") != scope["goal_version_id"]
            or manifest.get("question") != scope["original_question"]
            or manifest.get("research_scope") != scope["research_scope"]
            or (manifest.get("required_dimensions") or []) != scope["required_dimensions"]
            or manifest.get("topic_keys") != [item["topic_key"] for item in scope["topic_refs"]]
        ):
            _fail()
        if scope["source_selection_mode"] == "fixed":
            if manifest.get("source_id") not in scope["source_ids"] or manifest.get(
                "repository_url"
            ):
                _fail()
        elif manifest.get("source_id") or manifest.get("repository_url") != scope["repository_url"]:
            _fail()
    elif scope["source_selection_mode"] == "fixed":
        ids = manifest.get("source_ids") or (
            [manifest["source_id"]] if manifest.get("source_id") else []
        )
        if not set(ids) <= set(scope["source_ids"]):
            _fail()
    return scope
