# 从岗位要求到 Tutor 指导

## 1. 导入、目标与源码

`POST /api/v1/jds` 保存原文及其精确字节坐标。`POST /api/v1/jds/extractions` 冻结目标和 `requirement_topic_links`。本轮可将岗位中的 LangGraph 条件路由要求关联到 `agent.conditional_routing` 和 `framework.langgraph`。独立的 Python 要求保留自己的语言 Topic。归一化保留深度、必选关系和 AND/OR/NOT。没有可授权的主题时返回 `provisional`。

新库由现有 bootstrap 安装主题与 Facet。已有库的管理员可以通过现有 taxonomy 提议和审核流程引入主题。旧 JD 和运行版本保持其原有身份。在已具备分类的命名空间显式发起新提取才能生成新关联。

`POST /api/v1/knowledge/builds` 可直接接收固定 `source_id` 与 `goal_version_id`。新分析合同要求写出独立可讲的事实主体及条件。小型快照的 `dependency_paths` 还会被输出 schema 限定在已导入路径中。源码读取、独立审核、事实入库和整项任务覆盖分别记录。未读路径、缺失的任务维度和无法独立讲解的事实保留各自状态。

## 2. 计划与指导

更新学习者资料时使用实际 `goal_version_id`。对选中的有效 Publication 发起单次计划提议，经用户接受后，系统记录活动 plan、version 与 module。若所有候选事实均缺少独立使用审核，单次计划在提交模型前返回 `FACT_CONTEXT_REQUIRED`。

CLI 使用 `careerforge chat`。输入“开始学习”“开始本模块”“继续当前模块”或“带我学习当前模块”发起讲解。在已接受模块范围内追问可以用 `careerforge chat --guided --once '…'`。其余问题走原自然语言路由，由问题决定项目、技术或比较范围。

`POST /api/v1/conversations/{id}/guided-turns` 接收 `expected_revision`、`idempotency_key`、可选 `question`、`funding_id`、`model_call_cap` 和 `preferences`。`question` 省略时，服务端从本人已接受的活动模块构造讲解任务。显式提问仍经过当前问题的相关性检查。读取 `GET /api/v1/turns/{id}/session-tutor-binding` 可以看到使用的计划版本和 `module_guidance` 标记。

有额度时复用有界 Tutor 与独立回答检查。没有额度时，模块讲解可以直接显示当前模块已审事实原文和引用。这个确定性分支记录 `accepted_module_binding`。原事实、条件、引用、模块和权限均由已有校验链核对。交付回答、回答覆盖、模块确认和学习掌握是不同状态。

## 3. 中断记录

运行结束和预算结算时，将仍处于 `attempted` 的模型收据记为 `unknown`。已经消费的调用额度仍记录在账，子任务按原运行合同结算。下游不能把 `unknown` 当作成功或自动重发同一次调用。历史 SQLite 快照可用于查看中断当时的状态。
