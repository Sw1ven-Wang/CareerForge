# v1.5 需求、实现与验收映射

> 下表为原始 v1.5 交付映射，保留用于追溯。1.5.1 的新增修复、真实基础设施结果与剩余限制以 `VALIDATION.md`、`../DELIVERY_REPORT.md` 为准；未变更原始产品规格。

状态说明：**离线验证**只指本次代码测试；**外部未验**表示存在适配代码但缺实机证据；**部分实现**有明确规格缺口。没有一个状态自动等于“最终产品完成”。

| 规格位置/需求 | 实现位置 | 代表测试 | 状态与边界 |
|---|---|---|---|
| §1/§10 CLI 薄客户端、自然语言交互 | `cli/main.py`、`api.py` | `test_api::test_real_loopback_cli_chat_sources_and_feedback` | 真实 localhost HTTP、SSE、独立 CLI 进程通过；模型/数据仍合成 |
| §3 集中协调、自动范围、条件委派 | `services/orchestration.py`、`conversations.py`、`runtime.py` | `test_symbol_lookup_skips_model_and_preserves_followup_focus`、`test_parent_children_shared_budget_and_total_limit` | 工程链路离线验证；真实意图准确率未评测 |
| §3.4 不强制每轮所有 Agent | 规则定位 → 检索/Tutor；KnowledgeBuild 两个独立子 Run | `test_smoke.py`、`test_runtime.py` | 已实施；总计两个子 Run，不伪装无限并行 |
| §4 TaskSpec/Plan 动态通道 | `contracts.py`、`services/retrieval.py` | `test_task_aware_channels` | 覆盖定位、概念、架构、核验；预算/范围由服务端重建 |
| §4 FTS+vector+RRF+rerank | `retrieval.py`、`adapters/model.py`、`models.py` | `test_rrf_uses_configured_weights`、`test_vector_path_and_direct_evidence_mode`、`test_embedding_and_rerank_http_contracts` | PG/真实语义模型未验；本地测试向量不是语义效果证明 |
| §4 全匹配项目目录/分组回答 | `knowledge.py`、`retrieval.py` | `test_full_directory_pagination_and_authority_cursor`、`test_cross_project_results_are_grouped` | 权威关系分页与有界回答分开；大语料性能未验 |
| §5 Research Tree/Frontier/Reflector | `research.py`、`research_policy.py`、`adapters/github.py` | `test_jd_tree_whole_path`、`test_multichannel_github_observations_not_approved_knowledge` | 真实动态树与预算代码；检索观测替身，线上搜索质量未验 |
| §5 固定快照与多语言结构 | `sources.py`、`investigation.py` | `test_source_path_rejection`、`test_source_input_preserves_exact_crlf_and_outer_whitespace` | Python AST 已执行；Tree-sitter 未安装验证；解析失败明确降级 |
| §5 两层知识分析 | RepoMap + Analysis 原子 Unit/Claim | `test_source_build_review_publish_and_teach` | 确有结构和专题取证执行；任意真实大仓质量未知 |
| §5 Independent Review | 先独立读取/冻结Bundle，再对照分析证据、覆盖检查 | `test_review_failure_cannot_publish`、`test_evidence_fact_tamper_rejected` | 合同可运行；两个真实模型的误判率没有 Gold 证明 |
| §5/§9 Publisher与事实约束 | `knowledge.py`、`dependencies.py`、`answers.py` | `test_answer_guard_preserves_evidence_boundary` 六种反例 | 无支持内容不自动成为发布事实；语义检测仍是模型判断 |
| §6 学习上下文/Plan/Tutor | `conversations.py`、`learning.py`、`answers.py` | `test_dialogue_plan_proposal_requires_accept_and_observation_no_mastery` | 学习焦点、引用和计划采纳已连通；丰富自适应教学部分实现 |
| §6 校验后SSE/回放 | `answers.py`、`api.py`、CLI | `test_api_full_source_to_sse_and_replay` | 已提交块重放无新模型调用；本实现先完成整份草稿校验 |
| §7 三层缓存/Singleflight | `adapters/cache.py`、retrieval与comparison | `test_singleflight_and_mutation_isolation`、hot-cache撤销测试 | 内存测试通过；真实Redis分布式锁与故障未验 |
| §8 反馈→候选→独立评测 | `evolution.py`、`policies.py` | `test_reflection_has_no_release_authority`、`test_leakage_and_modified_gold_cannot_promote` | 受限配置可评测；任意自由Prompt优化未完成 |
| §8 Promotion/CAS/Canary/Rollback | `evolution.py`、`rollouts.py` | `test_candidate_paired_eval_atomic_promote_and_rollback`、`test_canary_authorized_assignment_frozen_and_regression_reverts` | 精确版本与基线校验，下一轮生效；真实收益无结论 |
| §8 Research策略回放 | 生产/评测共用`frontier_key`，`replay_frontier` | `test_research_frontier_candidate_is_evaluated_on_same_observation_budget` | 仅同冻结搜索观测下调度；新查询语言改动不准自动晋升 |
| §8 私人经验与撤回 | `evolution.py`、`rollouts.py` | `test_withdraw_feedback_rolls_back_future_policy_and_blocks_reuse` | 停止未来使用/灰度、经验正文清理；全历史数据删除不完备 |
| §9 双域知识、默认Head | `security.py`、`knowledge.py` | `test_private_namespace_no_admin_override_and_hot_cache_revoke`、`test_concept_default_head_does_not_fork_by_sourceset` | 私有权限、概念默认选择离线验证；完整公共订阅任务合并未实现 |
| §9 类型依赖、跨域失效 | `dependencies.py`、`lifecycle.py` | `test_shared_source_private_consumer_and_impact_privacy`、`test_topic_link_revoke_not_whole_publication` | 合法跨域边和同步读屏障；大图分页传播未实现 |
| §9 Dispute/增量更新 | `lifecycle.py` | `test_dispute_fresh_review_required_and_history_unchanged`、`test_changed_source_requires_review_does_not_revoke_old` | 新独立复核与保守diff；无自动语义carry-forward |
| §10 Pi/补丁/命令/成果 | `coding.py`、`adapters/pi.py`、`adapters/sandbox.py` | `test_coding_review_apply_run_submit_accept`、`test_old_patch_conflict_and_command_binds_revision`、`test_no_shell_fallback` | API/状态/审批通过；真实Pi/Docker未验，当前仅Python执行环境 |
| §10 独立尝试隔离 | `coding.py`、`conversations.py` | `test_independent_attempt_pauses_chat_and_seals_first_submission` | 系统内辅助暂停与提交封存；无法证明外部没有帮助 |
| §11 Trace/metrics | `observability.py`、gateway/runtime/API | metrics HTTP测试、收据测试 | Prometheus输出通过；OTLP外部collector和性能基线未验 |
| §11 持久化可靠性 | `runtime.py`、`worker.py` | 九项runtime反例、迁移测试 | SQLite服务层通过；PG/Celery实机集成阻塞 |
| §12 继承旧工程与数据 | 独立schema，bootstrap/Alembic拒绝legacy | `test_initial_migration_new_sqlite_schema_and_legacy_refusal`、`test_bootstrap_also_refuses_legacy_database` | **与原方案完整原地继承有差异**，旧代码/数据迁移未完成 |
| v1.2.1 Topic/Facet治理 | `taxonomy.py` + `data/topics.json` | 启动30种子及查询使用 | 种子/引用可用；完整治理、合并拆分迁移未完成 |
| v1.2.1 全功能/生产完成 | 各模块综合 | 只能引用实际测试记录 | **未达成**，不能由离线测试通过推导 |

完整规格原文位于 `docs/specifications/`。这里不修改规格来适配当前实现，剩余项仍按未完成记录。


## 1.5.1 安全与可靠性增量

| 边界 | 修复与新验收 |
|---|---|
| 预算/租约/重放 | 空授权范围不再扩大；过期租约与取消后的迟到写被拒绝；子任务合同固定；PG 并发同键创建/共享计量/撤销锁序实测 |
| 知识与权限 | 独立审核证据建立 blocking 依赖；关系投影与冻结身份复核；pin 与 source 取交集；缓存内容/历史 SSE 返回前重验；比较 Facet 按 Topic 分组 |
| 编程授权 | argv/文件/版本/环境统一摘要；旧审批无回填；执行前检查最新状态；旧版本回执不可验收新版本；Pi 工具/模型帧由服务端约束 |
| 自进化 | 推广和 Canary 重新验证当前权限/供应商/数据集环境；评测完成不能恢复撤回授权的候选 |
| CLI/API | GET-only 断线重放与显式 resume；完整事件才推进游标；不完整流/中断返回非零；C1/OSC/双向控制字符过滤；未迁移数据库 readiness503 |
| 部署 | 可丢弃 Docker 测试栈、明确门禁/清理；实际 Compose 解析；容器步骤 blocked/not_run，不冒充已通过 |
