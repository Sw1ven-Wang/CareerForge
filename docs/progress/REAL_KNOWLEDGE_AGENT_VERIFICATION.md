# 知识库学习 Agent：一次受限真实验证

日期：2026-10-13。用户授权一次验证，总计最多 4 次模型调用，每次请求最多 2048 输出 token。没有额外真实调用或 pytest 回归。

## 结论

自主检索、实际读取源码、形成有原文依据的学习说明已验证。原始任务因显示标题字段缺失而失败，已修复 schema 转换，并仅用原任务保存结果恢复可审阅说明；没有追加模型调用，模型解释和摘录未改写，只生成通用显示标题。

严格输出额度验证未通过：四次请求均配置 `max_completion_tokens=2048`，供应商最后一次却报告 2175 completion token。无法仅凭此记录确定是转发参数未生效还是供应商计量口径不同。已增加学习工作流的返回用量检查，检测超请求上限时停止后续模型调用、不自动重试；这不能逆转供应商已发生用量，也不能替代供应商侧硬上限保证。当前未增加未验证的参数组合。

## 本次输入与运行

- 运行：`6ed48cbf-1827-4c05-9986-59893646f046`
- 工作区：`18f0d683-cf61-408e-8d53-80abc710aff3`
- 当前计划：`08cc6b7d-bd02-45c1-9aa8-84be9a7da9a4`，版本 `e2d8df58-70b0-4381-b9f1-90b89a43fff1`
- 目标：`04513ea0-3c50-4db8-ab20-df5ed14dcaeb`，“【模拟计划】W2 模型接口与受控工具调用”。计划标签仍为模拟，但本次模型和代码来源为真实资料。
- 资料：smolagents 快照 `42a3927a-d62b-4e0b-b66e-0a41ab824d01`
- commit：`96f33faaf028479119ec8d34507b47694cf14e34`
- 模型：`gpt-6.1-sol`，推理强度 `low`；4 次真实模型调用，没有重试。

## 实际自主行为

初始检索之后，模型自主构造 3 组英文查询，寻找注册检查、schema、异常及 JSON/超时相关机制；继而读取 `ToolCallingAgent.process_tool_calls`、`ToolCallingAgent.execute_tool_call` 与 `validate_tool_arguments`。最后又查询异常处理相关代码，再生成说明。

共 8 次只读工具操作，实际读取 3 个片段，最终采用 2 个代码引用。未执行导入代码、项目命令或修改 IDE 文件、计划与学习完成状态。

恢复说明 ID：`9dad4d05-ad17-4db8-8ac0-418e3ae7afc5`。3 条原文摘录全部逐字匹配，文件和绝对行号由后端计算：

| 学习点 | 原文定位 |
| --- | --- |
| 工具注册检查 | `src/smolagents/agents.py:1463–1468` |
| 参数 schema 检查 | `src/smolagents/tools.py:1385–1388` |
| 参数校验异常包装 | `src/smolagents/agents.py:1475–1481` |

说明明确保留非法 JSON 解析、超时与只读权限白名单未覆盖的边界，并提供阅读顺序、迁移练习和自检问题。原文校验不等于解释语义或运行效果已认证。

## 发现与修复

1. `strict_schema()` 原本在所有字典中递归删除 `title`，误将 `properties.title` 当作 schema 元数据，供应商因此没有收到学习点标题契约。现在区分 schema 节点和命名字段映射，保留 `properties` / `$defs` 等映射中的字段名，同时继续去除 schema 元数据、转换 oneOf。由实际 LearningGuide schema 确认 `LearningPoint.title` 已保留且 required。
2. 增加只修复显示标题的兼容解析：缺少标题时补 `学习点 N` 并记录修复，不改解释、引用 ID 或摘录；其余契约与原文校验继续严格执行。
3. 增加 `PROVIDER_OUTPUT_OVER_LIMIT` 检测与页面原因提示。原始模型响应和用量先保存，以便诊断，随后停止工作流，不再追加模型调用。
4. 本次仅从保存的真实响应恢复页面说明，增加 `local_recovery:display_title` 派生产物与恢复事件。原始 run 的 `failed` 状态没有改写为成功；说明显示 recovered、显示标题补齐和输出额度异常。

## 用量与保存

| 调用 | 输入 token | 供应商报告输出 token |
| --- | ---: | ---: |
| 检索决定 1 | 1782 | 123 |
| 检索决定 2 | 7150 | 326 |
| 检索决定 3 | 9899 | 70 |
| 最终说明 | 10151 | 2175 |
| 合计 | 28982 | 2694 |

金额未定价，没有将输出额度异常隐藏或把超限用量计为请求上限。

原始收据、诊断与恢复收据位于 Git 忽略的 `private/knowledge/real_learning_verification_20261013.json`、`real_learning_raw_diagnostic.json` 和 `real_learning_verification_recovered_20261013.json`。原模型结果与调用 receipt 仍保留在数据库。完成了新增 / 修改代码静态检查、前端编译与部署；未运行额外测试或再次调用供应商。

供应商官方接口说明供协议排查参考：[端点选择](https://docs.modelflare.dev/guides/endpoints/)、[OpenAI-compatible 接入](https://modelflare.dev/articles/openai-compatible-api-guide)。文档未为本次请求返回超限给出可核实原因，不能据此宣称 token 上限问题已经解决。

后续无模型调用排查与本地修复见 [供应商输出 token 上限排查](PROVIDER_TOKEN_LIMIT_DIAGNOSIS.md)。原始 wire 与 usage 明细未保存，不能从历史归一结果还原；本次验证记录与用量未重写。
