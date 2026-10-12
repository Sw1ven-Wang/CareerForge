# CareerForge · V1 离线预览

个人 Agent 开发学习与实践工作台：岗位正文 → 要求与引用 → 个人材料与目标 → 固定版本参考代码 → 学习单元 → 在已有学习计划中编辑目标、交付物、工时和验收条件。

当前提供岗位来源、岗位分析、证据与学习计划、项目代码学习及运行记录，另有个人模拟调研汇总页。自主构建模块已按用户要求下线。工作流采用 **Python 显式阶段函数 + PostgreSQL 阶段产物 + Celery/Redis**；不安装或运行 LangGraph。保存的 LangGraph 源码仅是 S00 固定版本的只读学习材料，许可证和文件哈希随包保留。

**当前交付是离线工程版本。** 岗位提取使用规则与 FakeModel 契约；后续规划、代码学习和材料审阅采用保守规则，缺少证据保持 unknown。真实模型质量、独立校准/留出评测及作者本人真实迁移任务尚未验证，不宣称正式 R4 效果门槛通过。完整状态见 [V1 交付记录](docs/progress/V1_DELIVERY.md)。

## 启动

在项目目录运行（PowerShell）：

```powershell
uv sync --project backend --python 3.12 --locked
npm.cmd ci --prefix frontend
npm.cmd run build --prefix frontend
# 先准备 API 镜像，离线 UI 网关将复用它。
docker compose --profile app build api worker dispatcher migrate
docker compose -f compose.yaml -f compose.offline.yaml --profile app build frontend
docker compose -f compose.yaml -f compose.offline.yaml --profile app up -d --no-build
```

打开 **http://127.0.0.1:8080**。当前机器已启动这一入口。Docker Hub 的 Node 镜像拉取曾超时，故提供复用 Python 镜像与本地构建静态资源的网关；联网环境仍可使用原来的 Nginx 多阶段 Dockerfile。离线网关只用于回环地址下的本机私有预览。

没有通用初始管理员或后门账号。你可以在登录页注册自己的本地账号。当前已创建的**合成演示账号**：

- 邮箱：`v1-demo-20261012@example.test`
- 密码：`SyntheticOfflineDemo2026`

这个账号里的验收记录明确标记为合成流程测试，不构成 joblearn 或本人能力认证。joblearn 仅按授权清单读取，未修改、导入执行或测试它的代码。真实材料、数据库备份与完整运行记录均保存在 Git 忽略的 `private/` 或本地数据库中。

## 页面

| 入口 | 用途 |
| --- | --- |
| `/` | 职业目标与当前工作台 |
| `/sources`、`/market` | 正文/附件导入、要求引用、去重、人工更正与报告 |
| `/planning` | 个人材料改版与撤销、未知对齐、目标与完整条件、计划草稿和子集采纳 |
| `/projects` | 固定 commit 候选、代码引用、明确选择、学习单元、追问纠错、独立阅读进度 |
| `/mock-research` | 完整模拟调研、技能统计、个人材料与六周计划 |
| `/build` | 旧地址自动转到 `/planning`，不再提供独立构建功能 |
| `/runs` | 后台状态、持久化事件、等待决定与取消 |

## 模拟调研与计划

现有演示账号已导入五份虚构 JD、个人材料分析及六周计划草稿，打开 `/mock-research` 查看完整报告，打开 `/planning` 编辑已有计划。每周 12 小时仅为模拟假设。

计划内的“编辑目标”会在一个事务中保存目标新版本并更新计划草稿引用；失败时整体回滚。可调整目标选择、顺序、交付物、工时、前置依赖和完整条件。已采纳计划的修改保存为新草稿，原采纳版本仍保留。

旧六模块演示工具 `scripts/run_offline_demo.py` 已停用；本地 mock 原始文件与导入收据位于 Git 忽略的 `private/mock_research/`，可用 `scripts/import_mock_research.py` 在现有演示账号续传导入。历史流程记录仍保留，但不代表当前功能入口。变更见 [计划编辑调整记录](docs/progress/PLAN_EDITING_CHANGE.md)。

## 验证与运维

最终只做了一次整体回归：85 项通过、1 项并发确认失败、4 项 Linux 故障用例未重复运行；修复行锁后的 ORM 旧对象问题后，该失败用例定向通过。失败原始记录仍保留，没有改写成全绿。新业务关键闭环、类型检查、前端构建及 Linux Celery 七次业务运行通过。既有杀进程恢复证据沿用 S02/S03。

独立新库从全部迁移启动成功；备份恢复后 56 张表、430 行及内容聚合哈希一致。恢复工具只允许新建 `careerforge_restore_` 数据库，不能覆盖当前工作库。

- [启动与备份恢复](docs/operations/deployment.md)
- [恢复、取消与事件](docs/operations/recovery.md)
- [撤销、正文清理与保留边界](docs/operations/deletion.md)
- [工程取舍与真实限制](docs/interview/project_evidence.md)
- [离线检索与条件守卫对照](evals/reports/offline_comparison.json)

数据库密码为本机开发示例，所有端口仅绑定回环地址。自动物理擦除默认关闭；逻辑撤销及读取阻断立即生效，清理只在独立验证库执行过。外部模型、向量库、Langfuse 和 MCP 写操作均未开放。费用/Token/时长预算的真实供应商验证属于后续受控启用门槛，不能将当前调用数限额冒充生产成本控制。

岗位标准化 V1.1 已接入来源、调研和现有计划编辑流程；使用与回滚见 [数据协议说明](docs/progress/JOB_DATA_CONTRACT_V1_1.md)。

真实模型岗位候选提取入口与当前验证状态见 [真实 Agent 第一阶段](docs/progress/REAL_JOB_AGENT.md)。
