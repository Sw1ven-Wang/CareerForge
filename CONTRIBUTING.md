# 参与贡献

欢迎通过 Issue 描述问题或提出小范围 Pull Request。请说明复现步骤、预期与实际行为，避免附带个人岗位材料、模型密钥或完整私有日志。

## 开发约定

- 主流程：人工需求 → 技术提取 → 开源调研 → 相关性审核 → 学习计划 → 导学与 IDE。
- 保持 Python 显式工作流、版本化阶段产物和调用预算。新增外部请求不得绕过工具权限或计量。
- 模型、文档、代码和用户材料都是待核对输入。源码事实必须有实际读取的引用，未知不能标为通过。
- 本机补丁必须经过审阅，命令逐条确认；不开放任意宿主 Shell。
- 不提交 `.env`、数据库、私有材料、构建产物或模型原始日志。第三方代码应保留来源和许可证。

## 验证

针对改动运行最小必要检查即可。离线测试不得使用真实模型密钥；需要 PostgreSQL / Redis 的用例应使用独立数据库。

```powershell
uv sync --project backend --python 3.12 --locked
uv run --project backend ruff check --config backend/pyproject.toml backend/src
npm.cmd ci --prefix frontend
npm.cmd run build --prefix frontend
```

后端测试位于 `backend/tests/`。数据库迁移、测试环境和部署限制参见 `docs/operations/`；真实模型验证需要明确的调用数和输出预算，不在普通 CI 中启用。

PR 请描述具体行为变化、验证范围和未解决限制。修改接口时同步导出 OpenAPI 与前端生成类型。
