# 多项目检索、质量初审与入库

JD 完成提取、语言选择后，确认目标默认进入项目库阶段。显式提供固定来源的原分析接口仍保持原功能。

本阶段允许多个项目分别提供不同学习材料，不要求一个项目覆盖全部 JD；技术栈匹配属于参考条件。架构检索不限语言，实现检索使用用户确认的对应语言。默认社区门槛为 **Star > 100 且 Fork > 50**，严格大于，两项同时满足；使用 Fork 数量而非仓库是否为 fork 的布尔字段。社区指标不是代码正确性证明，也不是入库排序分值。

## 阶段边界

`搜项目并入库` / `/projects` → 公开 GitHub 搜索 → 固定提交 → 采集质量证据 → 质量分层 → 项目库。

此流程不会启动知识分析、独立知识审核、教学发布或 Tutor。不调用模型，运行类型为 `project_discovery`，模型额度为 0；GitHub HTTP 有独立请求上限。用户确认的 JD 及语言配置作为私人目标依赖，公开搜索只使用通用技术词。

## 质量分层

- `priority_for_source_review`：通过社区门槛，在固定证据中观察到实现和学习文档，进入后续源码复核候选。
- `needs_quality_review`：存在实现，但本轮质量证据仍不完整，放在待复核区。
- `insufficient_implementation`：未取得实现证据，例如论文/资源清单，不进入实现项目列表；保留检索观察、来源和质量审计。
- `below_community_threshold` / `community_unverified`：未达到社区门槛或指标缺失，不进入可选项目列表；保留排除原因。指标缺失不按零处理，也不默认放行。

测试目录名、Star、README 功能宣传和页面部署工作流都不能单独证明质量。`runtime_test_result=not_run`、`semantic_quality_verified=false`、`production_readiness_verified=false` 明确保留。这里的初审不是“测试已通过”或“生产质量已验证”。

新规则不检查运行成功、测试断言、CI、锁文件来决定入选。先核实 GitHub 元数据，达标后按默认分支固定提交 SHA，通过 Git tree 选择 README、依赖配置和核心代码。每个 Git blob 校验对象 SHA 和字节数；文件正文仍由来源服务保存 SHA256 指纹。证据片段标记为 `partial_snapshot=true`，不能当作完整仓库。后续源码研究需按固定版本补齐实际阅读范围。

默认目标为 3 个优先候选，最多考察 6 个项目；不足时返回 `partially_succeeded` 与明确缺口。只复用符合当前规则、仍可访问的优先候选。新规则项目列表按仓库最新有效评估去重，不能因历史版本曾经入选而绕过新的拒绝结果；历史记录保留。

用户点名的项目可通过服务器 `ProjectPoolService.start_in(..., repository_urls=[...])` 定向补查。新请求使用 `project-pool-v4-community` 冻结规则，记录 `candidate_origin=user_nomination`，不跳过社区及内容门槛。`quality_policy` 支持显式传入经校验的阈值，默认继承当前目标的个人筛选规则。仅接受去重的 HTTPS GitHub 仓库地址。

质量检测支持 Node `assert.equal`、`assert.deepEqual` 等测试写法，避免把 TypeScript/Node 测试误判为没有断言。修正检测后使用 `reassess_in` 新增质量报告和项目记录，保留原报告并记录追溯关联，不能改写旧评估或终态任务结果。

## 数据存储

复用 SQLite / PostgreSQL 现有表，无新增 DDL。

| 内容 | 存储 |
|---|---|
| 原始搜索及请求回执 | `research_nodes`、`search_observations`、`external_requests` |
| 固定提交与采样源码正文 | `source_sets`，`files` 为 JSON，范围信息在 `metadata_json` |
| 质量证据与未知项 | `dependency_objects.kind=project_quality_report` |
| 用户筛选规则与原文 | `dependency_objects.kind=project_selection_policy`，关联 GoalVersion，独立于 JD 雇主要求 |
| 项目、提交、来源、质量、目标关联 | `dependency_objects.kind=project_pool_entry` |

项目记录通过阻断依赖绑定原始目标、固定来源和质量报告。来源撤销、权限失效或指纹异常后，项目不能继续作为可用材料。项目记录明确 `requirements_satisfied=false`、`knowledge_publication_created=false`。

## 读取接口

- `GET /api/v1/workflows/{id}/projects`：默认只返回优先候选。
- `GET /api/v1/workflows/{id}/projects?include_pending=true`：包含待复核区。
- `GET /api/v1/projects/{entry_id}/quality`：质量依据、文件指纹与缺口。
- `GET /api/v1/conversations/{id}/workflow-status`：项目库阶段和计数。

成功入库后停在 `awaiting_project_selection`，等待选择后续分析项目。项目库没有自动生成知识发布或向量知识索引。

旧 `project-learning-quality-v1` 报告及 v1/v2/v3 运行输入保持原合同；新规则使用 `project-community-learning-v2`。`reassess_in(..., metadata=..., policy=...)` 新增报告及项目版本，原版本通过 provenance_only 血缘保留。
