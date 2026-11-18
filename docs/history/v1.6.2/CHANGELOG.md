# 1.6.2 (2026-11-03)

Bounded post-blind contract repair: new learning requests use typed task kinds and observable/rubric pairs with a versioned prompt/schema, accurate malformed-output versus opt-out errors, and the unchanged one-call cap. Old unmarked queued contracts and receipts remain exact. A generic adjacent multi-action regression receives bounded editing-verb-family detection with a frozen routing version; it only makes rules abstain. The original R08 model clarification is not overridden or claimed fixed. See docs/REPAIR_1_6_2.md; old reports and scores are historical, not 1.6.2 validation.

# 1.6.1 (2026-11-03)

General mechanism repairs from real-model failure evidence: explicit scope versus hints; conservative intent abstention; consent-bound evolution lineage; structured safe plan projection; relevance-checked budgeted atomic answers; versioned replay compatibility. Legacy context-bearing requests can only replay completed receipts; current-source navigation stays local and never rewrites the model question. The SDK lock corrects an unavailable base64-js tarball version while preserving its verified integrity and dependency range. See DELIVERY_REPORT.md for final evidence and limits.

# 更新记录

## 1.6.0 · 2026-11-02

以1.5.1可恢复检查点为基线，按两份附件补齐分类治理、学习闭环、四类策略实际回放、增量维护、系统公共共享构建、查询改写/批量向量、GitHub PR/Release/ETag与分布式限流、逐块Guard流、大图持久分页及个人删除治理。

- 新增`cf160_features`增量迁移与严格schema head/readiness；旧数据不获得新审核/费用权力。
- 原仓库固定基线只读核对，新增受限legacy只读导出/私人归档导入。
- 关闭跨人私有分类正文的模型外发依赖/回执擦除缺口；预约、派发、响应与缓存持久化均重验完整实际依赖。
- 来源资金范围贯穿准备/预约/外发/响应，跨Worker锁序与候选最终CAS已用真实PostgreSQL回归。
- 全部末版门禁和原始日志见`validation/v16/final_verified/`，历史1.5.1结论不冒充末版结果。Docker依旧blocked，不构建虚假通过。
- 具体保守能力与未完成验收详见`docs/KNOWN_GAPS.md`，不以测试数宣称产品全部完成。

## 1.5.1 · 2026-11-02

以原 v1.5 独立实现包为基线，集中修复可重现的权限、证据、预算、恢复和执行审批缺陷；不改写原产品规格，不将测试替身称作真实模型质量。

- Runtime：保留显式空授权；统一资金→根任务→子任务锁序；并发幂等创建；过期/撤销租约 fencing；恢复/失败子任务不能发布。
- Model Gateway：供应商端点纳入冻结身份；调用前配置验证；Embedding index/向量与 Reranker 响应校验；回放仍执行外发与角色合同。
- Knowledge：审核证据撤销沿依赖链阻断；来源/证据/事实/审核/发布投影与冻结内容核对；缓存和历史回答重验；固定版本不扩大来源范围；比较维度按 Topic 隔离。
- Evolution：评测环境/权限/模型漂移阻断晋升与灰度；撤回同意不能被迟到评测恢复。
- Coding：执行批准绑定完整文件/argv/版本/镜像身份；开始执行前重验；旧回执不能验证新版本；失败或撤销补丁不可应用。
- Pi：严格 JSONL 握手/顺序/预算与结束状态；服务端拥有模型上下文；仅选择文件经工具读取；完整多段读取与空文件支持。
- CLI/API：新增 `resume`；断流只重新GET，不重新提交问题；未完成事件不更新游标；中断非零退出；安全原子凭证写入与终端控制字符过滤；未迁移数据库 readiness503。
- Cache：内存 LRU 有界并释放完成锁；Redis 加锁后复查、原子租约核验与发布。
- Parser：实际 JS/TS/TSX/Go 解析验收，并补齐箭头函数、接口和类型声明。
- Deployment：独立 Docker 验证栈、生产迁移启动门禁、密码安全URL编码、严格沙箱回执/输出与清理验证；Python 精确版本约束、npm lock。

- Integrity：必需FacetValue投影缺失/被转至其他版本时阻断发布导出；可选维度仍允许省略。
- Types：修复原135条项目诊断，使用精确TypedDict/Literal和明确缺失记录拒绝路径；默认mypy零诊断并纳入CI，未用全局ignore掩盖。

### 数据兼容

新增 `cf151_command_binding` 迁移。**已由 Alembic 管理的本包 v1.5 数据库**可在备份后执行 `alembic upgrade head`。旧命令批准的摘要保持 NULL，必须重新提议和明确批准，不会凭迁移创造用户同意。

原包 `init --development` 通过 `create_all` 建立的未标记数据库，不能盲目运行 `alembic stamp` 或套用原项目迁移。先保留完整副本，在隔离副本中核对数据库来源与 schema；当前交付不自动认领这种旧库。全新开发库正常使用 `init --development`。

### 验收边界

实PG/Redis/Celery、真实解析器和 Pi SDK（合成推理）测试不等于 Docker 或模型效果验收。Docker 启动实际受当前 dot 工作空间权限/内核条件阻塞，构建和容器执行保持未运行。细节和完整证据见 `DELIVERY_REPORT.md`、`docs/VALIDATION.md`。
