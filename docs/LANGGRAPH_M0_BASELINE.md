# LangGraph M0：冻结基线与迁移边界

## 1. 审计范围与证据

本清单基于交付源码的静态审计，不是生产验收或真实模型运行报告。审计日期：2026-11-11。未调用模型、GitHub、外部服务或用户 API；未运行应用工作流。本文仅新增文档，不修改业务实现。

冻结输入 `RELEASE_STATUS.json`：

- candidate：`careerforge-1.6.6-dialogue-v2-20261111.1`
- version：`1.6.6`；status：`review_candidate`；production_accepted：`false`
- base_source：`CareerForge_Review8_Local_Trial_20261110.zip`
- baseline_sha256：`c99744431eac9a12c00be57a06e285506beb24e9b3760941eaa172f99f9078d6`
- product_git_commit：`7058de85ec2762791a2684bb05845363d30e0848`

上述 hash/commit 是交付声明，不是本次重新计算的源码树 hash。README 的标题仍为 1.6.6 RC9，并明确保留历史失败与未验收范围；不得把 README 的历史测试数字当成本次基线结果。实际输入已经包含 Dialogue Loop V2，不能退回纯 Review8 的状态模型。

主要证据：`src/careerforge/application.py`、`worker.py`、`api.py`、`models.py`、`models_dialogue.py`、`models_runtime_ext.py`、`services/runtime.py`、`services/run_pause.py`、`services/conversation_actions.py`、`services/workflow_controller.py`、`services/workflow_continuation.py`、`services/workflow_status.py`、`services/build.py`、`services/knowledge.py`、`adapters/model.py`、`adapters/cache.py`、`routes/dialogue.py`。

## 2. 现有执行入口与职责

`Application.execute(run_id)` 先 `Runtime.claim`，随后保持 epoch/worker lease heartbeat；`_execute` 验证授权与冻结的 model_profile，再分派业务服务。队列不是事实来源，数据库 AgentRun 才是执行账本。

| AgentRun.workflow | 当前分派 | 未来 Graph 包装边界 |
|---|---|---|
| knowledge_build | BuildService.root | 知识构建主图；暂保留原子阶段与子任务协议 |
| analysis | BuildService.analyze | 分析节点，保留独立 AgentRun |
| review | BuildService.review | 独立复核节点，保留独立 AgentRun |
| chat | ConversationService.process | 对话/检索/Tutor 包装，不重写回答合同 |
| coding | CodingService.generate | 编程与执行授权边界，不自动加入普通学习图 |
| evolution | EvolutionService.execute | 演化候选执行 |
| learning_plan | LearningService.execute_plan | 计划提议节点 |
| jd_extract | JDService.execute | JD 抽取节点 |
| learning_assessment | AssessmentService.execute | 学习评价节点 |
| evolution_replay | EvolutionService.execute_replay | 独立回放节点 |
| retrieval_index | RetrievalService.execute_index | 检索索引任务 |
| research | Runtime.WORKFLOWS 接受此值，但 Application 没有独立分派分支 | 当前研究由 build 调用 ResearchService；不可误称已有独立 research Worker |

成功出口使用 `result.gaps` 决定 `succeeded` 或 `partially_succeeded`。PauseBoundary 进入暂停确认；LeaseLost 不允许旧 Worker 提交；DomainError 中含 BUDGET 的错误记为 `budget_stopped`，其余记 `failed`。未注册分派以 WORKFLOW_UNREGISTERED 失败。

Runtime 必须继续负责：命名空间与身份检查、费用授权、角色/数据外发范围、总调用额度与子任务预留、deadline、epoch fencing、心跳、取消、暂停安全点、幂等请求、已付费结果重用、未知结果阻止再收费、运行审计及 durable outbox。LangGraph 只逐步接管通用节点调度、条件边与执行位置。

## 3. 状态合同与转移

### 3.1 AgentRun / 运行状态

当前状态列是 String，没有枚举 CheckConstraint；下列为已读执行路径的有效状态，不要从“列允许任意字符串”推导可接受的新状态。

| 起点 | 条件/操作 | 终点 |
|---|---|---|
| 新建 | 原子保存预算预留、run、Idempotency、outbox | queued |
| queued | claim 成功；增加 epoch，绑定 worker 和 lease | running |
| running | 子任务未全部 terminal | waiting_children |
| running / waiting_children | 子任务已全部 terminal；重新发 outbox | queued |
| running | 所有阶段完成 | succeeded / partially_succeeded |
| 非 terminal | 取消、授权撤销、失败、预算停止等 | cancelled / failed / budget_stopped |
| running | lease 超时，且允许恢复 | queued；旧 attempted receipt 标 unknown，epoch fencing |
| queued / waiting_children / running | pause 请求并到安全点 | paused，或 pause_pending_inflight |
| paused | 全家族暂停完成，授权/期限/依赖重新验证 | queued 或 waiting_children |
| pause_pending_inflight | 外部结果仍 attempted / unknown | 禁止恢复，要求 reconciliation |

terminal 集合严格为 `succeeded, partially_succeeded, failed, budget_stopped, cancelled`。`paused` 和 `pause_pending_inflight` 不是成功，也不是 terminal。暂停不重置 deadline，不重新授予预算。终态不能原地恢复为新任务。

### 3.2 Dialogue WorkflowSession

- control_state：代码显式使用 `active, pause_requested, paused, resume_requested`。
- milestone：`awaiting_jd`（模型默认）；动作/continuation 路径使用 `awaiting_extraction, extracting, awaiting_goal_confirmation, building, planning, awaiting_plan_acceptance, tutoring, practicing, feedback`。
- waiting_user_action 是 target 中的单独字段，不是 AgentRun.status。可见值包括 provide_goal、authorize_extraction、confirm_scope、confirm_goal、confirm_topic、select_source、authorize_plan、accept_plan、authorize_tutor、submit_practice、reviewed_evidence_required、resolve_workflow_blocker、review_run_failure、waiting_reauthorization、needs_reconciliation、await_pause_checkpoint、resume。
- status projector 另产生 execution_state（无 run 时 idle），聚合 active/completed stages、call cap/held/consumed、publication_count、coverage、错误与待确认版本；这些是投影，不应成为第二份权威执行状态。
- ConversationActionReceipt 保存 action plan/request hash 与结果；会话 focus revision、workflow revision 和明确的计划版本确认继续约束动作。

### 3.3 GoalVersion 与事件

GoalVersion 没有 execution status 列：它以 `(goal_id, version)` 唯一，保存 source_text、content_hash、requirements，并唯一绑定 run_id。CareerGoal 持有 active_version_id/revision。WorkflowSession 引用固定 goal_version_id 和 JD revision；GoalAddendum 保存增补及来源目标引用，不修改旧版本。

RunEvent 以 `(run_id, seq)` 为主键，事件包括 run.created、run.started、task.created、run.waiting_children、task.children_ready、stage.completed、run.recovering、run.finished，以及 run.pause_requested / run.paused / run.pause_pending_inflight / run.resumed。

对话活动事件不是 RunEvent：ConversationActivityEvent 以 `(conversation_id, seq)` 和 `(conversation_id, origin_key)` 分别唯一；保存 kind 和引用。Turn 的回答流为 StreamEvent，唯一键 `(turn_id, seq)`。事件行本身没有“执行状态”；Outbox 使用 delivered 布尔值；未知调用结果属于 receipt，不属于 SSE。

## 4. 当前业务节点、转移与副作用

| 节点 | 现有实现 | 数据库写入 | 模型/外部副作用 | 后继与门禁 |
|---|---|---|---|---|
| 接收动作/范围 | dialogue_routing / ActionExecutor / WorkflowController | receipt、workflow、focus、activity events | 常规确定性动作不要求模型；其他 Coordinator 路径可能调用模型 | 授权缺失时等待；不得从引用/否定文本获得授权 |
| JD 导入 | JDService 导入合同 | 原文、hash、段落与 revision | 导入本身无模型 | 显式授权后 jd_extract |
| JD 抽取与主题映射 | JDService.execute / jd_topics | GoalVersion、阶段引用 | extract 模型经 ModelGateway | 提取后 awaiting_goal_confirmation，不跳过确认 |
| 研究/固定来源 | BuildService.root / ResearchService / SourceService | 研究节点、缓存、SourceSnapshot/阶段 | GitHub 搜索/读取；规划/反思模型按原合同 | 源码必须固定 commit，来源/额度不扩大 |
| Analysis | BuildService.analyze | UnitVersion、Claim/FactAtom、证据/阶段 | analysis 模型、受限源码读取 | 完成独立 analysis child |
| Independent Review | BuildService.review | ReviewDecision、独立读取证据 | reviewer 模型、独立源码读取 | 完成独立 review child |
| Coverage / bounded evidence retry | BuildService._retry_evidence / _review_task_sufficiency / coverage 服务 | 有限重试计划、结果与覆盖 | 可能再次分析和独立审核；固定调用预算 | 不等于无条件循环；未知/缺证显式保留 |
| Publication | KnowledgeService.publish | Publication、依赖、索引 outbox | 本身不需模型 | 只有有效 accept_scoped / accept_partial_scoped；保留部分范围 |
| 计划提议 | WorkflowContinuation.propose_plan / LearningService | plan/version 与 run link | learning_planner 经 gateway | awaiting_plan_acceptance，精确版本采纳 |
| Tutor | WorkflowContinuation.tutor / ConversationService / AnswerService | turn、answer blocks、引用/事件 | 检索、Tutor/checker 等按现有合同 | 只消费有效已审已发布知识 |
| Practice/Feedback | continuation.practice / learning_ext / EvolutionService.feedback | activity、attempt、observation、feedback | 当前 understanding_check 不自动执行代码；评价/演化为单独授权路径 | 不把一次观察当永久掌握；反馈默认不授予训练 consent |
| Evolution | EvolutionService / replay / gate / rollouts | 候选、独立回放、门禁、晋升/回滚记录 | 模型与可能的执行受各自授权 | 候选不等于晋升，模拟指标不构成真实收益 |

重要现状：BuildService.root 已包含 sources、analysis_child、review_child、coverage/retry、publication 等恢复路径；不是每个逻辑节点都有独立公共 API。现有知识构建可能先发布已审初始证据，再评估覆盖并补证，不能为了贴合目标图示悄悄改变发布语义。若 M1 仅包装整个 root，则仍由旧 Runtime 执行子任务调度，不能声称 M2 已完成。

## 5. 幂等与崩溃窗口清单

| 边界 | 已有唯一键/防护 | Graph 必须保留的行为 |
|---|---|---|
| 创建 run | Idempotency `(principal_id, operation, key)`；run.create 请求 hash；PostgreSQL advisory transaction lock | 同键同请求返回原 run；变更输入拒绝；丢失结果不重新计费 |
| 子任务 | RunAllocation `(parent_id, task_key)`；child_id 唯一 | 重入复用原 child 与预算，不重新分配 |
| 阶段 | StageArtifact `(run_id, stage_key)`；input_hash 和 payload hash | 复用结果；内容冲突报 STAGE_CONFLICT |
| 模型 | ModelReceipt `(run_id, stage_key)`；request_hash、profile_version、epoch | completed 复用；attempted/unknown 不自动重发；not_dispatched 才可按合同重派 |
| 发布 | Publication.unit_version_id 唯一；publish 查询已有结果并重新验证读取资格 | checkpoint 丢失也不能重复发布；不得绕过 KnowledgeService |
| 对话动作 | ConversationActionReceipt `(conversation_id, idempotency_key)`；request/action plan hash | 重放回执，不重复执行多动作 |
| 工作流关联 | WorkflowRunLink.run_id 唯一；`(workflow_id, milestone, ordinal)` 唯一 | 同里程碑稳定 run 映射，不能创建新 run 代替恢复 |
| 活动事件 | `(conversation_id, origin_key)` / seq 唯一 | 稳定 origin 去重后再发 SSE |
| milestone 消费 | delivered + `milestone-observed:{run_id}` 活动 origin | 原子观察与继续，重复投递无重复计划 |
| Tutor / Practice | `dialogue.tutor:` / `dialogue.practice:` + 原消息 key fingerprint | 保留消息到业务结果身份；不把图 step 序号当唯一业务 key |
| 共享构建 | SharedBuildSlot `(namespace_id, scope_hash)`；TaskSubscription `(run_id, subscriber_id)` | 不把每个订阅者变成重复的共享付费任务 |

数据库写入完成与 graph checkpoint 提交之间必然可能崩溃；checkpointer 不会自动使领域副作用 exactly-once。节点重放必须先查询原领域回执/阶段，写入仍经原事务与授权 fence。ExternalRequest 有 run/operation/request_hash/status，但本模型没有数据库唯一约束；不能假设全部外部请求都自动幂等。

KnowledgeService.publish 明确要求当前 run.workflow=knowledge_build，review/analysis 分别为该 run 的成功子任务，且 namespace/actor/hash/依赖有效。Graph 节点不能用新 graph-run 身份直接写 Publication，也不能删除两个独立 AgentRun。

## 6. Celery、Redis、Outbox 与 SSE

- `worker.execute` 是 Celery delivery adapter，任务名 careerforge.execute，只传 run_id。acks_late=true、reject_on_worker_lost=true、prefetch=1、ignore_result=true，不使用 Celery result store。
- `dispatch_once` 先 reap/maintenance/projections，然后取未 delivered 的 kind=run outbox，send_task 成功后标记 delivered。两步之间崩溃可能重复投递，Runtime.claim/lease 与业务幂等负责保护。
- Application.process_projections 处理 invalidation、feedback.withdrawn、gap.auto/gap.answer、workflow.pause_checkpoint、workflow.milestone、index。迁移不能仅接入 graph 就跳过这些投影。
- compose 将 Redis broker 与 cache 分离：broker 为 AOF/noeviction；cache 为 256mb/allkeys-lru。RedisCache 提供分层 TTL、指标和带 token 的 single-flight 锁。缓存不是业务事实或执行 checkpoint。
- 生产 Application 使用 RedisCache；测试/开发使用 MemoryCache。Celery 缺失时 Worker 拒绝启动，不静默切换内存队列；Application.drain 在 production 禁用。
- `/api/v1/runs/{id}/events` 是按 after 读取 RunEvent 的 JSON 查询，不是 SSE。
- `/api/v1/turns/{id}/events` 为 SSE，cursor=max(after, Last-Event-ID)，轮询持久 StreamEvent，0.25 秒 heartbeat；completed/interrupted/blocked 结束。
- `/api/v1/conversations/{id}/activity-events` 为 SSE，读取持久活动投影，0.5 秒 heartbeat，可 once；检查 cursor 上限与可见性。重连使用原 cursor，不依赖 Redis pub/sub 或模型实时 token 流。
- 新 graph 事件应转换为最小安全引用和原 durable sequence，不直接输出 graph state、私有 prompt、token、原文或未经验证的草稿。

## 7. 建议 M1 State / 数据关系（设计，不冒充现有实现）

Graph state 只存稳定引用与执行元数据：schema_version、graph_name、graph_version、workflow_run_id/agent_run_id、workflow_session_id、conversation_id、principal_id、namespace_id、goal_version_id、source revision/snapshot refs、input_hash、result refs、bounded retry counters、interrupt reason。不要复制 JD 全文、FactAtom 正文、密钥、完整知识包或可变授权对象。恢复时从领域数据库重新读取和验证。

关系：Principal/Namespace → Conversation → WorkflowSession → WorkflowRunLink → AgentRun；WorkflowSession → GoalVersion → CareerGoal；AgentRun → FundingAgreement / parent AgentRun / RunAllocation / StageArtifact / ModelReceipt / RunEvent / Outbox；analysis AgentRun → UnitVersion → FactAtom；review AgentRun → ReviewDecision → Publication；WorkflowSession → GoalAddendum；Conversation → ActionReceipt / ActivityEvent / Turn → StreamEvent。

新 GraphExecution 映射应使用独立、稳定 thread_id，绑定一个长工作流执行而非所有 conversation_id。需要明确何时跨多个 milestone AgentRun 维持同一 graph execution，以及原 root/child ledger 的关系；不能把 WorkflowSession（长期交互容器）误当已存在 WorkflowRun 表。输入中未见独立 WorkflowRun 模型。

生产 checkpoint 使用 PostgreSQL saver（目标 AsyncPostgresSaver），与领域事务分离但通过稳定 ID 关联。InMemorySaver 仅测试；不支持持久恢复时应明确失败。graph_version/state schema 不匹配必须阻止误读，不能静默用新代码解释旧 checkpoint。

## 8. M0 验证与尚未完成项

本清单没有执行测试，因此不报告“迁移基线通过”。既有可用离线回归入口包括 test_runtime.py、test_runtime_safety.py、test_build_recovery.py、test_dialogue_actions.py、test_dialogue_pause.py、test_dialogue_status.py、test_dialogue_workflow_e2e.py、test_dialogue_run_authority_v166.py、test_lease_heartbeat_lifecycle_rc11.py。真实 PostgreSQL 有 test_runtime_postgresql.py / test_runtime_ext_postgresql.py，应与离线结果分开。

输入已有 tests/fixtures/public_c09、native_consumer_context、native_checker_evidence、scope_binding_native 等夹具；它们不自动等于迁移文档要求的真实 JD + Pi/KamaClaude 全链路冷启动数据集。固定真实样本、来源 commit/hash、许可/隐私边界与预期不变量仍需独立完成。

审计时原 pyproject 要求 Python >=3.12,<3.14；production 已包含 psycopg[binary,pool]>=3.2,<4、Celery、Redis，但原基线没有 LangGraph/checkpointer 依赖。应为 M1 另外记录实际锁定版本和依赖兼容验证，不能把版本范围视为锁文件。

完成 M0/M1 仍需：冻结源码与新增补丁的独立 manifest；可重跑的离线基线报告；锁定依赖；显式 graph/legacy 路由；PostgreSQL checkpoint 与 Worker 重启试验；拒绝权限/预算/未知 receipt 的恢复测试；事件重放/重复投递测试。真实模型与生产验收本次未执行，用户禁止 API 消费的约束继续有效。M2–M7 不因适配层或离线单测通过而自动完成。
