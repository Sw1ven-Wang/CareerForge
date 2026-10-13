# IDE 练习成果提交、人工验收与已有计划更新

日期：2026-10-13。入口 `/studio` → 练习卡片 → “提交成果 / 人工验收”。全部复用已有目标与完整条件；本轮不调用模型或执行练习代码。

## 流程

1. 提交：填写成果说明、自己的解释、迁移记录及可选实际投入，选择已完成命令记录。后端冻结当前完整 IDE 文件、内容哈希、选中日志及全部验收条件。本人解释为空仍可提交，但材料不足项不能整体接受。
2. 材料盘点：恢复原 S07 的零模型调用纯工作流注册。它只逐项列出材料与缺口，所有条件保持 unknown，不执行代码、不代填本人理解。
3. 人工验收：对完整条件集合分别选择 pass / fail / unknown、允许的检查方法、理由及自验步骤/观察/环境。自验代码版本绑定提交快照；关键未知项不能仅凭模型描述或退出码改成通过。材料不足、条件遗漏、旧提交、旧审阅及错误代码版本均由原 S07 服务拒绝。
4. 返修：选择要求返修后保存完整审阅，补充材料并重新提交新版本。旧提交快照保留，旧审阅不能验收新提交。已接受任务须先撤销再补交；撤销原因保留。
5. 计划：只有当前提交的有效人工接受，才能明确写入包含该目标的最新已有计划。保存的是同一计划的新草稿，保留成员、先修顺序、工时、旧版本及当前采纳版本；用户随后可在计划页核对并采纳。计划页显示冻结成果对应的验收记录，原确认撤销或来源失效后显示待复核，不继续冒充有效进展。

提交和人工验收按钮位于同页抽屉，支持历史提交只读查看、冻结文件与日志预览。所有结论均为当前用户自验，非第三方认证或通用能力认定。

## 代码版本与运行证据

IDE 没有 Git commit 时采用 `workspace-sha256:<64 位哈希>`，由服务端计算完整文件映射的内容哈希。`Submission.code_commit` 保持 null，不伪造 40 位 Git commit。完整文件 JSON 与运行记录 JSON 作为独立 submission 附件保存并绑定本次提交。代码包限 1 MiB，超过时拒绝而非截断。

新命令执行会保存实际输入代码哈希、运行器策略、开始和结束时间。提交仅把哈希与当前完整代码一致的已完成记录纳入 `execution_record`。其他记录（包括升级前未保存代码哈希的日志）仍可冻结存档，但明确显示未绑定，不能满足强制运行证据条件。此次没有重新执行上轮验证命令，也没有追补或伪造其历史代码绑定。

提交之后编辑 IDE 不改写旧快照。读取、审阅、更新计划和幂等重放继续校验用户归属、原学习说明、目标、引用、提交附件和快照完整性。来源撤销后拒绝继续使用相关提交；用户自己的 IDE 文件保持可读。

## 实现与接口

复用 `BuildProject / BuildTaskVersion / Submission / Assessment / AssessmentReview / AssessmentConfirmation / PlanDeltaProposal`；练习首次显式提交才关联成果任务。没有重建独立自主构建模块、增加模型或自动验收。原 `assessment-offline-v1` 工作流仅做材料盘点，预算 0。

在现有 JSONB payload 内保存 `workspace_snapshot`、practice → task 关联和计划 `practice_outcomes`，无需新表或数据库迁移。普通 `SubmissionInput` 不接受客户端注入 `workspace_snapshot` 或伪造 sandbox provenance。已有计划编辑、目标改版与采纳保留验收记录，但旧目标版本的确认不自动迁移为新版本的确认。

新增路由均在 `/api/v1/studio/workspaces/{workspace_id}/practices/{practice_id}` 下：

- `GET /delivery?submission_id=...`：当前/历史快照、完整条件、材料盘点、自验记录与可更新计划。
- `POST /submissions`：`expected_revision`、summary、command_ids、explanation_answers、transfer_notes、actual_minutes；需幂等键。
- `POST /assessments/{assessment_id}/confirm`：原 S07 `ConfirmationInput`，完整分项与版本检查。
- `POST /assessments/{assessment_id}/revoke`：原 S07 `RevocationInput`。
- `POST /plan-updates`：已有 plan_id、base_plan_version_id、expected_plan_version、reason 与 `scope_acknowledged=true`；需幂等键，保存新草稿。

OpenAPI 与前端生成类型同步更新。旧 submission 的 Git commit 语义与原 S07 条件/证据检查保持兼容。

## 定向验证

只运行 `test_studio_delivery.py` 中两项集成场景，使用独立 `careerforge_contract_validation_20261012` 数据库、明确的离线资料/运行记录；不调用真实模型或运行项目命令：

- 补交、完整条件人工接受、已有计划新草稿、幂等重放、旧审阅拦截、撤销后计划记录失效。
- 未绑定日志不能满足执行材料，提交后代码变化不改旧快照，客户端不能注入快照，来源撤销后不可继续读取。

首轮第一个场景通过；第二个场景仅因测试把既有撤销接口 202 误写为 200 而失败。修正状态码预期后仅复验该场景，通过。没有扩大或反复运行其他测试。后续补充逐条件材料盘点精度与无任务历史 ID 的防护是局部检查修正，未追加回归。

相关 Python 静态检查、前端 TypeScript/Vite 编译与契约生成已完成，已部署到本地 `http://127.0.0.1:8080`。验收表单重新打开时会显示已保存的逐项结论、核对记录及撤销状态。真实个人解释、逐项人工接受和计划采纳仍须由用户在页面操作，不由本轮自动填写。

## 本地交付状态

前端和 API / worker / dispatcher 已更新。浏览器确认现有 W2 练习可以打开新抽屉，提交表单展示成果说明、本人解释与原有运行记录的“仅存档”标识。未替用户提交、填写本人解释、保存人工验收或更新实际学习计划；这些操作由页面中的明确按钮触发。
