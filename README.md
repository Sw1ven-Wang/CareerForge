# CareerForge

将岗位要求转化为学习任务、实践成果与可信证据的个人 Agent 工作台。

当前：S00 环境与离线模型契约已验证；S01 最小工作台、S02 持久化运行内核、S03 来源导入与岗位要求报告已实现并离线验证（80 项测试，含 Linux Celery 队列与杀进程恢复）。工作流采用 Python 显式阶段函数，见 `docs/decisions/0002-explicit-workflows.md`。六模块应用尚未完成，阶段记录见 `docs/progress/`。
产品基线见 `docs/00_README.md`，按 `docs/03_IMPLEMENTATION_ROADMAP.md` 顺序推进。

## 开发验证（PowerShell）

```powershell
uv sync --project backend --python 3.12 --locked
docker compose up -d --wait
$env:TEST_DATABASE_URL="postgresql://careerforge:careerforge_dev@127.0.0.1:55432/careerforge_test?connect_timeout=5"
$env:TEST_REDIS_URL="redis://127.0.0.1:56379/15"
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
uv run --project backend ruff check --config backend/pyproject.toml backend/src backend/tests scripts
uv run --project backend mypy --config-file backend/pyproject.toml backend/src/careerforge
uv run --project backend python scripts/check_environment.py
uv run --project backend python scripts/validate_manual_case.py
```

模型测试使用 synthetic fixture 和 MockTransport，不读取真实 API 配置，不发生模型费用。
缺少基础设施环境变量时 infrastructure 测试明确跳过，不算验收通过。
Compose 只绑定回环地址，示例数据库密码仅用于本地隔离开发，禁止作为私有部署密码。
个人材料快照在 `private/`，默认不进入 Git 或公开证据包。

## 启动当前工作台

```powershell
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend uvicorn careerforge.api.app:app --host 127.0.0.1 --port 8000
# 新终端
npm.cmd --prefix frontend run dev
```

浏览器访问 http://127.0.0.1:5173；使用自己的邮箱与新密码创建本地账号。当前仅支持本机开发，不用于公网部署。

容器版配置已提供：`docker compose --profile app up --build`，入口 127.0.0.1:8080。此命令尚未做完整镜像构建验证，不算部署验收。


## S02 后台执行与运行记录

```powershell
docker compose --profile worker up -d --build worker
# 日常使用可另启持续投递器；故障测试期间不要同时启用。
docker compose --profile dispatcher up -d --build dispatcher
# 在已启动 Linux Worker 的环境中验证真实队列：
$env:RUN_LINUX_QUEUE_TESTS="1"
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
```

`/runs` 展示本用户持久化运行、待处理决定及事件。S02 合成演示只从测试代码创建；S03 已提供岗位分析业务创建入口。详情见 `docs/progress/S02_REPORT.md`。当前未开放真实模型调用，模型质量与完整 V1 尚未验收。

## S03 岗位要求分析（离线）

启动 API、前端、worker 与 dispatcher 后，打开 `/market` 选择目标固定版本及材料快照。页面支持附件导入、原文引用、明确的岗位分母、人工更正与重复判定。统计由程序计算；FakeModel 仅验证输出契约，不代表真实模型质量。

已有本地演示账号可生成三份已保存官方 JD 与 joblearn 静态清单的报告：

```powershell
uv run --project backend python scripts/seed_s03_demo.py --email browser-s01-20261012@example.test --joblearn E:\Coding\Project_Data\java_data\joblearn
```

阶段结果和边界见 `docs/progress/S03_REPORT.md`；可复算输出见 `docs/samples/S03_OFFLINE_REPORT.json`；提取评分约定见 `evals/README.md`。S04 尚未开始，进入下一阶段所需的用户解释检查和真实模型质量验证仍待完成。
