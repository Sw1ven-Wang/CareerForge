# 选定项目的关联知识分析预览

`linked-architecture-python-analysis-preview-v1` 是原生 `analysis` 任务的显式入口。
它复用 ModelGateway、租约/预算、SourceRead、引用预检与 KnowledgeService 的草稿存储，
不创建 knowledge_build 根任务，不派发独立审核、发布或 learning_plan 任务。

每次请求绑定原 GoalVersion、用户选定的 project_portfolio、其中一个项目记录与固定 SourceSet，
冻结 taxonomy_versions、读取行窗、模型 profile 和最多四个单元。模型只能引用真实读取回执，
dependency_paths 在输出 schema 中限制到该快照的实际路径。

输出包含 architecture 单元、implementation 单元及 subject_key 配对。架构语言约束为 null；
具体实现只允许 Python，并检查每条实现 Claim 的支持引用都来自本次 `.py` 原文。
纯架构材料必须明确 implementation=null，不可把 TypeScript 内容伪装为 Python。
静态分析禁止生成 runtime_observation。

每项目保留至多一次按原引用合同进行的歧义纠正；原生 run 的调用预算覆盖正常输出与纠正。
无效数量/读取范围在发出模型请求之前拒绝；运行终态和原模型回执不可改写。

数据继续使用现有表，无新增 DDL：

| 内容 | 存储 |
|---|---|
| 架构/实现候选及 Claim | 原生 knowledge_unit_versions 与关联对象 |
| 实际原文读取 | source_reads |
| 精确模型计量和响应 | model_receipts |
| 架构与 Python 实现的候选关联 | dependency_objects.kind=draft_implementation_link |
| 选定项目分析汇总 | dependency_objects.kind=linked_analysis_preview |

关联对架构/实现版本与 GoalVersion 建立阻断依赖；汇总依赖原组合及实际草稿。
所有草稿标识待用户确认、未完成独立语义审核。它们不能作为 Publication 或已满足 JD 的证明，
现有只读已发布知识的学习/Tutor 路径不会获得草稿的发布权限。

阶段停在 awaiting_analysis_confirmation。后续审核、发布与计划需要各自明确的流程准入；
本入口不会自动续跑这些步骤。
