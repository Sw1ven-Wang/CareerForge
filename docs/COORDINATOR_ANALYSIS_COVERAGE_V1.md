# Coordinator 与 Analysis 覆盖协作合同

合同标识为 coordinator-analysis-coverage-v1。启动条件为 coverage_guided_analysis=true、auto_evidence_retry=true、一个已授权的固定仓库提交和 max_units_per_project=1。现有 Root、Analysis 与独立 Review 负责全部步骤。选项默认关闭，旧请求键重放沿用原合同与预算。

## 冻结与补查

1. 根任务冻结 ResearchTask、来源、提交、主题提示、预算与授权，再持久化 coordinator.analysis_brief.0。Brief 记录原问题、必要维度、JD 原文及其深度与逻辑关系。原 ResearchTask 保留非必需和可选条目。
2. Analysis 读取与模型调用前重新核对父子身份、费用授权、任务哈希、来源快照与根阶段。每次重新领取执行租约时沿用相同的核验。来源授权或绑定失效时停止。
3. 首轮 Review 独立读取和核验。先由现有发布服务处理通过的事实，再用该批已发布锚点判断哪些目标仍缺证据。证据已覆盖目标时，跳过补查。
4. 补查优先采用 Reviewer 提供的有效新行窗，其余名额可使用固定源码符号索引中的定位候选。跳过 Analysis 已完整读取的窗口。最多四个预读行窗、同一固定来源及一组现有 Analysis/Review 子任务。保存 evidence_retry_plan 和 coordinator.analysis_brief.1。
5. 最终以现有独立充分性评估及有效发布锚点计算 task_coverage。coordinator_coverage_decision 同步显示完成状态、待覆盖目标和停止原因。显式要求跨文件调用链时须有相应独立审核边，自主调查的连边需能接续。

## 查看结果

- 执行状态在 GET /api/v1/runs/{id} 的 status。
- 目标覆盖在 result.task_coverage 及 result.coordinator_coverage_decision。
- 任务中止且尚无结果时，在当前授权允许查看任务的前提下，顶层 coordinator_coverage_decision 显示中止原因和未确认目标。
- OR 已有合法满足项时，其他分支退出必需缺口。可选项不阻断覆盖。深度未知保留为待澄清，单独的未知深度不会触发静态补查。
- 项目分析中的局部 Fact、Facet 和静态连边各自保留来源。覆盖判定不验证学习者掌握程度。

## CLI

```powershell
careerforge build "解释工具调用的执行流程与失败边界" --source '<SOURCE_ID>' --topics 'agent.tool_calling' --max-units-per-project 1 --auto-evidence-retry --coverage-guided-analysis --calls 24 --duration-minutes 40 --planning-call-seconds 75 --yes
```

同一提交也可以由 POST /api/v1/knowledge/builds 发起。包内 OpenAPI 包含该选项，release 下提供简报 JSON Schema 与占位样例。
