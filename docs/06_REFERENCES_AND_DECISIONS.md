# CareerForge 参考资料与设计决策

> 2026-10-12 实施修订：用户决定移除 LangGraph，采用 Python 显式阶段工作流；协议变更与验收映射见 [ADR 0002](decisions/0002-explicit-workflows.md)。原始 1.0 文档保留在 CareerForge_V1_Implementation_Package。

版本：1.0  
核查日期：2026-10-12

本文记录外部资料能够支持什么、CareerForge 据此作了什么决定，以及哪些事项仍需在实施中验证。官方文档和 main 分支会变化；本包的核查不替代 S00 的依赖兼容检查，也不代表已运行引用项目。

## 1. 原始输入与证据边界

产品依据为用户提供的 `CAREERFORGE_SIX_MODULE_DESIGN.md` 0.1 版，以及上一轮对需求、流程与技术选型的评审。本包细化六模块，没有读取到 EchoMind 或 joblearn 的当前源码、依赖锁文件和测试结果，因此不承诺其中某段代码可以直接复用。

下列外部资料以项目官方仓库、官方技术文档和雇主招聘正文为主。岗位页只说明对应岗位的要求，不能代表整个市场的统计分布；开源项目功能只说明其自身设计，不能直接证明 CareerForge 的职业建议或学习效果。

## 2. 技术来源索引

### S01/S02：显式阶段工作流与历史参考

当前采用 Python 阶段函数、PostgreSQL StageArtifact/WaitRequest/ResumeDecision、Celery/Redis，不依赖 LangGraph。详见 ADR 0002。

S00 曾验证 LangGraph PostgreSQL saver，并保存固定 commit 的参考源码；该实验只作为已完成的历史技术比较。可借鉴其持久化与业务事实职责划分，不将框架检查点引入当前恢复协议。当前验收使用阶段事务、防重键、稳定等待、数据库时间租约与 epoch。

### S03：Celery 任务与平台边界

来源：[Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)、[FAQ](https://docs.celeryq.dev/en/stable/faq.html)。

Celery 的投递、确认和重投策略需要结合任务幂等性配置。晚确认不能直接推导为外部副作用恰好发生一次；Worker 丢失的行为也需按配置与进程方式验证。CareerForge 将队列视为通知通道，以业务 outbox、租约和 StageArtifact 处理重复投递与故障恢复。

官方 FAQ 不支持原生 Windows Worker，因此后端集成环境采用 WSL2 或 Linux 容器。具体池、确认策略和 Worker 丢失配置写入实际版本清单，再通过 F01—F09 等用例验证。

### S04：PostgreSQL 与 pgvector 检索

来源：[pgvector 官方仓库](https://github.com/pgvector/pgvector)、[PostgreSQL 全文检索控制](https://www.postgresql.org/docs/current/textsearch-controls.html)。

pgvector 可与全文检索结果结合，融合与重排要记录实际实现。PostgreSQL 的 ts_rank 等全文排序函数不能直接标成 BM25。近似索引和过滤组合也可能影响召回，不能只看查询成功便认定资料已找全。

CareerForge 先用直接读取与明确词法策略，S09 在相同输入和上下文预算下比较后再决定是否保留向量检索。中文 JD、代码标识符与一般自然语言分别评测，不预设同一分词策略适合全部材料。

### S05：GitHub 仓库读取

来源：[Git trees API](https://docs.github.com/en/rest/git/trees)、[Search API](https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28)、[REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)。

目录树结果可能截断，搜索及读取受接口限制。CareerForge 记录 resolved_commit_sha、读取 manifest、truncated、跳过原因和实际覆盖，按剩余预算补读子树或明确不完整。源码引用必须来自实际读取的固定版本文件。

额度按实施时的认证方式与实际响应头处理，本包不把某个数字写成所有 GitHub 请求通用额度。仓库搜索、读取和许可证检查与运行代码是不同操作。

### S06：DeerFlow

来源：[v2.0.0 release](https://github.com/bytedance/deer-flow/releases/tag/v2.0.0)、[对应 commit](https://github.com/bytedance/deer-flow/commit/7e7f0410797693cf882594555ba414e0361d4c6f)、[当前架构文档](https://github.com/bytedance/deer-flow/blob/main/docs/ARCHITECTURE.md)、[RunEventStore 接口](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/runtime/events/store/base.py)、[运行事件说明](https://github.com/bytedance/deer-flow/blob/main/backend/docs/RUN_EVENT_STREAM.md)。

核对到 v2.0.0 的发布记录为 2026-06-25，commit 为 `7e7f0410797693cf882594555ba414e0361d4c6f`；2.0 是与 1.x 不共享代码的重写。当前 main 架构文档描述 harness/app 分层、Gateway 内嵌运行时与事件接口。不要把 main 后续内容全部归到该固定 release，也不要把 DeerFlow 架构写成 Celery 分布式任务系统。

CareerForge 主要借鉴职责分层、运行生命周期、可回放事件及有界工具。事件说明中的已知边界提示：图结束标记不能直接替代业务运行的真实状态；具体行为仍须读相应实现并验证。本项目使用自己的 AgentRun 终态和数据库事件，不照搬前端看到“结束”就宣告成功的判据。

推荐阅读顺序：S02 读运行入口与事件；S08 读取消、持久化与故障边界；S10 读委派与追踪。每次实际学习记录自己的固定 commit，不继续引用移动分支作为最终代码证据。

### S07：Open Deep Research

来源：[deep_researcher.py](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)。

可参考有限研究拆分、并行上限、原始材料与压缩研究结果的组织方式。CareerForge 的下游交接增加 evidence_ids、未确认项、结构化阶段状态与用量，且按自己的业务定义判断部分成功和失败。摘要用于组织上下文，不替代来源快照；研究代码本身不证明推荐或教学效果。

### S08：Aider 代码上下文

来源：[Repository map](https://aider.chat/docs/repomap.html)、[repomap.py](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py)。

可以学习符号信息、文件关系和上下文预算的组织。仓库地图并不是全语言精确运行调用图。CareerForge V1 先用 ripgrep 和 Python ast 定位与补读；S09 有实际需要时再比较更复杂的 repo map，不先复制整套编辑执行能力。

### S09：Langfuse 离线评测

来源：[Evaluate with Datasets](https://langfuse.com/docs/evaluation/get-started/offline)。

参考数据集、实验与评分结果关联的方式。CareerForge 的数据分组、金标准、可重复 runner、评分规则和原始收据保存在自己的工程中；Langfuse 用于观察和比较。外部 trace 服务不可用不能使业务状态丢失，也不能把一条 trace 的存在当作质量通过。

## 3. 求职方向的原始证据与推论

| 官方来源 | 对应岗位正文中的信号 | 本项目据此选择的展示内容 |
| --- | --- | --- |
| [百度 J105397：Agent 应用开发实习](https://talent.baidu.com/jobs/detail/INTERN/83a5dbcc-48a6-4356-b6d6-b4da9f0a2528)，页面日期 2026-09-03 | 包含评估样本/评分、工作流质量、上下文、异常和状态追踪等工作 | 有版本数据集、节点与业务质量评测、故障恢复与结果归档 |
| [百度 J99649：智能体应用开发实习生](https://talent.baidu.com/jobs/detail/INTERN/e3cec5b8-b7a3-4946-99fc-b292b749cd53)，页面日期 2026-07-21 | 涉及 RAG、Agent 框架、代码数据与 AST 等，语言要求为 Python/Java/Go 任一种 | Python 主线、代码定位与引用、经对照验证的检索方案 |
| [百度 J103363：AI 平台相关实习](https://talent.baidu.com/jobs/detail/INTERN/ef30e579-2d3d-4539-ad90-884521b815c9) | 该岗位偏 Java/Spring、数据存储及平台能力 | 当前 Python 项目可证明部分平台思路；不能替代岗位明确要求的 Java 能力 |

由这些具体样本作出的设计推论是：CareerForge 更适合定位为 Agent 应用/AI 应用后端的工程项目，优先证明业务建模、可靠执行、代码证据与评测。这个判断不是对全部岗位的频率结论，也不保证面试结果。若主投岗位改成 Java 后端或训练算法，应重新对照真实 JD 调整实现重心，而不是给现有项目添加一个无实际职责的服务。

## 4. 已采纳的设计决策

| 编号 | 决定 | 原因与代价 | 何时重新考虑 |
| --- | --- | --- | --- |
| D01 | 保留六模块，先做一条小闭环 | 减少采集与候选范围，保留任务到成果的价值验证 | 真实用户证明需要更广输入之后 |
| D02 | 一个后端代码库，API/Worker/dispatcher 多进程 | 职责清楚，部署仍可管理；需要明确事务边界 | 独立扩容或团队所有权构成真实需求时 |
| D03 | PostgreSQL 统一保存业务状态与阶段恢复事实，Redis 只发通知 | 去除独立图状态同步，但需维护显式阶段执行器 | 复杂动态工作流需求及对照证据出现时 |
| D04 | outbox + 有效租约 + 阶段唯一键 | S02 工作量较大；换来可解释的重复投递和晚到写入处理 | 只有等价能力的替代方案完成同组故障验收时 |
| D05 | 正常恢复与故障接管均按阶段产物和稳定决定重放 | 旧 Worker 的所有业务写入由 epoch/租约拒绝 | 等价恢复机制通过同组故障用例时 |
| D06 | 计划/目标/条件/提交内容不可变 | 数据对象增加；历史不会被当前材料误代表 | 只优化存储，不撤销可追溯性要求 |
| D07 | adopt 建新计划版本，创建实践任务独立 | 多一个明确操作；避免重复任务与学习意图混淆 | 用户研究支持合并交互时，业务事务仍分清 |
| D08 | 评审读取服务端完整条件，确认只覆盖当前提交 | 用户不能通过少传字段跳过难条件；修改范围需新版本 | 引入新验收维度时扩展，不弱化集合约束 |
| D09 | 导入优先、公开 Python 仓库、只读工具 | 第一版覆盖较窄；可核实材料并避免执行环境工程吞噬主线 | 来源、语言或可信执行有明确需求和预算后 |
| D10 | 直接/词法基线，向量与角色增强调用有实验门槛 | 不能一开始展示所有关键词；保留解释每个组件价值的证据 | S09/S10 的冻结对照结果支持时 |
| D11 | 自述、材料、自验、平台执行分开 | 不能自动给出“掌握了”总分；结论范围更清楚 | 增加可信 runner 或独立评审时，另定义身份与来源 |
| D12 | 正文不进入常规日志，删除先撤权再清理 | 需要依赖关系与删除清单；调试便捷性下降 | 只调整受控调试与保留窗口，不复活已撤销内容 |

这些是项目选择，不是外部框架的保证。对应落地细节见 02/05，对应验证见 04。修改决定时保留旧记录、理由、受影响版本和替代验收，不只改一处文字。

## 5. EchoMind 与现有代码复用记录模板

进入 S00/S01 后，按实际源码填写，不预填“已复用”：

| 待核对项 | 记录内容 |
| --- | --- |
| 身份 | 项目 URL、本地提交、文件路径、许可证 |
| 职责 | 输入输出、依赖、错误语义、是否含跨业务副作用 |
| 隐含假设 | 单用户、进程内状态、全局可变配置、无版本文档等 |
| 验证 | 现有测试、缺失测试、真实兼容检查与未运行项 |
| 决定 | 直接复用 / 改造后复用 / 只参考 / 不适用 |
| 归属 | 哪些来自上游，哪些由本人新增，后续如何跟进修复 |

可以优先检查配置、模型适配、API 客户端、页面状态和通用展示组件。不要为了大规模重构旧项目而延误 CareerForge 的第一条业务链；不适配的部分按当前阶段重做并记录原因。

## 6. 仍需实施时填入的信息

| 信息 | 最晚决定阶段 | 当前状态 |
| --- | --- | --- |
| 精确 Python/Node、框架/数据库/Worker 版本与锁文件 | S00/S01 | 待兼容验证 |
| 单一模型供应商、模型标识、结构化和工具契约、真实预算 | S00，最晚首次真实调用前 | 待用户实际环境配置 |
| 第一组 JD、个人材料、参考仓库 commit 与金标准 | S00 | 待按真实材料建立 |
| 个人每周投入、里程碑日期、运行费用上限 | 开始实施并持续更新 | 未提供，不代填 |
| 附件、索引、阶段副本、trace、备份的保留与删除窗口 | S08/S11 | 按实际部署填写 |
| 模型质量校准阈值及留出集配置 | 04 所定义的校准阶段 | 未实测，冻结前不得写提升结果 |
| 是否保留 pgvector、角色并行、MCP | S09/S10 | 以具体实验决定 |
| 部署域名、HTTPS、邀请范围与备份位置 | S11 | 未部署 |

工程不变量从对应阶段就执行；效果阈值和启用决定用真实样例校准。补齐待验证值是实施任务的一部分，不应由文档假造一个确定答案。
