# ADR 0006：模块练习与条件化成果证据

日期：2026-10-17；承接 review 的 C 包。

## 复用已有验收链路

StudyModule → StudyArtifact(exercise) → StudyArtifact(attempt) → LearningWorkspace / Practice → Submission → Assessment / Confirmation → module_outcomes。

exercise 是冻结的 ExerciseSpecVersion，记录原计划、模块与需求版本、定义及验收条件、参考 commit / 行区间 / 哈希。attempt 单独记录序号、帮助模式与新空白工作区；相同定义复用练习版本，不同尝试不复用代码。最多每方向 100 次尝试。首次建立练习时才创建内部目标适配，不虚构岗位事实，不自动建立另一份旧版计划。

复用现有 Objective / Criterion、BuildProject / BuildTask、不可变 Submission、附件、运行记录、Assessment 和人工确认。三项关键条件分别核对产物与运行观察、本人解释、迁移或差异说明；均由人工逐项审阅。材料整理与缺项检查不调用模型，不替代语义或能力评价。命令仍须用户单独确认，在原无网络沙箱执行；导入参考项目不执行。

## 帮助条件与尝试封存

- 允许辅助模式：可使用现有 Agent 与补丁审阅，验收后仅标记“允许辅助条件下完成”。未观察到帮助也不会据此改为独立完成。
- 独立模式：新建空白副本；进行中暂停同账号 Tutor、学习说明和 IDE 编码 Agent，不能通过省略 practice_id 绕过绑定。该副本始终拒绝 Agent 或来源不明的补丁；可编辑、读取固定参考代码，确认命令及应用命令产生的变更。第一份提交后封存该独立尝试，再提交须创建新尝试；幂等重试返回原提交。
- 提交冻结本系统请求数、同模块导师回答数、已应用 Agent 补丁数，以及本人声明的外部帮助、提示次数和说明，绑定代码哈希与帮助条件哈希。系统观测与本人申报分别展示。无法监测其他设备或证明没有外部帮助。
- 独立模式仍声明外部帮助或提示时，证据归为辅助完成；未声明时保留帮助条件未知。声明未使用且系统未观察到帮助时，才显示“本人声明独立完成”或“迁移任务通过（本人声明独立）”。

User 行锁串行化独立尝试创建与 Agent 启动；已有导师/IDE 请求未结束时不能开始独立尝试。关闭/放弃释放账号级暂停，记录和已提交材料保留；此副本本身仍禁止 Agent。

## 更新计划与失效

人工接受后，由用户显式选择更新当前兼容的已有模块计划。保留原计划，创建带 source_plan_id、递增 revision_no、module_outcomes 的新版本，模块内容不变，原先自报阅读进度按模块复制。以方向 revision 校验并发，以幂等键避免重复发布；不会调用 Plan Agent 或悄悄重排其他模块。

module_outcomes 引用任务、提交、确认、尝试与冻结练习，不另建一套能力状态。读取时重新验证原需求、审核项目、引用版本、任务当前版本和确认有效性。来源撤销、提交替换或验收撤销后，成果动态失效；不可再据历史“接受”显示有效完成。

用户自报模块进度仍是 reported_by=user、competence_verified=False，与成果证据分开。人工验收来源是同账号自验，不是第三方认证；所有条件化成果仍 competence_verified=False，不自动修改岗位目标的了解/熟悉/掌握。没有声称测量职业能力或真实迁移收益。

## 接口与范围

新增 GET / POST /study/tracks/{id}/attempts 与 POST /study/tracks/{id}/attempts/{attempt_id}/close；提交、评阅、撤销与计划更新继续复用 /studio 的 delivery 接口。OpenAPI / TypeScript 合同同步，无数据库迁移和付费工作流版本变更。

此次不实现 D 包的通用上下文服务、子任务委派、自动评估模型或新效果实验。离线验证范围见 C 包实施记录。
