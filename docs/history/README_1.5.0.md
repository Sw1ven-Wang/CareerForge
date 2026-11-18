# CareerForge v1.5 — CLI 实现与测试包

一个围绕职业需求和真实代码学习的 CLI 多 Agent 系统：自然语言路由、树状研究、固定源码分析、独立审核、证据检索、教学反馈和受控编程实践。

> **交付状态：离线业务流程可运行；不等于整个 v1.5 产品已验收完成。**
>
> 本包是依据 v1.5 规格构建的**独立实现**，不是原 GitHub 仓库的完整原地升级。没有携带原 Vue/Tauri 与所有旧接口；不支持把旧数据库直接迁入。务必使用独立虚拟环境和新数据库。
>
> PostgreSQL/pgvector、Redis/Celery、真实模型/Embedding/Reranker、Tree-sitter、Pi SDK 和 Docker 的外部集成尚未在本次环境中验收；还存在 `docs/KNOWN_GAPS.md` 所列实现缺口。不得把本包描述成“全部需求已完成且生产全链路通过”。

## 1. 已落入代码的主要流程

```text
CLI / HTTP 自然语言输入
  → Coordinator：范围解析、规则快速路径、必要的模型意图识别
  → 已有知识：任务感知检索 → EvidencePack → Tutor / Answer Guard → SSE
  → 新知识：JD 提取 / 指定仓库 → Research Tree → 固定快照
           → Analysis 子任务 → 独立 Review 子任务 → Publisher
  → 反馈：个人偏好 / Dispute / Experience
           → 受限策略候选 → Holdout 配对评测 → 显式晋升 / Canary / 回滚
  → 实践：只读参考 → 自己的工作区 → Pi 待审补丁 → APPLY
           → 固定代码和环境 → 独立 RUN → Docker 回执 → 条件化自验
```

模型不能自授预算、修改知识发布状态、扩大源码权限或执行宿主 Shell。没有凭证时返回配置错误或知识缺口，**不会自动改用假模型**。

## 2. 目录

| 路径 | 内容 |
|---|---|
| `src/careerforge/api.py` | FastAPI 接口、鉴权与 SSE |
| `src/careerforge/cli/main.py` | Typer/Rich 终端客户端 |
| `src/careerforge/services/` | Runtime、研究、取证、发布、检索、教学、进化、实践 |
| `src/careerforge/adapters/` | 模型 HTTP、GitHub、Redis、Pi JSONL、Docker 执行端 |
| `src/careerforge/models.py` | 关系模型及数据库约束 |
| `alembic/` | **新库**初始迁移；检测旧库时拒绝 |
| `coding-agent/` | 受控 Pi SDK Node 运行器；不包含已安装依赖 |
| `deploy/`、`compose.yaml` | 生产部署与隔离沙箱构建文件，尚未实机验收 |
| `tests/` | 显式替身、集成与反向验收用例 |
| `validation/` | JUnit、覆盖率、安装、环境和流程原始记录 |
| `docs/specifications/` | 本次依据的原始 v1.5 与 v1.2.1 文档 |
| `docs/REQUIREMENTS_MATRIX.md` | 需求—代码—测试—剩余缺口对应表 |
| `docs/KNOWN_GAPS.md` | 未完成和未验证事项，不能忽略 |
| `dist/` | 本次构建的 Python wheel |

## 3. 安装与离线测试

需要 Python 3.12 或 3.13。以下命令在项目根目录执行：

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]' pytest-cov
python scripts/run_tests.py
python scripts/demo_offline.py
```

`run_tests.py` 默认禁用无关的全局 pytest 插件并保存退出状态。`--coverage` 开启覆盖率，`--label` 可区分报告，后接测试文件可分批运行。完整命令和本次实际结果见 `docs/VALIDATION.md`。

`demo_offline.py` 明确使用 `mode=test`、隔离临时 SQLite 及测试供应商。它真正调用业务服务、建立研究树、审核、发布、问答和反馈，但材料与模型返回是手写合成数据。**演示输出不是 GitHub 实时研究，也不是实际模型质量证明。**

## 4. 不连接生产基础设施的本地开发入口

开三个终端，使用相同的用户状态目录：

```bash
# 终端一：只需初始化一次，不会覆盖已有令牌
careerforge init --development
careerforge serve --development

# 终端二：明确的本地持久队列执行器，不冒充 Celery
careerforge worker --development

# 终端三
careerforge chat --evidence-only
careerforge tasks
```

新库没有预灌入“已审核知识”，因此第一次无材料提问返回知识缺口是正确行为。开发模式只把数据库、缓存与队列替换为明确的本地实现，**不替换真实模型**。

要启用实际知识构建，先按 `.env.example` 设置模型环境变量并重启 API/Worker，再执行：

```bash
careerforge authorize --calls 100
careerforge build '分析 Tool Calling 的实现' --repo 'https://github.com/OWNER/REPO' --calls 24
careerforge tasks
careerforge chat
careerforge sources
careerforge feedback '先展示代码，再讲概念' --consent
careerforge improve status
```

`OWNER/REPO` 应替换为有权读取、适用许可且大小符合限制的真实项目；不是内置已验证样例。`authorize` 的调用次数包含规划、审核、Embedding 和 Reranker，**不是货币账单硬封顶**。该命令默认授权本实例处理公开源码、私人源码、JD 和学习状态，请先检查 provider 与数据外发范围；HTTP 接口可指定更窄的类别。

`.env` 仅供 Docker Compose 自动读取；本地 Python 进程读取系统环境变量。不要把密钥提交到 Git 或放进日志。完整配置、部署和命令说明见 `docs/DEPLOYMENT.md`。

## 5. 三个核心能力的实际边界

**任务感知检索。** 定位任务优先符号/路径；概念、实现与架构解释选择关系、FTS、向量等通道；比较按项目与 Facet 分组。所有缓存命中返回前仍检查权限、发布状态、来源和依赖。生产 pgvector SQL 已写入，但本次测试的向量通路使用明确的测试向量，不代表语义召回率。

**条件委派。** 简单导航不调用模型；知识充分时不重新调研；新知识必须走审核。根任务最多创建两个子 Run（批量 Analysis、批量 Review），研究树是持久逻辑树。SQL 数据是权威，Outbox 投递、租约和模型收据负责恢复。

**验证后发布。** 能优化受限的教学布局、路由规则、检索权重和研究 Frontier 排序。未经服务端计算的 Holdout 结果不能晋升；组泄露、基线变化、权限变化或样本不足会阻断。研究回放仅证明相同冻结观测下的调度效果；修改查询生成语言需另外的真实研究评测。任意 Prompt 自动改写、真实教学收益与全系统自动进化未完成验收。

## 6. 实际验收与交付界限

请先看 `DELIVERY_REPORT.md`。本次验收分开记录：

- 离线业务、权限、预算、撤销、缓存、SSE 和 CLI 的工程测试；
- wheel 构建及独立目录安装验证；
- 生产依赖预检，缺失时显式失败；
- 尚未实现的规格与尚未验证的外部效果。

没有配置真实模型、没有运行 Docker、没有真实 PG/Redis 的结果不会被标成通过。旧仓库文件和数据库应单独保留，**不要覆盖**。
