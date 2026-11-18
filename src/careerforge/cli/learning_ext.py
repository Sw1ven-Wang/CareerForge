"""Thin learning/replay CLI. JSON files carry explicit scope, funding and revisions.

No namespace, payment consent, reviewer identity or idempotency key is invented by
this client. Server-side contracts remain the authority.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ValidationError

from ..contracts_learning_events_v166 import (
    LearningIntentInput,
    NavigationAckInput,
    PreferenceEventInput,
)
from ..contracts_learning_ext import (
    ActivityInput,
    AdjustmentInput,
    AttemptInput,
    AttemptSubmission,
    DatasetAttestationInput,
    ObservationInput,
    PersonalizedPlanInput,
    ProfileUpdate,
    ReplayDatasetInput,
    ReplayInput,
    ResearchReplayDatasetInput,
    TraceCaptureInput,
    TypedReplayDatasetInput,
)
from ..contracts_retrieval_replay import RetrievalR3ReplayDatasetInput
from ..contracts_session_plan_v166 import SessionPlanInput
from ..contracts_session_progress_v166 import SessionModuleAckInput
from ..contracts_session_tutor_v166 import SessionTutorTurnInput


def load_contract(path: Path, schema: type[BaseModel]) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            raise typer.BadParameter("需要不超过2MB的明确JSON输入文件。")
        return schema.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")
    except (OSError, UnicodeError, ValidationError):
        # Pydantic input diagnostics can contain private learner/source content.
        raise typer.BadParameter("JSON文件不可读或不符合该操作合同；未发送请求。") from None


def register_cli(
    app: typer.Typer, request: Callable[..., Any], show: Callable[[Any], None]
) -> None:
    learning = typer.Typer(no_args_is_help=True, help="私人学习计划、冻结Rubric、观察与可选复习")
    replay = typer.Typer(no_args_is_help=True, help="同输入同预算实际执行；不是模型自评或自动发布")
    app.add_typer(learning, name="learning")
    app.add_typer(replay, name="replay")

    @learning.command("session-tutor")
    def session_tutor(conversation: str, file: Path):
        """逐次显式绑定已采纳单次计划；不改长期偏好或自动完成模块。"""
        from .integration_v166 import _identifier

        show(
            request(
                "POST",
                f"/api/v1/conversations/{_identifier(conversation)}/session-tutor-turns",
                load_contract(file, SessionTutorTurnInput),
            )
        )

    @learning.command("session-tutor-binding")
    def session_tutor_binding(turn: str):
        """只读查看固定模块、有效偏好及其来源；不推进学习状态。"""
        from .integration_v166 import _identifier

        show(request("GET", f"/api/v1/turns/{_identifier(turn)}/session-tutor-binding"))

    @learning.command("teaching-session")
    def teaching_session(identifier: str):
        """查看真实教学会话、最终交付与尚未确认的模块状态。"""
        from .integration_v166 import _identifier

        show(request("GET", f"/api/v1/learning/teaching-sessions/{_identifier(identifier)}"))

    @learning.command("module-ack")
    def module_ack(file: Path):
        """本人显式确认并推进模块；不足须明确接受，不代表掌握。"""
        show(
            request(
                "POST",
                "/api/v1/learning/session-module-acks",
                load_contract(file, SessionModuleAckInput),
            )
        )

    @learning.command("profile")
    def profile():
        show(request("GET", "/api/v1/learning/profile"))

    @learning.command("profile-update")
    def profile_update(file: Path):
        """文件须含expected_revision与完整preferences；关闭检查不等于已掌握。"""
        show(request("PUT", "/api/v1/learning/profile", load_contract(file, ProfileUpdate)))

    @learning.command("intent")
    def learning_intent(file: Path):
        """只读解析当前问题与可选会话焦点；不要求用户选择检索模式。"""
        show(request("POST", "/api/v1/learning/intent", load_contract(file, LearningIntentInput)))

    @learning.command("preference-events")
    def preference_events():
        show(request("GET", "/api/v1/learning/preference-events"))

    @learning.command("preference-event")
    def preference_event(file: Path):
        """显式提交有期限、作用域和版本的偏好；永久偏好需durable_consent。"""
        show(
            request(
                "POST",
                "/api/v1/learning/preference-events",
                load_contract(file, PreferenceEventInput),
            )
        )

    @learning.command("navigation")
    def navigation(block_id: str):
        """只读服务器位置及客户端确认状态；查看不会自动生成ACK。"""
        show(request("GET", f"/api/v1/learning/navigation/{block_id}"))

    @learning.command("navigation-ack")
    def navigation_ack(file: Path):
        """显式上报客户端确认；服务器不会把请求或成功定位当作高亮回执。"""
        show(
            request(
                "POST", "/api/v1/learning/navigation-acks", load_contract(file, NavigationAckInput)
            )
        )

    @learning.command("propose")
    def propose(file: Path):
        """显式namespace/funding/publication/revision/idempotency；仅生成待采纳提议。"""
        show(
            request(
                "POST",
                "/api/v1/learning/plan-proposals",
                load_contract(file, PersonalizedPlanInput),
            )
        )

    @learning.command("session-propose")
    def session_propose(file: Path):
        """有总时长上限的单次学习草案；空资料返回缺口，不进行模型调用。"""
        show(
            request(
                "POST",
                "/api/v1/learning/session-plan-proposals",
                load_contract(file, SessionPlanInput),
            )
        )

    @learning.command("plan")
    def plan(identifier: str):
        """读取全部历史版本、授权目标缺口、具体输入/边界/验收项；不采纳或启动活动。"""
        show(request("GET", f"/api/v1/plans/{identifier}"))

    @learning.command("gaps")
    def gaps(identifier: str):
        """显示服务器已校验的未覆盖目标；不会打印原始模型草稿。"""
        detail = request("GET", f"/api/v1/plans/{identifier}")
        show(
            {
                "plan_id": detail["plan_id"],
                "versions": [
                    {
                        key: version[key]
                        for key in (
                            "version_id",
                            "version",
                            "proposal_metadata_available",
                            "uncovered_goal_requirements",
                            "source_gap_count",
                            "projected_gap_count",
                            "deduplicated_gap_count",
                            "unprojected_gap_count",
                            "legacy_gap_count",
                        )
                        if key in version
                    }
                    for version in detail["versions"]
                ],
            }
        )

    @learning.command("accept")
    def accept(plan: str, version: str, revision: int, confirm: str = typer.Option(...)):
        """明确采纳精确版本，需--confirm ACCEPT；不自动提高目标或能力。"""
        if confirm != "ACCEPT" or revision < 1:
            raise typer.BadParameter("需要有效revision与--confirm ACCEPT；未更改计划。")
        show(
            request(
                "POST",
                f"/api/v1/plans/{plan}/accept",
                {"version_id": version, "expected_revision": revision},
            )
        )

    @learning.command("activity")
    def activity(file: Path):
        show(request("POST", "/api/v1/learning/activities", load_contract(file, ActivityInput)))

    @learning.command("activity-show")
    def activity_show(identifier: str):
        show(request("GET", f"/api/v1/learning/activities/{identifier}"))

    @learning.command("attempt")
    def attempt(activity: str, file: Path):
        show(
            request(
                "POST",
                f"/api/v1/learning/activities/{activity}/attempts",
                load_contract(file, AttemptInput),
            )
        )

    @learning.command("attempt-show")
    def attempt_show(identifier: str):
        show(request("GET", f"/api/v1/learning/attempts/{identifier}"))

    @learning.command("submit")
    def submit(attempt: str, file: Path):
        """保存本人解释、迁移说明和帮助声明；运行成功不会自动标记掌握。"""
        show(
            request(
                "POST",
                f"/api/v1/learning/attempts/{attempt}/submit",
                load_contract(file, AttemptSubmission),
            )
        )

    @learning.command("close")
    def close(attempt: str):
        show(request("POST", f"/api/v1/learning/attempts/{attempt}/close"))

    @learning.command("observe")
    def observe(attempt: str, file: Path):
        """rubric_item_id必须来自冻结活动；独立人工review需要另一获授权身份。"""
        show(
            request(
                "POST",
                f"/api/v1/learning/attempts/{attempt}/observations",
                load_contract(file, ObservationInput),
            )
        )

    @learning.command("evidence")
    def evidence():
        show(request("GET", "/api/v1/learning/evidence"))

    @learning.command("withdraw")
    def withdraw(observation: str, revision: int, confirm: str = typer.Option(...)):
        if confirm != "WITHDRAW" or revision < 1:
            raise typer.BadParameter("需要有效revision与--confirm WITHDRAW。")
        show(
            request(
                "POST",
                f"/api/v1/learning/evidence/{observation}/withdraw",
                {"expected_revision": revision},
            )
        )

    @learning.command("adjust")
    def adjust(plan: str, file: Path):
        """基于具体未撤回观察提议新版本；不会替换已采纳计划。"""
        show(
            request(
                "POST",
                f"/api/v1/plans/{plan}/adjustment-proposals",
                load_contract(file, AdjustmentInput),
            )
        )

    @learning.command("reviews")
    def reviews():
        """仅查看可选复习建议，不创建外部提醒或定时任务。"""
        show(request("GET", "/api/v1/learning/review-suggestions"))

    @replay.command("teaching-dataset")
    def teaching_dataset(file: Path):
        show(
            request(
                "POST", "/api/v1/evolution/replay-datasets", load_contract(file, ReplayDatasetInput)
            )
        )

    @replay.command("research-dataset")
    def research_dataset(file: Path):
        """只捕获指定已完成研究run的实际观测；不接受伪造搜索响应。"""
        show(
            request(
                "POST",
                "/api/v1/evolution/research-replay-datasets",
                load_contract(file, ResearchReplayDatasetInput),
            )
        )

    @replay.command("attest")
    def attest(dataset: str, file: Path):
        """当前登录标注者确认精确frozen_hash；不会代填另一个人的同意。"""
        show(
            request(
                "POST",
                f"/api/v1/evolution/datasets/{dataset}/attest",
                load_contract(file, DatasetAttestationInput),
            )
        )

    @replay.command("run")
    def run(candidate: str, file: Path):
        """须显式funding_id与每臂预算；成功也不自动晋升策略。"""
        show(
            request(
                "POST",
                f"/api/v1/evolution/candidates/{candidate}/replays",
                load_contract(file, ReplayInput),
            )
        )

    @replay.command("show")
    def show_replay(identifier: str):
        show(request("GET", f"/api/v1/evolution/replays/{identifier}"))

    @replay.command("capture-trace")
    def capture_trace(file: Path):
        """显式逐会话同意采集真实服务轨迹；弱行为不作为正确或掌握标签。"""
        show(
            request(
                "POST", "/api/v1/evolution/trace-feedback", load_contract(file, TraceCaptureInput)
            )
        )

    @replay.command("typed-dataset")
    def typed_dataset(file: Path):
        """冻结真实路由目录或检索发布池/索引；用于同模型同预算双臂执行。"""
        show(
            request(
                "POST",
                "/api/v1/evolution/typed-replay-datasets",
                load_contract(file, TypedReplayDatasetInput),
            )
        )

    @replay.command("r3-retrieval-dataset")
    def r3_retrieval_dataset(file: Path):
        """显式冻结 r3 完整事实/证据/版本独立标签；不自动生成或猜测 Gold。"""
        show(
            request(
                "POST",
                "/api/v1/evolution/r3-retrieval-datasets",
                load_contract(file, RetrievalR3ReplayDatasetInput),
            )
        )
