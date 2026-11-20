# CareerForge — 对话式项目学习助手

当前快照更新于 2026-11-20。输入项目地址和学习目标，由协调 Agent 组织源码分析、独立审核、知识发布和教学回答。点击回答中的引用，可在右侧查看固定提交的源码与行号。

## 本地流程验收

Codex 订阅模型完成了从 JD 导入到练习反馈的用户流程。[2026-11-20 验收记录](docs/本地流程验收_20261120.md)。

## 2026-11-18 架构问题验收

输入 `https://github.com/earendil-works/pi   我要学这个的架构`，真实模型验证完成项目导入、分析、独立审核、入库和架构概览讲解。最终 `task_satisfied=true`；6 个源码导航目标验证有效，页面引用点击实测通过。[验收摘要](docs/Pi原句闭环验收_20261118.md)。

修复包括单句架构问题的分面检索、原回答缺口传递、分析读取分页，以及架构快照漏采大型会话实现文件。入库与回答的完成状态分别核验，局部知识不会自动等同于完整回答。

## Windows 本地启动（Python 3.12）

在仓库根目录的 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
$env:CAREERFORGE_STATE_DIR = Join-Path $PWD 'private\local-runtime'
.\.venv\Scripts\careerforge.exe init --development
```

在当前终端配置自己的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 环境变量，然后启动：

```powershell
.\run-web.ps1 Start -Model '你的模型名称'
.\run-web.ps1 Status
# 结束使用时：
.\run-web.ps1 Stop
```

打开 <http://127.0.0.1:8001/#chat>，在对话框输入项目 URL 和学习目标。模型调用会消耗所配置服务的额度，源码与模型服务需网络可用。服务仅监听本机回环地址。

`init` 仅用于首次初始化，不覆盖已有账号。本仓库包含源码和文档，不包含开发者的密钥、会话、知识库数据库或模型调用日志。首次启动的知识库为空，需通过对话导入项目。已有数据库请沿用自己的运行目录并遵循迁移说明，不要用旧快照覆盖运行库。

## 目录

- `src/careerforge/`：API、协调和教学工作流、检索、审核及知识发布。
- `src/careerforge/web/`：本地对话页面及源码侧栏。
- `tests/`：离线回归与合同测试。
- `coding-agent/`：受控编码工作器。
- `scripts/`、`alembic/`、`deploy/`：验证工具、数据库迁移及部署支持。
- `docs/`：设计和历史文档，历史报告仅代表对应版本。

## 验证范围

本轮相关回归：分页/预算/构建 98 项、架构快照 3 项、项目对话 14 项、缺口与覆盖 33 项、新增缺口绑定 2 项通过。较宽的 bounded evidence 测试仍有一个旧主题元数据用例失败，未宣称全量测试或生产验收通过。Pi 验收为架构入门概览，不等于全仓实现穷尽或运行过 Pi 项目。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_desk_projects.py tests/test_gap_research_context.py tests/test_tutor_question_coverage.py -q
```

其他模型、项目和网络条件下的结果需另行验证。许可证见 [LICENSE](LICENSE)。
