# JD 语言确认与关联知识存储

新导入 JD 的对话启用 `learning-language-v1`。语言配置属于用户学习政策，不加入或改写雇主要求。

## 用户流程

1. 保留 JD 原文，提取逐项要求与来源。
2. 若没有明确必需的实现语言，进入 `awaiting_language_choice`，向用户提问。
3. AI 应用和 Agent 开发的实现路线必学 Python；后端实现可选 Python、Java、Go、Rust 等，允许多选。
4. 用户可回复 `Python`、`Java,Go`、`后端用 Java` 或 `/language Python`。自定义语言使用 `/language <语言名>`。
5. 选择只保存配置和关联学习目录，之后进入 `awaiting_goal_confirmation`；不会自动确认目标或启动研究。
6. 学习架构、原理与取舍不限制语言；只有具体实现材料按对应 AI/Agent 或后端轨道筛选。

AI/Agent 必修 Python 的来源记录为 `product_rule_ai_agent_python`。JD 明确要求的语言及 AND/OR/NOT 限定仍由原始 GoalVersion 保留；用户不能通过新选择删除必需语言，OR 组满足一种即可。JD 已给出必需语言时，用这些语言配置实现候选范围，不再次强制提问。

## 存储形式

继续使用现有 SQLite / PostgreSQL 数据模型，不新增表或覆盖历史对象。

| 内容 | 位置 | 含义 |
|---|---|---|
| JD 与逐项需求 | 原有 JD 对象、`career_goal_versions` | 原文、证据、岗位限定不变 |
| 语言配置 | `dependency_objects.kind=learning_language_profile` | 目标绑定、AI 必修语言、后端选择、JD 语言来源、动作回执 |
| 关联学习目录 | `dependency_objects.kind=learning_knowledge_outline` | 每项需求关联跨语言架构目标与具体实现目标 |
| 真正的架构/实现知识 | 原有 `knowledge_units`、`knowledge_unit_versions`、`publications` | 仍须证据、独立审核与原有发布检查 |
| 架构与实现关联 | `dependency_objects.kind=knowledge_implementation_link` | 引用两份已审核 publication，并保存实现语言与所属轨道 |

语言配置是不可变对象，重新选择生成新对象；工作流 target 保存当前指针。原有 profile 和目录保留。会话执行冻结配置指纹，执行开始后调整语言需要新建目标。新增目标继承之前的选择，重新绑定新目标并记录血缘。

学习目录示意：

```json
{
  "requirement_id": "原需求ID",
  "technology": "任务编排",
  "architecture": {
    "entry_id": "architecture-稳定ID",
    "language": null,
    "knowledge_status": "awaiting_reviewed_material"
  },
  "implementation": {
    "entry_id": "implementation-稳定ID",
    "domain": "ai_agent",
    "languages": ["python"],
    "knowledge_status": "awaiting_reviewed_material"
  },
  "relation": "implementation_example_for",
  "semantic_quality_verified": false
}
```

目录是学习目标，不能计为已经生产或审核通过的知识。真正的内容继续保存为原有知识单元和发布版本。关联接口要求架构/原理类型与实现/函数/测试类型各一份、具有共同的已审核主题，且实现语言有引用源码扩展名的支持。关联表示用户指定的示例关系，`semantic_quality_verified=false`，不会制造新的事实、跳过独立审核或宣称已掌握。

## 接口

- `GET /api/v1/conversations/{conversation_id}/workflow-status`：返回 `language_question`、`language_profile`、`learning_knowledge_outline`。
- `POST /api/v1/workflows/{workflow_id}/implementation-language`：提交语言选择，沿用工作流修订号、焦点修订号、幂等键和身份鉴权。
- `POST /api/v1/knowledge/implementation-links`：关联已审核的架构知识与实现知识。
- `GET /api/v1/workflows/{workflow_id}/knowledge-materials`：读取关联材料，保留架构材料，仅选择语言匹配的实现材料。被撤销或无权限的关联不会返回。

选择请求：

```json
{
  "expected_revision": 4,
  "expected_focus_revision": 2,
  "idempotency_key": "language-choice-001",
  "languages": ["python"]
}
```

关联请求：

```json
{
  "namespace_id": "所属知识空间ID",
  "architecture_publication_id": "已发布架构知识ID",
  "implementation_publication_id": "已发布实现知识ID",
  "language": "python",
  "domain": "ai_agent"
}
```

语言筛选也接入 JD 对话学习计划的材料选择。来自其他语言项目的架构单元仍可使用；实现单元依据已审核引用路径判定语言。当前路径识别支持 Python、Java、Go、Rust、JavaScript、TypeScript、C++、C#、Kotlin、Scala、Ruby、PHP、Swift；无法确认语言的实现不冒充匹配结果。直接使用底层独立知识接口的旧调用保留原合同，本次交互约束从新 JD 对话入口生效。

## 验证边界

本轮验证 JD → 语言提问 → 选择保存 → 关联目录，以及已审核知识的关联持久化、按语言读取、权限和失效处理。JD 重放使用此前真实模型回执；知识关联测试使用明确标注的本地测试来源及模型替身。没有新增真实模型调用，没有 GitHub 检索，也不代表新的生产知识已生成。
