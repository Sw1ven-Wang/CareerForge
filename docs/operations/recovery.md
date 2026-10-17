# 运行恢复与取消

五类业务工作流只有服务端注册的版本可执行，客户端不能注入 Python 阶段或指定 worker/epoch。运行事实保存在 PostgreSQL；Redis 只投递消息。

创建运行与 outbox 同事务。dispatcher 投递成功但确认丢失可产生重复消息；claim、阶段唯一键、稳定等待键及原子提交去重。Worker 使用数据库时钟与 epoch/worker/running/lease CAS；reaper 取得行锁后重查，拒绝旧 Worker 的迟到写入。

正常 waiting_user 不持有 Worker。resume 只能消费当前等待身份和固定输入，不把过期或沉默解释为同意。取消递增 epoch；业务阶段在同事务中重新检查租约后写产物与终态。界面 SSE 只是投影，断线后按序补读，数据库状态为权威。

部署不识别旧 workflow_version 时保留记录并明确失败，不静默套用新算法。内容变化创建新运行；目标/任务/提交变化均创建独立内容版本，不能沿用旧审阅验收。

排查顺序：在 `/runs` 看状态与事件 → 核对输入是否可读 → 看 `docker compose logs --tail 50 worker dispatcher` → 允许 dispatcher 重试 outbox 与 reaper。不要手改任务 accepted、租约或阶段正文。

证据：S02/S03 已有真实 Linux Celery、SIGKILL、接管与重放验证。本轮未反复杀进程；真实 Linux Worker 完成七个新业务 run，含返修补交与重新规划。最终整体回归暴露的并发确认旧对象问题已修复为锁后 populate_existing，专用竞争用例得到 201/409 且只写一份 Review/Confirmation。

## 固定版本审核子任务

审核批次父任务在 `waiting_children` 时不占 Worker。最后一个子任务结算后由 outbox 唤醒；重复投递沿用相同子任务键与阶段产物。父任务取消或 15 分钟截止将撤销活跃子任务租约，释放尚未使用的预留；已发出或结果未知的请求不退额度。不能保证中断供应商在途请求，迟到响应只允许补记计量。

在 `/runs` 查看父子任务和预算账本。不要手工修改 `held_calls`、`calls_reserved` 或 `run_allocations`，也不要把未知收据改为零费用。子任务暂存结果只有父任务持有效租约并重验输入后才能发布；父任务取消后不能单独恢复子任务入库。新部署须先备份数据库并运行迁移 `cf201017a002`。详见 [ADR 0008](../decisions/0008-bounded-review-subtasks.md)。
