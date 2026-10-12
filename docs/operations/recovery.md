# 运行恢复与取消

五类业务工作流只有服务端注册的版本可执行，客户端不能注入 Python 阶段或指定 worker/epoch。运行事实保存在 PostgreSQL；Redis 只投递消息。

创建运行与 outbox 同事务。dispatcher 投递成功但确认丢失可产生重复消息；claim、阶段唯一键、稳定等待键及原子提交去重。Worker 使用数据库时钟与 epoch/worker/running/lease CAS；reaper 取得行锁后重查，拒绝旧 Worker 的迟到写入。

正常 waiting_user 不持有 Worker。resume 只能消费当前等待身份和固定输入，不把过期或沉默解释为同意。取消递增 epoch；业务阶段在同事务中重新检查租约后写产物与终态。界面 SSE 只是投影，断线后按序补读，数据库状态为权威。

部署不识别旧 workflow_version 时保留记录并明确失败，不静默套用新算法。内容变化创建新运行；目标/任务/提交变化均创建独立内容版本，不能沿用旧审阅验收。

排查顺序：在 `/runs` 看状态与事件 → 核对输入是否可读 → 看 `docker compose logs --tail 50 worker dispatcher` → 允许 dispatcher 重试 outbox 与 reaper。不要手改任务 accepted、租约或阶段正文。

证据：S02/S03 已有真实 Linux Celery、SIGKILL、接管与重放验证。本轮未反复杀进程；真实 Linux Worker 完成七个新业务 run，含返修补交与重新规划。最终整体回归暴露的并发确认旧对象问题已修复为锁后 populate_existing，专用竞争用例得到 201/409 且只写一份 Review/Confirmation。
