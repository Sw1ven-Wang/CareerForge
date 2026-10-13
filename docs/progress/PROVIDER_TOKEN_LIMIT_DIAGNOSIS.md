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


## 新增诊断后的唯一一次真实验证

2026-10-13，用户另行明确授权一次真实验证，最多 2048 输出 token，超限停止，无额外测试。使用上次冻结计划与 smolagents 已读代码输入，仅生成最终学习说明，不重新运行检索。schema 已包含上次修复的标题字段，因此不是历史请求字节完全重放。

- 运行 `a690a8ba-ae3f-4a6a-b034-7263e85b97a2`，receipt `d5442a10-c0e2-461f-b2c6-4d36e389e67f`；调用预算 1，实际预留/receipt 均为 1；自动重试 0，追加调用 0。
- 发送路径 `/v1/chat/completions`，模型 `gpt-6.1-sol`，`reasoning_effort=low`，唯一上限参数 `max_completion_tokens=2048`；没有 `max_tokens` / `max_output_tokens`。请求是结构化输出，无工具。
- 序列化 body 为 22261 bytes，SHA-256 `aad09bdba7e3bcacb5a15d9e4e58389edfff7c19c7bc2e7f615526afa97235f2`。
- HTTP 200，响应 `object=chat.completion`、`model=gpt-6.1-sol`、`finish_reason=stop`；HTTP `x-request-id` 未返回，JSON response ID 为 `resp_0fa7bb4650aa5a6d016acdafa4510c8194b42d98e0a92f4f9e`。

| 供应商计量字段 | 实际值 |
| --- | ---: |
| prompt_tokens / input_tokens | 10162 / 10162 |
| completion_tokens / output_tokens | 2137 / 2137 |
| total_tokens | 12299 |
| completion_tokens_details.reasoning_tokens | 241 |
| completion_tokens_details.text_tokens | 0 |
| completion_tokens_details.audio_tokens / image_tokens | 0 / 0 |
| prompt_tokens_details.cached_tokens | 0 |

**本次仍然超限：2137 > 2048，超出 89 token（约 4.35%）。** 收据和完整模型结果先保存，随后将运行置为 failed，并记录 `PROVIDER_OUTPUT_OVER_LIMIT`；没有追加模型调用、切换协议、重试或更新旧学习报告。

发送摘要已确认本地输出上限参数未遗漏。供应商两套 usage 别名一致，输入与输出之和等于 total_tokens，项目直接采用 completion_tokens，没有把 reasoning_tokens 再加一遍。因此本次不能归因于项目的重复计数或计量别名选择。

2137 中报告的推理量为 241，算术差为 1896；该差值不能直接认定为可见文本量（供应商同时返回 text_tokens=0），也不能用来把总上限超限判为合规。存在“供应商只对部分输出执行上限”或“上游转发/计量转换不同”等可能，但这次响应仍不包含真实上游请求参数或渠道日志，无法在这些假设间定论。`resp_` 前缀和混合 usage 字段只说明返回形状，不能证明内部协议实现。

诊断结果与原模型结果保存在数据库 StageArtifact 和调用 receipt；本地原始验证存档为 Git 忽略的 `private/knowledge/provider_token_verification_20261013.json`，一次性执行脚本为 `provider_token_verification_20261013.py`，已有存档时拒绝再次执行。独立诊断不发布学习报告，succeeded/failed 只描述本次检查结果。

后续若继续定位，应使用上述 response ID 向供应商查询实际上游上限、路由及计量日志。本轮授权已用完，不再发起请求，也没有运行任何额外测试。


## 输出额度调整

2026-10-13，按用户要求将新任务每次输出上限从 2048 提高到 **4096**。更新本地 `.env`、配置默认值/校验上界、学习工作流冻结上限和提示词，并让界面从配置接口显示实际额度。岗位采集也冻结配置上限，历史缺失该字段的任务仍使用原先 2048；已有其他任务的 manifest 同样不改写。

本次调整不追加模型调用、不运行测试。保留请求诊断与学习工作流的超限停止保护；提高允许额度不等于修复或证明供应商的硬上限行为。上面的 2048 请求、2137/2175 超限证据保持原样。
