# CareerForge 1.5.1 · 可靠性与安全加固版

CLI 优先的职业学习系统：任务感知检索、独立知识审核、证据约束回答、受控策略进化和编程练习。

> 基于你提供的 v1.5 独立实现包完善，**不是原 Vue/Tauri 仓库的完整原地升级，也不是完整产品验收完成**。原规格保持原文；剩余范围见 [KNOWN_GAPS](docs/KNOWN_GAPS.md)。

## 本轮完成

- 修复预算空范围扩权、租约/取消迟到写、并发幂等与失败子任务发布等边界。
- 审核证据撤销真正阻断发布；来源、事实、比较和缓存/历史回答重新校验固定身份与当前权限。
- 命令批准绑定文件、版本、argv 与镜像；旧批准、旧回执和失效补丁不能越界复用。
- 严格 Pi 协议与服务端模型上下文，新增 CLI `resume`、可靠断流重放、安全凭证写入。
- 补齐解析器、内存缓存边界和 Redis 原子锁发布；提供依赖锁定与可丢弃 Docker 测试栈。

完整说明：[CHANGELOG](CHANGELOG.md) · [交付报告](DELIVERY_REPORT.md) · [验证结果](docs/VALIDATION.md)。

## 已验证与未验证

| 层次 | 当前结论 |
|---|---|
| 原有与新增离线工程测试 | 已在独立依赖环境执行；最终数量见验证报告 |
| PostgreSQL / pgvector / Redis / Celery | **真实本地进程集成通过**，仅在 dot 工作空间；包含并发、迁移、向量SQL、撤销与消息投递 |
| Tree-sitter / Pi SDK | 真实解析器与实际 SDK 循环已测；Pi 的模型响应仍为明确测试替身 |
| Docker | 官方 CLI/Compose 实际配置解析通过；rootless 启动受环境条件阻塞；**镜像构建、容器运行、隔离验收未运行** |
| 真实模型 / GitHub研究质量 / 人工Gold / 教学收益 | 未验收；没有为本轮测试调用真实付费模型 |

**Native 进程测试不能改名为 Docker 通过；SDK 测试不能改名为真实模型效果通过。**

## 快速验证

推荐 CPython 3.12、Node 24。在新的项目目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-python312-linux.txt '.[dev,production]' pytest-cov
npm ci --prefix coding-agent --ignore-scripts --no-audit --no-fund
python scripts/run_tests.py --coverage
python scripts/demo_offline.py
```

Windows 可使用 `.venv\Scripts\Activate.ps1`；本轮没有执行 Windows 验收。Python 约束是 Linux/3.12 的实际解析版本，不是含分发包哈希的跨平台锁。Node 依赖具有 `package-lock.json`。

`run_tests.py` 默认写入 `validation/current/`，排除 `production/live/sandbox` 标记并移除继承的真实供应商配置。演示使用隔离 SQLite 与明确 synthetic fixtures，不是实时 GitHub 研究。

## 本地开发

三个终端使用相同 `CAREERFORGE_STATE_DIR`：

```bash
careerforge init --development       # 一次性初始化新库与私密 client.json
careerforge serve --development      # 终端一
careerforge worker --development     # 终端二
careerforge chat --evidence-only     # 终端三
careerforge resume                   # 断线后只重放已保存事件，不新建模型调用
careerforge sources
```

新库没有预置“已审核”知识；没有材料时返回知识缺口是正常结果。开发模式不伪装真实模型。实际知识构建需要自行配置模型、检查资料外发范围并显式授权调用预算；调用次数不是货币硬封顶。详见 [部署文档](docs/DEPLOYMENT.md)。

## Docker 验证入口

只在已经具备受支持 Docker Linux daemon、资源限制与 Compose 的隔离测试环境运行：

```bash
python scripts/verify_docker.py --confirm-docker-test
```

脚本创建唯一临时 Compose 项目，构建后端/沙箱、运行容器测试与真实基础设施检查，最后清理自身资源。无 Docker 时明确返回 blocked，不使用宿主 Shell 代替沙箱。`compose.yaml` 是应用部署栈，`compose.test.yaml` 是独立验证栈；两者不可混用。当前 dot 环境的真实阻塞证据在 `validation/current/docker-attempt/`。

## 升级与数据保护

- 原始 v1.5 资料与历史验证放在 `docs/specifications/`、`docs/history/` 和 `validation/`；本轮记录统一在 `validation/current/`。
- 已经由 Alembic 管理的**本包 v1.5 新后端库**，备份后执行 `alembic upgrade head`。旧命令批准不会自动补造授权，必须重新批准。
- 原 v1.5 未标记开发库、原 Vue/Tauri 工程旧库均不能盲目 `stamp` 或覆盖；保留副本，遵循 [升级边界](CHANGELOG.md#数据兼容)。
- `client.json`、`.env`、真实源码/数据库与凭证不要进入交付包或 Git。
