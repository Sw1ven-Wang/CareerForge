# 1.6.0 附件 MUST / 验收映射

依据未改写的两份 v1.5 附件及其继承的 v1.2.1 合同。技术 §11、业务 §9 的真实质量验收仍须独立数据与真实环境；测试数量不是需求覆盖率。**本表取代旧当前状态；1.5.1矩阵仅留档。**

路径以下以 `src/careerforge/` 为根。最终统一结果与逐节点对账：`validation/v16/final_verified/`；分支报告只是辅助，不代替末版全量复跑。

| 规格位置 / MUST | 实现与入口 | 可复现工程证据 | 状态/剩余界限 |
|---|---|---|---|
| 技术§3、业务§3：模型提议不是授权；确定性操作直达、按需委派 | orchestration/runtime/conversations；冻结资金、来源、角色/外发类别；最多两个子任务合同不扩大 | `test_runtime*`、`test_api.py`、`test_funding_source_scope.py` | 实现；真实意图准确率待Gold |
| 技术§4.1–4.4、业务§4：任务通道/混合检索、范围分页、唯一EvidencePack | retrieval/knowledge、routes/retrieval；查询改写、批量index/rebuild、目录与回答样本分离 | `test_retrieval_v16.py`、`test_retrieval_native_v16.py`、`test_knowledge_postgres_integration.py` | 实际PG向量/ACL；合成基准不是语义收益 |
| 技术§5、业务§5：固定来源→Research/Analysis→独立Review→Publisher | sources/research/investigation/build/knowledge；模型输出闭合schema、actualsource/pack依赖 | `test_build_v16_integration.py`、knowledge/source/review测试 | 真实语法库；误判率仍待独立Gold |
| 技术§5/§7：多通道搜索、缓存优化不能扩大事实/权限 | adapters/github/cache；PR/Release/ETag、DB预算限流、Redis发布栅栏 | `test_github_v16.py`、`test_cache_resilience.py`、`test_retrieval_privacy_guard_v16.py` | 实际Redis/PG并发；未做真实GitHub质量评测 |
| 继承v1.2.1§6.2–6.4：分类治理、typed Facet、版本不改写 | taxonomy_governance/taxonomy/knowledge；`taxonomy` CLI/API | `test_taxonomy_governance_v16.py`、`test_governance_postgres_v16.py` | 已接真实生产路径；仅exact-definition映射 |
| 继承v1.2.1§11.5、§12.5–12.6：增量维护/复核 | lifecycle/build；`maintenance`计划/execute/schedule/toggle/tick | `test_incremental_maintenance_v16.py`、治理PG测试 | A/B/C安全路线；本地已导入快照调度，不自动远端抓取 |
| 业务§6、§9、继承v1.2.1学习合同：活动、帮助条件、观察与调整 | learning + routes/learning、`learning`CLI；Plan proposal/accept、attempt/rubric/evidence/reviews | `test_learning_loop_v16.py`、学习CLI/API/隐私测试 | 已接通；自评不推导掌握、复习不是外部提醒 |
| 技术§6、§10、业务§6.1/§9：引用条件不丢、校验后块流、恢复不重付 | answers/conversations/api/cli；one-block→Guard→commit→next，SSE完整元数据 | `test_atomic_streaming_v16.py`、`test_cli_resilience.py`、API/隐私并发测试 | 实现；真实首个有效块时延未测 |
| 技术§8.1–8.4、业务§7：三种反馈、独立评测后发布 | evolution/policies/research_policy/rollouts；`replay`与`improve` | 教学/研究/typed replay、trace feedback、Gold/Canary测试 | 四类真实服务回放；legacy生产旁路关闭；无真实效果结论 |
| 技术§9、业务§9：双域、系统预算共享、不转移个人费用 | services/shared + models_runtime_ext；`shared`API/CLI | `test_shared_builds.py`、`test_runtime_ext_postgresql.py` | 语义singleflight/订阅/清理/结果lineage通过工程验证 |
| 技术§7/§9：同步撤销读屏障、大图持久失效 | dependencies/lifecycle/runtime；SQL闭包流与durable cursor | `test_dependency_pagination.py`、runtime PG大图/并发测试 | >10k真实图验证；不是无限规模承诺 |
| 技术§9、业务版本边界：个人数据、来源与派生治理 | privacy + gateway +所有读写路径；`privacy`export/propose/confirm | `test_privacy_v16.py`、`test_privacy_postgresql_v16.py`、optional分类依赖两时机回归 | 运营删除治理实现；外部副本/备份/法律合规不作完成声明 |
| 技术§10、业务§6.2：Pi、补丁/执行/验收分开授权 | coding/adapters/pi/sandbox、Node runner、Python sandbox镜像 | `test_pi_runtime.py`、coding边界/执行合同测试 | 实际SDK合成推理；Docker blocked，本机IDE/多语言执行未实现 |
| 技术§11：收据、Trace、指标与质量对照 | observability/gateway、benchmark/hnsw probe/真实replay | HTTP metrics、实际收据/队列、fixture benchmark | 计量实现；人工Gold与生产性能待验 |
| 技术§12：保护旧数据，原工程兼容 | compat_export、services/legacy、routes/legacy；`legacy-export/import` | `test_legacy_compatibility.py`、固定基线SHA核对 | 私人未审核归档已做；原UI/API/schema非drop-in |
| 技术部署、业务长期一致性：迁移/worker/缓存/恢复 | cf160_features迁移、schema readiness、Compose/test harness | `test_v16_migrations.py`、全量native+production verifier | 原生PG/Redis/Celery实测；Docker not_run |

## 具体限制索引

- 分类/维护：`validation/v16/G02_G06_IMPLEMENTATION.md`。
- 学习/进化：`validation/v16/learning_evolution/MUST_SCOPE.md`。
- 公共任务/大图：`validation/v16/runtime/README.md`。
- 检索/搜索：`validation/v16/retrieval/README.md`。
- 流式/擦除：`docs/STREAMING_AND_PRIVACY_V16.md`。
- 所有剩余差异的严重性与是否本轮未完成代码：`docs/KNOWN_GAPS.md`。

未把规则/固定fixture的通过冒充“同一真实对话正确路由全部任务”“零幻觉”“独立Holdout改善”“教学效果”。这些仍属于附件原文产品验收，不因工程实现完成而删除。
