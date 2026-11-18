# Docker 构建、隔离验证与本次证据

## 1.6.0 本次状态（2026-11-02，仅 dot 工作空间）

**真实 Docker 镜像构建与容器运行没有通过：当前工作空间缺少可用 daemon，rootless 前提也不满足。** 没有切换到用户电脑、启动外部付费环境、修改宿主安全策略或把原生服务冒充 Docker。

| 项目 | 本次结果 | 证据 |
|---|---|---|
| 生产/测试 Compose 配置 | 官方 Compose 解析通过；不等于镜像可构建 | `validation/current/docker-attempt/` |
| 可移植 Docker 工具 | Docker CLI 29.9.2、Compose 5.6.1、rootlesskit 3.3.0 已实际运行 | `rootless-prerequisites.log`；Compose release SHA256 核对通过 |
| Rootless 前提 | 实际执行失败：`newuidmap` 不存在；`NoNewPrivs=1`、capability bounding set=0；无 `/sys/fs/cgroup` | 同上 |
| Docker daemon | `docker info` 失败；验证器退出 2，`blocked` | `docker-attempt/final-harness/docker.json` |
| 后端/沙箱镜像构建、容器内测试、容器隔离 | **not_run**，没有用假结果替代 | 同上 |
| 真实 PG/pgvector/Redis/Celery（非 Docker） | 通过；PG 17.11、pgvector 0.8.0，分别用 Redis 8.0.2 与 7.0.15 复验；模型收据 0 | `native-infrastructure/production-pass-1/production.json` 与 `production-redis7/production.json` |
| 非空验证库保护 | 第二次运行在迁移前被拒绝，预期退出 2 | `native-infrastructure/reuse-guard/guard-result.json` |
| 新部署/适配器回归 | 本机 Python 离线合同测试通过；其中模拟 Docker transport 明确不是真实 Docker | `validation/current/deployment-handoff.xml` |

单 UID 的 `unshare -Urnm` 探测实际成功，因此没有把“用户命名空间完全禁用”写成失败原因；它不等于 Docker 所需的多 UID 映射与资源控制已就绪。官方 [Rootless 前提](https://docs.docker.com/engine/security/rootless/)要求映射工具与 subordinate IDs；官方[资源限制说明](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)指出不满足 cgroup 条件时相关限制可能被忽略。本实现明确拒绝缺少内存、进程、CPU 或 seccomp 能力的 daemon，不降级运行不受限练习。

原生服务由官方 Debian 签名包索引和包 SHA256 校验后解包到工作空间；未做系统级安装，服务只绑定回环地址，各次测试自行启动、停止并移除其新建数据目录。原生版本、初次失败尝试、生命周期和具体限制见 `validation/current/native-infrastructure/README.md`。这些通过结果不证明 Docker 镜像/Alpine 行为、持久化故障恢复、真实模型质量或全量产品验收。

1.6.0末版Compose/daemon复核以 `validation/v16/final_verified/docker/docker.json` 为准；此前rootless真实启动证据留在 `validation/current/docker-attempt/`。表中旧native报告是1.5.1历史证明，1.6.0原生结果以 `validation/v16/final_verified/summary.json`、`native_main.xml`、`native_governance.xml`、`production/production.json` 为准。历史通过不替代本版重新执行。

## 以后在具备 Docker 的已授权环境运行

需要 Linux Docker Engine、可用 cgroup v2、Compose v2+、Python 3.12、镜像及包仓库网络。先安装项目的验证依赖：

```bash
python -m pip install -c constraints-python312-linux.txt '.[dev,production]'
python scripts/verify_docker.py --confirm-docker-test
```

默认建立唯一 `cf-validation-<随机值>` Compose project，并将报告放入新的 `validation/local/docker-<随机值>/`。也可使用新的 `--output-dir`。同名已有 `docker.json` 会被拒绝覆盖。

验证顺序：

1. 解析专用 `compose.test.yaml`，确认 daemon 可用。
2. 分别构建 `runtime` 生产镜像、`validation` 测试镜像及独立 Python 沙箱镜像。
3. 在容器内运行明确标注的离线测试；不将 fixture 测试当作外部集成。
4. 启动独立、无发布端口的 PG/Redis；在全新库运行真实基础设施验证。
5. 使用不含 dev 依赖的 `runtime` 镜像显式初始化临时客户端，确认重复初始化被拒绝，再启动 API/Worker/Dispatcher，检查 API health、保留凭据的 CLI 和 Celery ping。
6. 固定实际沙箱 image ID，检查非 root 作业、只读根目录/输入、无网络、cgroup 限额、安全文本导出、非零退出与 30 秒超时。
7. 无论成功失败，保存日志并清理**本次唯一 project**的容器、网络、数据卷与唯一镜像 tag；不执行全局 prune。

测试 Compose 不加载生产 `.env`、不挂载 Docker socket、不用 privileged、不引用生产卷或 host port，不向模型发送请求。它使用专用临时数据库口令；不是生产凭据方案。基础设施验证器使用独立随机 Celery queue，避免验证 worker 消费共享 broker 上的正常任务。

`.github/workflows/checks.yml` 包含同一真实 Docker 测试入口。**本次未提交或触发远程 CI**，因此不能把 CI 配置存在写成 CI 已通过。

## 实现修复

- 生产 Compose 将 API/Worker/Dispatcher 排在成功迁移之后；初始化账号仍需显式 `initialize`，不会隐式重建令牌。
- 数据库密码由可信 entrypoint 进行 URL 编码；包含 `@:/?#%` 的密码不会被直接拼坏连接 URL。
- 后端以 uid 65534 运行，根文件系统只读，临时写入与状态卷分离；API/Worker 不获取 Docker 权限。
- Dockerfile 分离 `runtime` 与 `validation` target；普通 `docker build` 默认得到生产 target。Python 使用当前 CPython 3.12/Linux 已测试版本约束；Node 使用 `npm ci` 与 package-lock。
- 基础镜像标签及 apt 源不是完整 hash 锁定；版本约束不等于跨平台或完整供应链可复现保证。`NODE_IMAGE`、`PYTHON_IMAGE` build args 可由部署方固定到经过验证的摘要。
- `.dockerignore` 排除验证日志、`.env*`（保留示例）、VCS、虚拟环境及 Node 安装目录。
- 沙箱 supervisor 只允许在容器内作为 root PID 1 启动；作业降权至 65534 并清空有效能力，停止并回收所有子进程后再取回文件；可信 PID 1 保留 `DAC_READ_SEARCH` 以读取作业新建的 0600 文件及 0700 目录；导出排除软链接、私密路径、二进制和超限文本。
- Host adapter 严格校验回执字段与类型，流式限制 Docker stdout/stderr，异常清理不覆盖原错误；清理不能确认时不把执行标为成功。

这仍是隔离烟雾测试和工程加固，不是恶意代码隔离的独立安全认证。不要运行 `deploy/sandbox_entry.py` 来在宿主机代替 Docker，也不要添加 `--init` 改变它的 PID 1 合同。
