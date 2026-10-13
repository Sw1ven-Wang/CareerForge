# 供应商输出 token 上限排查

日期：2026-10-13。本轮只读历史证据、接口文档和本地代码并修复记录缺口；新增模型调用 **0**，未运行测试、MockTransport 请求或重放真实任务。

## 当前结论

原最终说明请求配置输出上限 2048，供应商报告 `completion_tokens=2175`，超过 127（约 6.2%），`finish_reason=stop`。**超限原因尚未证实，不能宣称已修复供应商硬上限。**

本地调用链为学习 manifest 的 `max_output_tokens` → `ModelRequest.max_output_tokens` → OpenAI-compatible HTTP 请求的 `max_completion_tokens`。`reasoning_effort=low` 分支只发 `max_completion_tokens`，没有同时发 `max_tokens` / `max_output_tokens`，没有本地追加、相加推理用量、修改 token 数字或自动付费重试。本轮保留该协议，不以未验证的参数组合替换它。

旧调用收据只保存 ModelRequest 的哈希及归一用量；旧阶段产物保存的是解析后的 ModelResult，并非原始 HTTP 响应。当前代码与冻结配置支持上述参数路径，但历史实际发出的字节、响应 `model`、`total_tokens`、推理明细、HTTP request ID 没有保存，无法追溯确认中间网关是否删除、改写或忽略上限。

## 计量口径

按 [OpenAI Chat Completions 契约](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，`max_completion_tokens` 包含可见输出和推理 token；`usage.completion_tokens` 是生成 completion 的总量。按 [OpenAI token 计量说明](https://developers.openai.com/api/docs/guides/token-counting)，不可见消息格式、工具结构 token 也可能计入输出，且同样受到输出上限限制。

因此正文的本地 tokenizer 估算不能取代供应商用量，也不能用“2175 含推理，2048 只限正文”解释为合规。明细是总量的组成部分，不应再加到总量，也不能扣掉推理后与总上限比较。当前统计直接使用 `completion_tokens`，没有重复计数。

[Modelflare 端点文档](https://docs.modelflare.dev/guides/endpoints/) 区分 Chat Completions 和 Responses；[模型与分组](https://docs.modelflare.dev/guides/models-and-groups/) 说明路由受 key 绑定分组和上游渠道影响。公开说明未能确证该 key 的 `gpt-6.1-sol` 路由如何处理 token 上限。历史响应 ID 以 `resp_` 开头仅是线索，不能证明项目调用了 Responses、也不能据此确认上游桥接错误。没有静默切换端点、模型、key 或推理强度。

## 已完成的本地修复

- 请求构建与发送共用 HTTPX 请求构建方法。在发送前的预留事务中保存安全摘要：端点路径、模型、推理强度、上限字段和值、顶层字段名、工具数、结构化输出标记、实际序列化 body 的 SHA-256 和字节数。发送时重新记录同一构建路径的摘要；不保存密钥、Authorization、prompt、代码或完整 body。摘要证明项目送往网关的请求配置，不能证明供应商内部转发。
- `ModelResult.diagnostics` 与 `model_call_receipts.diagnostics` 保存数值白名单内的供应商 usage：输入/输出/总量，以及推理、缓存、文本、音频、预测等明细（仅当供应商实际返回）。保留响应 model / object / ID，区分 JSON response ID 与 HTTP `x-request-id`。不保存任意响应正文或任意 header。
- Chat 契约继续只用 `prompt_tokens` / `completion_tokens` 结算，不把 Responses 的 `input_tokens` / `output_tokens` 静默当作等价字段。若供应商混合返回两套计量且不一致、总量不等于输入加输出、推理量大于输出量或字段缺失，诊断保留警告和原数字；不猜测、不裁剪、不将超限改成 2048。
- 解析失败、结构化 JSON 无效或 `finish_reason=length` 时，已返回的数值 usage 和安全诊断仍结算至 receipt。以前这些错误会将收据用量设为未知。receipt 的 completed 表示收到用量记录，不代表业务成功；是否已知仍以 `usage.status` 判断。
- 诊断与结算字段分开存储，不改写原有用量契约。迁移 `a61f93d0c503` 只增加可空 JSONB 列，历史行仍为 null，没有伪造历史 wire 数据。学习工作流已有的超限停止逻辑继续生效、不自动重试；该逻辑只能阻止后续调用，不能逆转供应商已发生的费用。

## 仍需的供应商侧证据

历史最终响应 ID 为 `resp_0d607e71c407852d016acda871290881949517b3d98615f564`。供应商控制台的该请求日志、真实上游路由与实际转发的 token 参数，以及计量明细，才能区分上限转发失效、桥接差异和用量重算问题。[供应商用量说明](https://docs.modelflare.dev/guides/usage-and-cost/) 指向控制台的逐请求日志。

本轮没有登录控制台、发送供应商请求或再次验证协议。以上本地修复通过代码静态检查并部署，不构成真实供应商验证。旧验证的 2175 / 2694 用量、失败状态和 recovered 报告保持原样。
