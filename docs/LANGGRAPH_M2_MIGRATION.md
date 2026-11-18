# Knowledge Build Graph 迁移说明

candidate550 的 M2 调度由 `workflows/knowledge_build.py` 承担，绑定 `knowledge-build-v1`。节点按目标、来源、分析派发与等待、审核派发与等待、覆盖检查、有限补证和最终发布推进。

Analysis 与 Review 保留独立 AgentRun，其租约、账单及结果仍由领域服务管理。等待子任务时，父图释放执行权，恢复时核对业务数据库。Checkpoint 保存引用和位置，发布仍调用原 KnowledgeService。

## 验证入口

`tests/test_langgraph_m2.py` 检查等待、失败子任务、有限补证和发布幂等。`tests/test_langgraph_postgres_m2.py` 在真实 PostgreSQL Saver 中验证恢复。相邻会话接续见 `tests/test_session_graph_m2.py`。

本轮实际结果见包根目录 `evidence/` 和 `ITERATION_REPORT.md`。M1 文档中的后续阶段描述属于该阶段历史记录。M3 至 M6 的实现和限制分别记于各自文档。M7 默认切换仍受真实业务与质量验收门槛约束。

图调度成功与语义质量分别验收。固定源码、真实 Research 和独立质量评分需要各自保留输入、模型输出和审核血缘。
