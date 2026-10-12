# S00 人工核对样例

日期：2026-10-12。材料为真实公开 JD 和经用户同意读取的 joblearn；没有真实模型调用。

1. **M1 材料**：`evals/datasets/manual_case/jd/` 保存百度 J105397、J99649、J99560 的实际 HTML、提取文本、来源、日期与哈希。前两份为实习，第三份为社招参考。单雇主、小样本不能代表市场。采集日不是发布日。
2. **M2 要求**：J105397 正文明确提出工作流节点编排、上下文传递、异常处理、状态追踪、结果归档；manifest 保存原句的 Unicode 左闭右开位置。初始需求是可追踪与可核查执行，不是“所有 Agent 岗位都必须用 Celery”。
3. **M3 个人对齐**：joblearn 的 `backend/app/services/import_service.py` 展示结构化数据导入，`learning_path_service.py` 展示学习路径 CRUD，`db/session.py` 展示 SQLAlchemy 会话管理。仅做静态阅读，没有运行该项目测试，也不据此认定所有代码由用户独立完成。持久化 Agent 状态、断线恢复及幂等副作用缺乏材料，结论是 unknown；不能据此判不会。
4. **M4 参考**：LangGraph 固定 commit `799c2f04fd3a56186e24e9fb8bbf3568e543cfbe`。已保存 MIT 许可证及选定源码，未执行下载代码。PostgresSaver 的连接入口和 setup 在 `libs/checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py` 64–110 行，put 在 262–349 行；实际文件和哈希见 reference manifest。其 checkpoint 只解决图状态持久化，不自动解决 CareerForge 业务事务与副作用防重。
5. **M5 阅读与小实验**：先读连接/setup，再读 get_tuple/put，最后读 types 中 interrupt。受控实验使用已锁定安装包，自编一节点图 value+1，保存 PostgreSQL checkpoint、关闭连接，用新连接读取值。实测通过；只证明 checkpoint 跨连接保存，不证明 Worker 故障恢复。
6. **M6 迁移任务**：在 CareerForge 自己的岗位研究业务上，实现“创建运行 → 后台处理 → 页面断线后重读状态与事件”。不改动 joblearn 代码。运行创建与 outbox 同事务；stage 防重；Worker 租约和 epoch 控制迟到写入；用户刷新不取消运行。实现位于 S02，当前是待执行任务。

## 冻结的验收条件

- 成果表现：固定输入的同一运行重复投递后只有一份有效阶段产物；事件可从指定 seq 补读。需要 PostgreSQL 行数、运行 ID、事件和执行日志。没有这些记录则 unknown。
- 解释证据：用户说明队列、业务表与 checkpoint 的职责以及“业务提交后 checkpoint 前崩溃”的处理。需要本人解释与代码位置；Agent 代写说明不算本人已掌握。
- 迁移证据：在岗位研究业务中演示该机制，说明与参考 value+1 图的差异，并保留故障注入前后记录。仅运行参考样例不算通过。

当前三项迁移条件均 not_executed。S00 的 checkpoint 兼容实验不能替代它们；首条真实迁移完成证据要到 S02/S07 及作者实际操作后补齐。

先修：HTTP/SQLAlchemy 事务与基本测试；已有材料可作学习起点但仍需本人确认。投入时间未提供，不生成周计划。
