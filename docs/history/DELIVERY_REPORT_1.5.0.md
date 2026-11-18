# CareerForge v1.5 交付验收报告

**日期：2026-10-29**

## 最终状态

这是可下载、可安装、带测试和部署文件的 **CareerForge v1.5 独立实现包**。它不是只有文档或空目录，也没有把测试替身作为默认生产模型。

但本次**没有达到“严格完整实现所有 v1.5/v1.2.1 规格，并且真实整体流程全部验收通过”的条件**。离线回归通过不等于完整产品通过；源码仍有明确规格差异，生产依赖和真实模型也未完成验收。

## 已交付的实际代码

- Typer/Rich CLI、FastAPI 接口、Bearer 鉴权、持久SSE与断线重放。
- 中央Coordinator、规则快速路径、模型意图识别、按条件调用 Tutor 或新知识构建。
- 研究问题树、Frontier调度、观测驱动Reflector、GitHub多检索通道适配器、固定源码与只读调查。
- Analysis/Review 隔离取证、Claim/FactAtom、内容覆盖检查、确定性 Publisher。
- 概念与项目知识、默认SelectionHead、跨域依赖、撤销、Dispute新复核。
- 任务感知 FTS/pgvector/RRF/可选重排代码，分项目检索与完整目录分页；Redis三级缓存和Singleflight。
- PostgreSQL目标关系模型、Alembic新库迁移、Outbox/Celery适配、租约/epoch/预算/收据和保守恢复。
- 教学布局/路由/检索/Frontier的受限候选、冻结Holdout评测、晋升、个人Canary、回滚及授权撤回。
- Pi JSONL桥接、服务端工作区、补丁审批、固定执行快照、独立命令批准、Docker监督端与成果记录。
- Prometheus输出、OpenTelemetry接入、Compose、wheel、CLI操作文档与83项工程测试。

上述条目中“有真实适配代码”和“已完成外部集成测试”不是同义词。

## 实际测试结果

| 验收项 | 结果 |
|---|---|
| 离线测试全部收集项 | **83通过，0失败，0错误，0跳过** |
| 是否只挑选部分测试 | 否；三批集合与完整 collect 列表逐项一致 |
| Python语句覆盖率 | **78.43%（4,280/5,457）**，不代表模型质量 |
| HTTP/SSE/CLI | 实际回环 Uvicorn + CLI子进程测试通过 |
| Wheel | 构建、独立目录安装、CLI启动和空库初始化通过 |
| 数据/接口 | 60个新库表，30个种子Topic，62个OpenAPI路径 |
| 真实模型/PG/Redis/Celery/Pi/Docker | **未验收** |
| 整个v1.5最终产品验收 | **未通过：存在实现缺口与外部验证阻塞** |

## 最重要的差异

1. **不是原仓库原地升级。** 本包没有包含原Vue/Tauri、全部原API与旧数据库迁移；初始化时主动拒绝旧库。只能放入独立目录、新环境、新库使用。
2. **还有功能未完整覆盖。** Topic/Facet完整治理、丰富个性化计划/Rubric、自由Prompt/Skill优化、公共多订阅者构建合并、大图分页传播、完整增量复核等见 `docs/KNOWN_GAPS.md`。没有把这些删出规格来宣称完成。
3. **外部依赖验收未执行。** 本环境没有生产PG/Redis/Celery/Docker及SDK依赖，没有模型/Embedding/Reranker凭证。测试明确使用fixture；不会以它们证明真实学习或审核质量。

## 重要文件

- `README.md`：实际启动方式与交付边界。
- `docs/DEPLOYMENT.md`：目标生产部署、本机开发、CLI与受控实践。
- `docs/REQUIREMENTS_MATRIX.md`：需求、实现、测试和差异逐项对应。
- `docs/KNOWN_GAPS.md`：全部已知未完成/未验收项目。
- `docs/VALIDATION.md`：实际测试范围、修复记录及重现命令。
- `validation/pytest.xml`：83项最终合并JUnit；原分批记录同时保留。
- `validation/environment.json`、`production.json`：真实环境阻塞记录。
- `validation/wheel_install.json`：构建产物安装检查。
- `docs/specifications/`：用户原始规格，不改写为“已经实现”。

**不要覆盖原CareerForge或把本包连接到旧数据库。** 它提供了实际可运行的实现和可复核工程证据，但不能将其包装成已经完成全产品上线验收。
