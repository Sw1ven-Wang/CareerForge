# S02：Python 显式工作流持久化内核

日期：2026-10-12。按用户确认的 ADR 0002 实施；不依赖 LangGraph 或独立 checkpointer。

## 当前结果

已实现 PostgreSQL 运行事实、Celery/Redis 通知、事务 outbox、租约与 epoch、阶段唯一键、稳定等待与唯一决定、调用额度预留/未知收据/独立晚到结算、有界瞬时重试、版本化函数注册、运行控制与 JSON/SSE 事件、Vue 运行记录和决定页面。演示工作流仅供内部测试，不新增公开的第六种工作流或任意阶段执行接口。

运行输入与产物保存在 PostgreSQL；恢复按 `(run_id, stage_key, input_fingerprint, stage_version)` 复用产物。等待与恢复保留输入、run_id 和累计预算。等待时没有活动租约；取消和接管后的旧执行身份不能发布业务产物。模型调用与队列发送都不持有业务数据库事务。

S02 演示版本 v2 在问题中展示合成草稿；v1 已有问题保持兼容，问题不原地变化。所有模型调用均为 FakeModel/MockTransport，未调用真实模型、未使用现有模型凭据。

## 验证证据

- 完整测试：**60 passed，0 skipped**，见 `S02_TEST_RESULTS.xml`。设置 `RUN_LINUX_QUEUE_TESTS=1`，另行运行真实 Linux Docker Celery Worker；未使用 eager mode。
- Linux prefork Worker（并发 2）：真实 Redis 投递、重复通知、等待后恢复、outbox 发布后标记失败再投递均完成。
- 真正发送 SIGKILL 杀死阶段提交后暂停的 Celery 子进程，随后人工注入已过期 DB 租约并运行 reaper；新 epoch 复用草稿，模型额度仍为 1。该测试验证进程死亡和接管，不声称验证了真实等待 60 秒的生产检测时延。
- 真实 PostgreSQL `pg_terminate_backend` 在 run/outbox 提交前断开事务，两者都没有残留。
- 阶段事务提交后重放无重复；稳定问题冲突拒绝；同一决定只保存一份；决定保存后执行器失联不重新提问。
- 过期心跳、旧 epoch 写入、取消后的写入拒绝；两个 reaper 只有一次撤销；持锁续租不会被恢复扫描抢占。
- 调用预留跨接管累计，未知结果不退款；超时最多一次短暂重试；结构化失败不发布草稿或成功；取消后允许已有收据结算。
- API：跨用户 404、恢复幂等、不同决定冲突、schema 错误、JSON/SSE 一致、补读游标、历史过期 410。
- 浏览器：测试账号的合成运行，读取已保存事件、提交接受决定、后台完成后显示已完成。截图位于本机 Codex visualization 目录。新版 v2 还展示了草稿正文；持续 dispatcher 自动接收浏览器决定并完成后台恢复，无需手工投递。该账号与数据不代表用户真实职业评估。
- Ruff 通过；mypy 检查 40 文件通过；Alembic check 无差异；新数据库 `careerforge_s02_verify` 从空库完整升级成功；Vue/TypeScript/Vite 构建通过。

迁移 head：`f4e95ad00102`。官方 uv GHCR Python 镜像按 digest 固定，解决先前 Docker Hub 认证端点超时；实际构建并启动了 migrate/worker/dispatcher。前端与 API 容器完整组合仍未验收。

## 故障映射与适用范围

| 用例 | 当前证据 | 边界 |
|---|---|---|
| F01 | 真实 DB 连接中断，run/outbox 同时回滚 | 公开业务创建接口的幂等复用在 S03 继续接入 |
| F02 | broker 发送异常，outbox 保留并重新投递 | 不是长期网络分区实验 |
| F03 | 真实 Redis 接受消息后注入标记异常，再次投递 | 未杀 dispatcher 整个 OS 进程，故障点等价由函数异常注入 |
| F04/F05 | SIGKILL 真实 Worker 子进程，复用已提交草稿 | 当前业务副作用是内部 StageArtifact，不代表后续所有业务模块已验证 |
| F07 | 等待释放租约、决定唯一、响应冲突和版本校验 | 未穷尽所有提交前并发组合 |
| F09 | 旧身份拒绝、DB 时间、双 reaper、续租不抢占 | S08 扩大延迟心跳与网络组合 |
| F14 | 持久有序补读、JSON/SSE 相同、浏览器重新连接去重 | 尚无自动化浏览器断网故障套件 |

不将本阶段当作完整 V1。真实供应商访问仍关闭；金额/token/累计运行时长预算须在开放真实模型前补齐，目前演示仅启用调用次数额度。业务输入撤销传播、物理清理、其他五类工作流、质量评测、备份恢复及 S11 作者验收仍待后续阶段。CI 配置存在但尚未在远端 GitHub 执行。Vite 仍提示 ElementPlus 主 bundle 大于 500 KB。

## 本地复现

```powershell
docker compose --profile worker up -d --build worker
$env:TEST_DATABASE_URL="postgresql://careerforge:careerforge_dev@127.0.0.1:55432/careerforge_test?connect_timeout=5"
$env:TEST_REDIS_URL="redis://127.0.0.1:56379/0"
$env:RUN_LINUX_QUEUE_TESTS="1"
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
```

持续 dispatcher：`docker compose --profile dispatcher up -d --build dispatcher`。故障测试期间不要另开持续 dispatcher，避免它提前投递测试特意保留的通知。完整部署与真实模型费用另行验证，当前无需真实模型授权。
