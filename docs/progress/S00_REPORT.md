# S00 阶段报告

日期：2026-10-12。开始状态为只有原始文档、无代码、无 Git。

## 范围与结果

完成项目文档归档、Python 3.12 隔离环境与 uv.lock、PostgreSQL/Redis Compose、模型统一契约、单供应商适配雏形、FakeModel、离线 fixture、环境盘点脚本、人工样例与哈希验证脚本。

真实模型验证按用户明确要求暂缓；未发模型请求，费用未发生。HTTP 请求与流式解析仅用 MockTransport/合成夹具验证，真实供应商流式传输、工具调用及 schema 支持仍待验。

人工链条见 ../samples/manual_walkthrough.md。三份真实百度 Agent JD 中两份实习、一份社招；不将社招混进实习分母。参考 LangGraph 固定 commit、许可证与已读文件已保存。joblearn 个人材料只读快照在 private/，不提交公开仓库；未核验个人贡献和该项目实际运行。没有材料证明某项能力时保留 unknown。

## 实际命令及结果

- uv sync --project backend --python 3.12：成功，80 个解析包（含项目）锁定。
- docker compose up -d --wait：PostgreSQL/Redis 健康。
- pytest 显式配置 backend/pyproject.toml：离线 26 passed。首次未显式配置导致异步用例失败，修正执行入口后通过。
- PostgreSQL/Redis/checkpointer 集成：3 passed；checkpoint 在新连接读取 value=2。
- mypy backend/src/careerforge：8 source files 无问题。
- scripts/validate_manual_case.py：来源哈希、原句区间、commit、许可证和 unknown 边界通过。
- 最终统一回归和静态检查记录见同目录 S00_TEST_RESULTS.xml。

## 结论

环境与人工业务样例有明确结果，可进入 S01 工程骨架与 FakeModel 路线。S00 不代表模型效果或学习发生已验证。Celery Linux Worker、业务 Alembic 迁移分别在 S02/S01 验证；真实模型及作者实际迁移成果仍为后续门槛。未创建应用前端或宣称六模块完成。

## 后续架构修订

用户随后明确选择无 LangGraph 的显式工作流，见 ADR 0002。S00 原始 29 项实验保留为历史事实；当前依赖及基础设施测试已移除 checkpointer，当前回归为 40 项（含 S01 的 12 项）。
