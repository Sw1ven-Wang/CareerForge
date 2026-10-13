# 内置编码 Agent：一次真实小型编码闭环验证

日期：2026-10-13。用户授权真实编码验证，并另行明确确认唯一命令 `python -B verify_registry.py`。本次没有使用 FakeModel、模拟命令运行器或宿主直接执行生成代码。

## 结论

**给定小型练习的真实闭环通过：已有学习说明 → 迁移练习 → 真实模型生成 → 审阅补丁 → 应用到独立代码副本 → 用户确认命令 → 隔离运行 → 六项断言通过 → 保存练习记录。**

两份文件为模型原始输出，没有人工补写或修复实现。没有修改 joblearn、参考仓库或 CareerForge 的业务源码；生成文件保存在独立 IDE 工作区，模型调用和命令实际通过部署后的 CareerForge API / Worker / workspace-sandbox 执行。

这不代表整个 CareerForge 已完成全真实端到端验证，不代表用户已掌握目标，也不代表注册表可作为生产安全边界。

## 输入与身份

- 现有工作区：`18f0d683-cf61-408e-8d53-80abc710aff3`，“我的学习项目”；调用前为空。
- 练习：`e5b6f7ed-5cc0-4703-b674-e0ac2914e491`，离线只读工具注册表。
- 目标：`04513ea0-3c50-4db8-ab20-df5ed14dcaeb`，“【模拟计划】W2 模型接口与受控工具调用”。当前计划仍为模拟标签，未改写为真实个人能力。
- 源学习说明：`9dad4d05-ad17-4db8-8ac0-418e3ae7afc5`，真实 smolagents 阅读结果，本次沿用其已校验原文；旧任务 recovered 与旧超限记录保留。
- 参考 commit：`96f33faaf028479119ec8d34507b47694cf14e34`。
- 引用：`3ca8aed0-c11e-4a5e-9889-e9548444044c`（agents.py:1453–1502）与 `53f26610-1e84-4dc1-a872-6a6d065280ec`（tools.py:1361–1412）。

## 模型生成与补丁

编码 run：`8254c4ed-ca48-45cb-ab8e-cd22165e7335`，最终 succeeded。仅 **1 次** `gpt-6.1-sol low` 真实调用，零重试、零追加模型调用。

实际发送 `max_completion_tokens=4096`；供应商报告输入 7736、输出 3875、总量 11611，其中推理 215，finish_reason=stop，HTTP 200。两套输入/输出计量别名一致，总量校验通过。本次输出未超限，不据此宣称供应商硬上限问题已修复。金额未定价。

response ID：`resp_097da6f6f739070a016acdbf91d8a887d18c58ea9b1f47f888`；请求 body SHA-256：`65c2da270a81309d0790bee02270f5feb1f7d9caa7d10badb719c06a8290fcb0`。完整安全诊断与 receipt 在数据库保存，未保存或输出模型密钥。

补丁 `f6720f8c-d599-42e1-b807-45cb4c93f6cb` 包含两个新文件：

- `tool_registry.py`：只读工具注册白名单、简单 object schema 子集、参数失败结构化返回；明确 bool 不能作为 integer。timeout_seconds 仅为策略元数据。
- `verify_registry.py`：标准库验收入口，使用真实 assert，成功输出逐项 JSON；不只是打印 PASS。

补丁已只读审阅并做语法解析，随后通过正式 APPLY 接口写入工作区。文件哈希与模型输出一致；没有自动执行代码。工作区 revision 由 2 到 3，运行后保存笔记为 4。

## 用户确认与真实运行

待确认命令 action：`62ef7e4b-3f87-402d-9a1d-d244207b9bf6`。用户在聊天明确选择“确认执行这条命令”，随后通过正式 RUN 接口执行 **1 次**：

```text
python -B verify_registry.py
```

运行环境为现有专用容器：无网络、无模型密钥、不挂载宿主项目或 Docker socket；命令子进程 uid/gid 65534，最长 30 秒，256 MB / 64 进程 / 1 CPU。`-B` 禁止生成字节码，不禁用 assert。未下载依赖或运行导入仓库脚本。

实际退出码 **0**，stderr 为空，stdout 返回六项 JSON 结果：

| 检查 | 实际结果 |
| --- | --- |
| 正常只读调用 | ok=true，字符串长度为 2 |
| 未知工具 | UNKNOWN_TOOL，ok=false |
| 额外字段 | EXTRA_FIELD，ok=false |
| 缺少必填字段 | MISSING_FIELD，ok=false |
| 错误类型 | INVALID_TYPE，ok=false |
| 非只读工具注册 | NON_READ_ONLY_REJECTED，ok=false |

没有额外 CareerForge 测试、模型调用或项目运行命令。实际 stdout、stderr、退出码保存在同练习 command action；结果说明已保存到练习笔记。退出码和这些断言只证明上述范围。

## 边界与存档

当前没有强制超时执行、完整 JSON Schema、JSON 文本解析或工具函数异常统一包装。只读操作是注册元数据约定，并不能阻止任意恶意 handler 产生副作用；本次实际执行工具只处理内存字符串。模型生成的 schema 子集实现也没有经过完整异常输入、并发或安全专项测试。

练习仍为 draft、verified=false，个人理解与学习目标未自动确认。正式成果提交、人工验收与计划更新仍是后续独立步骤。

完整过程存档：Git 忽略的 `private/knowledge/real_coding_verification_20261013.json`。模型原始代码导出位于 `private/knowledge/real_coding_generated_20261013/`，便于本地审阅；权威副本仍在 IDE 工作区。一次性生成与执行脚本均有已执行阶段保护，不会自动再次调用模型或执行命令。

已在部署页面查看两份代码、已应用补丁、唯一命令退出码 0 和练习笔记；页面证据截图：`C:/Users/sw/.codex/visualizations/2026/10/12/01a12e3e-f79a-76a0-87d3-210a136aa0c5/real-coding-closed-loop.png`。
