# LangGraph M1 候选：JD 提取执行适配

## 状态与范围

本轮基线是用户提供的 CareerForge Dialogue Loop V2 两部分 ZIP。两包包含不重叠文件，合并前验证了 CRC、相对路径、符号链接和重复路径内容，合并后 1,146 个唯一文件。

这是渐进式迁移的 M0/M1 工程候选，不是 M0–M7 完成声明。M2–M7 不进入默认执行路径。真实 PostgreSQL 持久化、独立进程硬退出恢复及 Celery/Redis 队列恢复已经在模拟模型条件下通过；真实 Pi/KamaClaude 冷启动仍属于后续验收，不能以这些工程测试替代。原有专业 Agent、Analysis/Review/Publication 合同、Celery、Redis、预算和授权全部保留。

## 本轮架构

```text
现有 POST /api/v1/jds/extractions / 对话动作
  → JDService.start_extraction
  → Runtime.create_in（预算、幂等、同事务 Outbox）
  → AgentRun 固定 workflow_engine / graph_version / state_schema_version
  → 原 Celery Worker / Application.execute
  → 原 claim / epoch / lease / authorization
  → LangGraph: validate → execute_domain → save_reference
  → 原 JDService.execute / ModelGateway / GoalVersion / StageArtifact
  → 原 Runtime.finish / RunEvent / SSE
```

只有新建的 `jd_extract` 根任务能够按配置选择 LangGraph。其他工作流继续使用 legacy，不创建第二个调度队列或新的 Agent。旧任务固定 legacy；回滚配置也不会改变已创建 Graph Run 的引擎。

Knowledge Build 的 research/analysis/review 子运行、有限补证和发布仍由原实现处理。禁止把本轮三节点 Graph 描述成完整 Knowledge Build Graph。

## 数据合同

AgentRun 增加：
- `workflow_engine`：legacy 或 langgraph，旧记录迁移为 legacy。
- `graph_version`：当前只注册 jd-extract-v1。
- `state_schema_version`：当前只注册 1。

Graph thread_id 等于 AgentRun.id，而不是 conversation_id。Graph Checkpoint 只保存 workflow_run_id、graph_version、state_schema_version、current_stage、result_stage_key，不复制 JD、源码、模型响应、知识或凭据。

业务结果始终由 CareerForge StageArtifact/GoalVersion 决定。恢复前调用原 JDService.validate_result，验证 GoalVersion 属于本 Run，业务 payload 与固定版本完全一致。Checkpoint 完成不代表业务结果有效；缺失或损坏时关闭成功路径，不重复调用模型掩盖数据问题。

数据库迁移：`cf166_pause → cf166_graph`。LangGraph Checkpointer 自带表由单独显式部署脚本创建，不在每次 Worker 启动时调用 setup。

## 引擎和依赖

默认保持 `CAREERFORGE_WORKFLOW_ENGINE=legacy`。

安装验证使用 Python 3.12.14；workflow extra 固定：
- langgraph 1.0.3
- langgraph-checkpoint-postgres 3.0.1
- psycopg[binary,pool] 3.3.8

完整本轮环境锁定位于 `constraints-langgraph-python312-linux.txt`。它与旧基线 constraints 分开保存，不能把本轮依赖矩阵的结果当作所有旧平台验证。生产部署需单独确认操作系统和 Python 版本。

```sh
python -m pip install -c constraints-langgraph-python312-linux.txt -e '.[dev,production,workflows]'
```

仅在明确的 test 模式允许 InMemorySaver，且测试报告明确其非持久特性。development 和 production 必须配置 PostgreSQL URL，连接失败不会降级到内存。

## PostgreSQL 验证与部署前置步骤

初始环境没有 PostgreSQL 服务。经用户授权后，已从 Debian 官方签名软件源在隔离工作区安装 PostgreSQL 17.11、pgvector 0.8.0、Redis 8.0.2 及依赖，完成下述核心持久化与恢复试验。先前官方源码构建因缺少 bison 终止的记录保留；随后采用官方二进制包完成验证。Docker 与真实外部模型不在本轮验证范围。

在用户可控的隔离 PostgreSQL 测试实例中，由运维准备数据库/角色，配置：

```sh
export CAREERFORGE_DATABASE_URL='postgresql+psycopg://.../careerforge_test'
export CAREERFORGE_GRAPH_CHECKPOINT_URL='postgresql://.../careerforge_checkpoints_test'
# 不配置模型 API key。先运行明确注入 fixture provider 的工程测试。
python -m alembic upgrade head
python scripts/setup_graph_checkpointer.py
```

脚本只输出健康状态，不输出 DSN。业务库与 Checkpointer 是两个提交边界，不能视为同一事务。

验收至少包含：
1. 真实 AsyncPostgresSaver setup、写入、读取及连接中断。
2. 完成 JD 业务提交后，在 Checkpoint 提交前终止 Worker，启动新进程并经 reaper/outbox 恢复。
3. 新 Worker 的 model receipt、GoalVersion 与 AgentRun 数量不增加；重复投递不重复计费。
4. 已发出的模型调用状态未知时保留 unknown，不能自动重付调用。
5. Checkpoint 已完成但业务结果缺失/损坏时不宣告成功。
6. 原 Worker 迟到写入被 epoch/lease 拒绝；授权撤销和预算上限仍生效。
7. 独立数据库维护/备份恢复后执行显式对账。

通过后才可在受控新任务中设置 `CAREERFORGE_WORKFLOW_ENGINE=langgraph`。此开关当前只覆盖 JD 提取，不能用于宣称全量切换。

## 恢复、暂停与错误

最终代码显式使用 LangGraph `durability="sync"`，保证下一节点开始前先完成前一 Checkpoint 写入。默认 async 模式的立即硬退出试验曾观察到无 Checkpoint，虽业务幂等可恢复，仍不符合本阶段持久化目标；该证据与修复后结果分开保留。

节点每次进入及业务提交后重新验证 Runtime fence 与 JD 授权。节点不设置通用 RetryPolicy；JD 原有有界结构化修复与 ModelReceipt 语义保持不变。

- 暂停：沿用原 pause_requested 和安全边界。节点被恢复时先核对业务结果，不重复创建 GoalVersion 或调用模型。这不是 M3 的完整 interrupt/resume 对话实现。
- 取消：沿用原终态；不能 claim 已取消任务。
- PostgreSQL OperationalError / InterfaceError：抛出专用 checkpoint transport 错误。Application 不立即终结或重试领域调用，保留运行租约；现有 reaper 在租约到期后提升 epoch、将 attempted receipt 对账为 unknown，并写恢复 Outbox。
- 缺表、未注册 Graph/schema、非法业务结果等配置/合同问题：明确失败，不通过默认值解释旧 Checkpoint。
- 进程真正终止：沿用现有租约回收。离线故障注入不等同真实 PostgreSQL 进程重启验收。

## 事件和只读 API

Graph 节点事件通过现有 RunEvent 记录：workflow.stage.started / workflow.stage.completed。事件与去重 StageArtifact 在原 lease 事务中提交；payload 仅包含 workflow_run_id、stage、graph_node。原游标事件接口/SSE 保持不变，不发送原始 Graph State。

新增 `GET /api/v1/runs/{identifier}/graph`：沿用 owner、namespace 与业务访问检查；返回引擎、thread_id、Checkpoint 是否存在、版本及允许的阶段名。Checkpoint 与 Run 的 identity/version 不一致则拒绝；不返回内部 State 或数据库错误。legacy 返回无 Graph Checkpoint。

`Checkpointers.health()` 提供只读连通性检查；setup 脚本完成后输出该检查，不把 SELECT 1 误认为 Checkpoint 表已可用。

## 回滚

1. 对新任务恢复 `CAREERFORGE_WORKFLOW_ENGINE=legacy`。
2. 保留 Graph 代码、依赖、Checkpoint 和旧 Graph Run，使其按原版本完成或由授权管理员诊断。
3. 不把未完成 Run 改成另一引擎；不删除已有 GoalVersion、AgentRun、ModelReceipt 或 Publication。
4. 存在 Graph Run 时迁移 downgrade 明确拒绝移除列。无 Graph Run 的纯试验库才可按正常迁移降级。
5. 不将损坏或不兼容 Checkpoint 静默清空后重新收费执行。

## 下一阶段门禁

M1 最终源码已经通过真实 PostgreSQL 持久化/重启和真实 Celery/Redis 队列恢复试验（模拟模型、solo Worker、加速租约过期）。最终全量离线回归结果以阶段报告为准。完成 M1 工程门禁后实施并验收 M2：现有知识构建节点拆分、独立审核不通过分支、有限补证后再次审核、发布幂等、学习计划 interrupt，以及 Pi/KamaClaude 各一次真实冷启动。

M3–M6 分别推进对话控制、学习、练习和 Evolution，保持独立版本/授权/业务合同。M7 只能在 M0–M6 全部验收通过后启用全量新工作流并清理无调用者代码。真实模型验收需要可用且经授权的模型执行渠道；本轮没有调用用户 API 凭据或付费外部模型，也未测量真实 token/费用、教学质量或质量提升。


## candidate550 后续实现索引

M2 已有独立说明 `LANGGRAPH_M2_MIGRATION.md`，M3–M6 分别见同目录阶段文档。上文的“下一阶段”保留 M1 冻结时语境。本轮执行结果以包根目录 `ITERATION_REPORT.md` 和新生成日志为准。
