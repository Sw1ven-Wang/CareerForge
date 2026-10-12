# 真实岗位分析 Agent 第一阶段

模型：用户配置 gpt-6.1-sol；推理强度 low；OpenAI 兼容 Chat Completions；供应商 Base URL 为 https://modelflare.dev/v1。只处理用户明确选中的单份完整岗位快照，不联网搜索岗位、不发送 joblearn 私人材料。

## 使用

刷新来源页，打开完整岗位详情，在“岗位标准化”中点击“使用真实 Agent 提取要求”。每次显式创建一个后台运行，最多一次模型请求、最多 2048 输出 token（包括模型推理消耗），不自动重试。运行完成后显示 job-agent-1.0 候选要求；人工核对并确认后再生成固定统计报告和计划草稿。刷新、重复投递或幂等重放不会自动重复消费。

模型只有候选提议权限：结构必须通过 Pydantic、字典白名单、所属快照/section、唯一精确 quote 校验。Unicode 偏移由程序从引用文本计算。模型不能确认事实、改变统计分母或证明本人能力。事实记录包含模型、prompt 版本、运行 ID 和 synthetic=false。

## 配置

私有 .env（Git 忽略）：OPENAI_API_KEY、OPENAI_BASE_URL、MODEL_NAME、CAREERFORGE_REAL_AGENT_ENABLED=true。
API 和 Worker 通过 Compose env_file 加载。前端镜像不包含私有 .env；状态接口只返回模型及配置是否齐全，不返回密钥。示例 .env.example 中的密钥必须为空。

CAREERFORGE_REAL_AGENT_ENABLED=false 可停用新的真实任务。CAREERFORGE_MODEL_MAX_OUTPUT_TOKENS 上限为 2048；CAREERFORGE_REAL_AGENT_TIMEOUT_SECONDS 默认120秒、上限180秒。等待期间每10秒续租；取消或租约丢失阻止产物写入，并按未知用量记账。没有供应商单价时 cost_status=not_priced，不把未知成本写成0。

## 本次验证

用户仅授权一次真实请求，输出上限2048。2026-10-12 在隔离库 careerforge_contract_validation_20261012 使用自造两条模拟岗位要求验证；首次调用在原30秒等待窗超时。调用预留=1，用量未知，无自动重试，不能宣称供应商接口已成功打通。安全回执保存在 private/real_agent_smoke_20261012.json，未提交私有文件。

之后将等待窗扩为120秒并补上心跳，仅用离线模拟验证，不再次请求供应商。4项针对性用例通过，覆盖low请求参数、JSON schema、原文不可伪造、候选不自动确认，以及记账/保存/重复投递/账号隔离。既有模型契约用例在适配准备阶段通过。前端完成类型检查与构建；没有跑完整回归。

参数依据：[OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。第三方兼容性以实际响应为准。

已部署到 http://127.0.0.1:8080/sources；API 容器加载真实模型配置，等待窗120秒。单次超时后的修改未再进行真实调用验证。
