# 安装、部署与操作

> Docker 镜像与容器运行在本次 dot 工作空间中仍被环境前提阻断；真实原生 PG/pgvector/Redis/Celery 已另行测试通过，不等于 Docker 通过。具体证据与一键验证入口见 `DOCKER_VALIDATION.md`。先阅读 `KNOWN_GAPS.md`，只使用新库、新目录，不覆盖原 CareerForge。

## 1. 运行模式

| 模式 | 数据库/缓存/队列 | 模型 | 用途 |
|---|---|---|---|
| `production` | PostgreSQL + pgvector / Redis / Celery | 真实已配置供应商 | 目标部署；缺依赖就失败，无隐式本地替代 |
| `development` | SQLite / MemoryCache / 本地持久 Run 执行 | 仍为真实供应商，未配则报错 | 无基础设施时检查本机 API/CLI，不代表 PG 并发语义 |
| `test` | 测试夹具指定 | 仅此模式允许注入 synthetic providers | pytest 与明确离线演示，不用于用户正式知识 |

## 2. 目标生产部署

需要 Docker Linux containers、Compose、能访问包仓库和镜像仓库的网络。API/Worker 镜像提供 Python 3.12 与 Node 24。模型调用需有效的供应商配置；GitHub 代码搜索还需要适用 Token。

```bash
cp .env.example .env
# 手工编辑 .env：更换 POSTGRES_PASSWORD，配置模型等；不提交此文件。
docker compose up -d postgres broker cache
docker compose build api
# migrate 是默认 one-shot 服务；后端等待其成功，不会在空 schema 上提前启动。
docker compose run --rm initialize
docker compose up -d api worker dispatcher
```

数据库 URL 在容器的可信 entrypoint 中编码组装，密码中的 URL 保留字符不会直接进入连接串。后端根文件系统只读，`/tmp` 使用有界 tmpfs，客户端状态使用独立 volume。

`initialize` 只初始化一次，在专用 volume 写入 `client.json`；不会输出令牌。正常再次部署不要重复创建账号。命令失败时检查日志和环境，不要删除已有数据卷或把失败状态手改为成功。

在 API 容器内运行 CLI（使用同一 volume 内客户端配置）：

```bash
docker compose exec api careerforge chat
docker compose exec api careerforge tasks
docker compose exec api careerforge authorize --calls 100
```

也可在自己的独立 venv 中安装源码包，把 `client.json` 安全复制到 `CAREERFORGE_STATE_DIR` 指定目录，然后连接回环 API。该文件包含 bearer token，不能公开、截图或复制到代码仓库；文件应只对本人可读。

默认只把服务端口绑定 `127.0.0.1`。公开部署的 TLS、反向代理、访问控制、限流和系统密钥管理不包含已完成安全认证。

## 3. 模型与检索配置

| 配置 | 用途 |
|---|---|
| `OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME` | Chat Completions 兼容端点和真实模型；无内置默认凭证 |
| `CAREERFORGE_ROLE_MODELS` | JSON 对象，按 extract/planner/reflector/analysis/reviewer/tutor 等角色选模型；同一配置供应商 |
| `CAREERFORGE_STRUCTURED_OUTPUT` | auto/json_schema/json_object；在调用前选输出模式，响应仍本地校验；失败不自动付费重试 |
| `EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL` | `/embeddings` 兼容端点；实际请求包含 dimensions，供应商必须支持 |
| `EMBEDDING_DIMENSIONS` | 输出向量维度，必须与所用模型兼容，8–2000；不同模型配置不能混用旧向量 |
| `RERANK_URL / RERANK_API_KEY / RERANK_MODEL` | 可选重排端点，返回 results/index/relevance_score 格式 |
| `GITHUB_TOKEN` | GitHub 访问与代码/文档搜索鉴权，不传给模型 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 可选 OTLP HTTP traces 地址 |

不要仅因为供应商号称兼容就假定全部 Schema、工具、计量和模型参数都可用。先使用受限额度验证实际接口；环境变量改变后重启 API/Worker。旧 Run 冻结模型配置，不能用新配置静默重放。

`CAREERFORGE_ROLE_MODELS` 示例是配置形状，不是可直接使用的模型名称：

```json
{"planner":"configured-fast-model","analysis":"configured-code-model","reviewer":"configured-review-model","tutor":"configured-teaching-model"}
```

## 4. 主要 CLI 操作

```bash
careerforge --help
careerforge chat --once '我想学习 Agent 的 Tool Calling'
careerforge chat --new --auto-build
careerforge build '根据JD寻找学习项目' --jd-file ./jd.txt --calls 24
careerforge build '讲解该项目架构' --repo https://github.com/OWNER/REPO --calls 24
careerforge tasks --run RUN_ID
careerforge tasks --run RUN_ID --cancel
careerforge sources --turn TURN_ID
careerforge feedback '这个条件解释不准确' --turn TURN_ID --correction
careerforge gaps
careerforge disputes
```

`--auto-build` 只对新建会话设置自动补建偏好，仍需要有效 funding。学习问题不要求用户指定 project/topic 模式。`--evidence-only` 使用已经审核的精确材料，不调用模型；没有材料时如实返回缺口。

导入本人选择的本地**参考源码**：

```bash
careerforge import-source ./reference-subdir owner/repository 0123456789abcdef0123456789abcdef01234567 --license-id MIT
```

替换为实际身份、固定 SHA 和有依据的许可证。默认 `unknown` 不自动按 MIT 放行。此入口是用户提交副本，不保证声明的 SHA 已与远端逐文件一致；元数据会保留导入来源。不会执行该目录的项目代码。

## 5. 编程工作区与 Docker 执行端

当前支持的执行模式是 `server_snapshot`：参考源码只读；自己的工作区文本保存在服务器；批准执行时冻结当前文件 manifest 与镜像 ID。**本机运行沙箱 supervisor，不代表在本机直接执行练习代码。**

先建立不可变沙箱镜像：

```bash
docker build -f deploy/Dockerfile.sandbox -t careerforge-sandbox:local .
docker image inspect --format '{{.Id}}' careerforge-sandbox:local
```

把实际输出的 `sha256:...` 写入 API/Worker 与可信执行端的 `CAREERFORGE_SANDBOX_IMAGE`，重建配置后启动服务。可变 tag 会被拒绝。

可信执行端在拥有 Docker 权限、能访问同一数据库的主机运行：

```bash
careerforge sandbox-supervisor
```

API 与普通 Worker 不挂载 Docker socket；不要为了省事把它暴露给模型工具。监督端只领取已确认的 CommandAuthorization，然后用无网络、只读根文件系统、受限资源的容器执行。需要真实部署测试才可声称隔离有效。

```bash
careerforge workspace new --publications PUBLICATION_ID
careerforge workspace save WORKSPACE_ID main.py ./my_main.py
careerforge workspace code WORKSPACE_ID '根据参考实现完善这个练习' --files main.py --calls 2
careerforge workspace show WORKSPACE_ID
careerforge workspace apply WORKSPACE_ID PATCH_ID
careerforge workspace run WORKSPACE_ID '["python","-B","main.py"]'
```

`apply` 展示 before/after 并确认，`run` 再展示命令、代码版本与执行环境并单独确认。命令成功不会自动改变用户能力或参考知识事实。当前容器只提供 Python；不承诺 npm/Go/Rust 构建。

在源码之外安装 wheel 时，Pi 依赖需另外准备：

```bash
npm install --prefix coding-agent --ignore-scripts --no-audit --no-fund
# 设置 CAREERFORGE_PI_RUNTIME_DIR 为该 coding-agent 目录的绝对路径
```

Pi SDK 版本1.3.0与 Node 至少22.19，建议使用镜像中的 Node24；本次已执行 clean npm ci 与真实 SDK 循环；推理为明确合成fixture，不是付费模型质量验收。

## 6. 新控制面与真实回放

```bash
careerforge learning --help
careerforge replay --help
careerforge taxonomy --help
careerforge maintenance --help
careerforge shared --help
careerforge index --help
careerforge privacy --help
careerforge legacy-export --help
careerforge legacy-import --help
```

这些薄客户端接受显式JSON合同，服务端验证funding/source/role/data_class、idempotency/revision和确认hash；使用`docs/openapi.json`查字段。不要把占位ID、source SHA或示例预算照抄到实际工作。资金`--source`可重复指定固定来源；省略来源保留旧“不限制来源”语义，**空角色/类别仍拒绝全部**。

学习计划先proposal再精确version/revision采纳；独立尝试、观察来源、外部帮助和运行结果分别记录。复习建议不发送外部提醒。

`replay teaching-dataset/research-dataset/typed-dataset`登记冻结语料，`replay attest`由实际认证的独立标注者明确认证，`replay run`启动有预算的真实服务双臂回放。生产Gold需要两名独立标注者及开发集基线校准；未知研究查询、样本不足、环境漂移均保持inconclusive。旧`improve evaluate`配置分数在非test模式不能绕过真实回放晋升。真实Gold、标注专业资格与真实效果需运营方建立，不能自动编造。

`maintenance schedule`创建默认禁用计划，显式toggle同意后只扫描已导入新快照，模型花费另行授权；公共shared任务只有系统预算和固定公共源，私人任务不自动合并。

隐私删除必须先export/propose查看范围，再用proposal nonce/hash确认不可逆清理；有保留例外则拒绝操作并披露。完成后新私人域和新资金授权接续，旧客户端conversation/funding清除。详见`STREAMING_AND_PRIVACY_V16.md`。

## 7. 基础设施验证

```bash
python scripts/doctor.py --production
# 必须先人为创建一个全新的、可丢弃的验证库，并配置对应环境：
# CAREERFORGE_DATABASE_URL=.../careerforge_v15_validation_<unique>
python scripts/verify_production.py --confirm-infrastructure-test
# 如需真实 Docker 构建、容器内离线/基础设施测试和隔离烟雾测试：
python scripts/verify_docker.py --confirm-docker-test
```

基础设施验证脚本在迁移前拒绝非空数据库；实际检查 PG vector SQL、跨客户端 Redis singleflight 和独立随机队列的 Outbox→Celery→Worker→PG 投递，清除模型配置并确认模型收据为零。默认报告写入验证器声明的新目录；离线与Docker验证默认 `validation/local/`，不覆盖历史证据。它不替代真实 provider、Gold、Pi 和 Docker 专项验收。非零退出必须保留，不能忽略后宣称整体通过。

## 8. 维护

`GET /health/ready` 只反映 API 所见数据库和模型配置状态，不证明 Worker、Redis、provider、索引或沙箱全部健康。管理员 `/metrics` 返回 Prometheus 指标；正式部署应分别监控各依赖。

任何备份/恢复先在隔离新库进行，恢复后保留知识撤销、预算未知收据和策略版本。不要清空未知收据、回写旧知识审核通过或恢复已撤销 Publication 为 active 来排障。
