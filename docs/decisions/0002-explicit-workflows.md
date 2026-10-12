# ADR 0002：移除 LangGraph，采用显式阶段工作流

日期：2026-10-12。状态：用户明确选择，替代 ADR 0001 的图执行方案及原 D03/D05。

## 决定

采用 Python 显式阶段函数与版本化工作流注册表；PostgreSQL 是运行、阶段结果、等待和决定的唯一权威。继续使用 Celery/Redis、outbox、数据库时间租约、epoch、预算收据和 SSE。不引入另一套工作流框架。

每个工作流定义按 workflow_version 冻结，由有限、有序阶段组成。阶段开始前复用有效 StageArtifact，否则执行受限工具/模型调用，在短事务校验 owner/输入授权/epoch/有效租约/状态，原子提交业务结果、StageArtifact 与关键事件。外部调用不持有数据库事务。

需要用户输入时，在同一事务保存冻结的问题/schema、WaitRequest、waiting_user 和事件并释放租约。回复按稳定 wait_request_id 保存唯一 ResumeDecision 并重新排队；重试先读取决定，不重复提问。API 移除 interrupt_id；运行模型移除 graph_generation/thread_id，graph_version 改为 workflow_version。

故障恢复在行锁内重查 DB 时间租约并撤销旧 epoch，从冻结输入和有效阶段产物重进。不存在独立 CP 或游标可直接证明阶段完成。预算不随重试或用户等待恢复归零。

## 验收映射与代价

F05 改为阶段提交后、执行器推进前失联；F06 改为摘要完成但缺阶段产物；F07 检查等待事务提交边界及唯一决定；F08 检查旧执行晚到、决定未消费时接管和新问题隔离；F23 检查 workflow_version 兼容。编号保留，权限、引用、幂等、预算与结果真实性门槛不降低。

不再需要业务状态与独立图检查点间的同步协议；需要自己维护阶段定义、恢复、等待及版本兼容，必须通过真实 PostgreSQL/队列故障测试。没有这些实测不得宣称 S02 完成。

S00 的 LangGraph 依赖兼容实验、JUnit 和公开参考源码保留为历史事实；不再是当前环境要求。原始输入文档不改写，docs/ 编号分册是更新后的实施依据。
