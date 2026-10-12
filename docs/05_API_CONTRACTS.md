# CareerForge 接口与对象契约

> 2026-10-12 实施修订：用户决定移除 LangGraph，采用 Python 显式阶段工作流；协议变更与验收映射见 [ADR 0002](decisions/0002-explicit-workflows.md)。原始 1.0 文档保留在 CareerForge_V1_Implementation_Package。

版本：1.0  
日期：2026-10-12

本文件作为前后端和工作流之间的字段约定。接口尚待实现，示例中的 UUID 是文档占位值；开发时替换成实际接口返回值。初期通过 FastAPI/Pydantic 生成 OpenAPI，并由已固定的 OpenAPI 生成或校验 TypeScript 类型，避免手写两套互相漂移的字段。

## 1 通用 HTTP 约定

业务前缀为 `/api/v1`。请求和响应使用 JSON UTF-8；附件上传单独使用 multipart。时间为 UTC ISO 8601。所有业务 ID 为 UUID，稳定对象的 `version` 是乐观并发 revision 整数，内容版本 ID 单独返回。

### 1.1 通用响应

普通对象返回 `{ "data": {...}, "request_id": "..." }`。列表返回 `{ "data": [...], "next_cursor": null, "request_id": "..." }`。排序规则由具体列表固定；不使用可能在并发插入时漏/重行的任意页码作为唯一协议。

错误格式：

```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "计划已有更新，请重新读取后比较修改。",
    "retryable": false,
    "details": {"current_version": 3}
  },
  "request_id": "req_example"
}
```

current_version只在当前用户有权访问该对象时返回。模型输出、堆栈、密钥、其他用户ID或正文不得作为未过滤错误回传。

### 1.2 状态码

| HTTP | 用法 |
| --- | --- |
| 200 | 读取、更新、返回已有的幂等结果 |
| 201 | 新建短事务业务对象 |
| 202 | 新运行已受理、恢复已排队、删除清理已受理 |
| 400 | 请求组合不合法或游标格式错误 |
| 401 | 未登录或会话过期 |
| 403 | 当前身份无权执行某类动作，如使用受限开发入口 |
| 404 | 对象不存在或不归当前用户，避免暴露资源存在性 |
| 409 | 幂等键异payload、版本冲突、状态冲突、重复恢复冲突 |
| 410 | 已确认属于当前用户的事件历史已过保留窗口，需重取运行详情 |
| 413 | 附件或文本超限 |
| 422 | schema、字段或业务前置条件不满足 |
| 429 | 用户/来源/供应商并发或配额限流 |
| 503 | 依赖暂不可用，返回是否允许重试，不冒充已受理 |

所有具有业务效果的请求使用 `Idempotency-Key`。新建对象通常无 expected_version；修改已存在的稳定对象必须携带 expected_version，追加不可变快照的例外由对应接口明确。相同键、相同规范payload在输入与结果引用仍获授权时返回初次结果；相同键不同payload返回409。先检查幂等重放，再检查首次写入的版本/状态前置条件，避免合法重试因上次成功造成 revision 变化而失败；当前授权检查始终执行。

幂等缓存仅保存对象ID、响应摘要和非敏感字段。删除或撤销之后重试旧键必须重新鉴权，不得返回旧私人正文；已撤销资源返回404，删除自身的合法重试可返回当前清理状态。若历史实现曾缓存正文，删除作业必须一并清理或脱敏。

### 1.3 身份与输入范围

owner_id、worker_id、执行epoch、trusted_execution等字段由服务端决定，普通业务请求不得指定。输入中的所有对象ID要逐个检查同一owner和版本可用性。未知字段默认拒绝，避免模型偷偷增加控制字段。

## 2 认证 健康与附件

| 方法与路径 | 输入 | 输出与验收 |
| --- | --- | --- |
| GET `/health/live` | 无 | 进程存活；不访问模型 |
| GET `/health/ready` | 无 | 数据库和必要配置就绪；不暴露秘密 |
| POST `/api/v1/auth/register` | email、password | 创建用户；S11私有试用可限制邀请白名单，具体用户名单由部署者配置 |
| POST `/api/v1/auth/login` | email、password | 设置服务端会话Cookie；失败信息不区分账号不存在与密码错 |
| POST `/api/v1/auth/logout` | CSRF令牌 | 撤销当前会话；重复操作无副作用 |
| GET `/api/v1/auth/me` | Cookie | 当前用户公开字段及auth_mode |
| GET `/api/v1/auth/csrf` | 已有会话 | 当前会话绑定的CSRF令牌 |
| POST `/api/v1/attachments` | multipart file、purpose | 返回attachment_id、name、mime、size、content_hash、read_status、version |
| GET `/api/v1/attachments/{id}` | 当前身份 | 元数据和可读性；下载正文另用受控content入口 |
| GET `/api/v1/attachments/{id}/content` | 当前身份 | 按权限返回内容，不返回服务器绝对路径 |
| DELETE `/api/v1/attachments/{id}` | expected_version | 立即撤销附件及绑定引用读取，202返回deletion_id；不等待物理清理 |

认证接口按会话语义处理，登录频率受限，密码不得写入通用幂等响应缓存。核心业务幂等规则不要求复用登录响应中的会话秘密。

R1附件仅支持本产品所需的文本、Markdown、JSON、日志、Python文本和安全解析的JUnit XML。XML禁用外部实体，附件按实际内容校验而非仅看扩展名。Zip、安装包、扫描PDF和任意二进制暂不进入首版。附件不会自动执行。未被引用的临时附件按部署保留窗口清理。

read_status为pending/available/rejected/revoked，仅available可绑定业务输入。此字段表示格式与可读性检查结果，不宣称平台已执行杀毒或验证代码安全。V1一个attachment_id只归一个根资源，可绑定该根的多个内容版本；跨独立根复用需重新上传或显式复制为新附件身份。下载同时检查附件、绑定根与所请求版本的授权；根删除后旧content链接不可继续读取。上传者对未绑定附件的临时读取权限有到期窗口。

## 3 目标与来源接口

### 3.1 目标

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| POST `/career-goals` | role_direction、job_type、regions、experience_constraints、time_budget、user_objectives | goal_id、goal_version_id、version=1 |
| GET `/career-goals` | cursor、limit | 当前用户目标列表 |
| GET `/career-goals/{id}` | 无 | 当前目标版本及active_plan_version_id |
| POST `/career-goals/{id}/versions` | expected_version、新目标内容、change_reason | 新goal_version_id及更新后的version |

`time_budget`为`weekly_hours`（可空）、`target_date`（可空）、`reserved_fraction`。时间未知可以出粗粒度任务，不输出伪精确周计划。job_type表示internship/entry_level等目标类别，材料不足时不补造学历、城市或经验条件。

### 3.2 来源导入与更正

`POST /sources/import` 主要字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| source_type | enum | job_posting / interview_experience / official_doc / user_note |
| title | string | 可空，保留用户原题名 |
| source_url | string/null | 原始链接；link_clue模式必填，保存成功不代表正文已读取 |
| import_mode | enum | text / attachment / link_clue |
| content | string/null | text模式必填，其余模式为空 |
| attachment_id | UUID/null | attachment模式必填，其余模式为空 |
| published_at | datetime/null | 不知道则null |
| published_at_precision | enum | exact / date_only / approximate / unknown |
| declared_provenance | string/null | 用户说明，只作为自述 |

正文导入返回source_id、source_snapshot_id、content_hash、read_status、duplicate_of_snapshot_id（可空）与version。link_clue仅创建Source线索身份，返回source_snapshot_id=null、content_hash=null、read_status=unread，明确“链接已保存，正文未读取”；不创建JobPosting，不进入JD分母，也不自动发起研究。之后补正文使用snapshots入口。

同owner同来源且正文与版本化元数据均相同时复用已有快照；元数据更正仍创建新快照。V1不同根来源保留各自内容身份和blob，不建设跨根共享正文去重，以简化删除与授权。

修订用 `POST /sources/{id}/snapshots`，输入expected_version、change_reason，以及content/attachment_id/base_snapshot_id三者恰选一种。base_snapshot_id表示复用该源已授权快照的正文，只更正元数据；metadata_overrides可包含source_type、title、source_name、published_at、published_at_precision及其原始时间表达。返回新snapshot_id。新快照冻结完整正文和元数据，历史报告使用旧快照类别与时间；不能只改Source当前分类而改变旧报告分母。

| 方法与路径 | 用途 |
| --- | --- |
| GET `/sources` | 按材料类型、可读性、时间范围筛选 |
| GET `/sources/{id}` | 来源身份、版本列表、当前可用性 |
| GET `/sources/{id}/snapshots/{snapshot_id}` | 读取某一授权快照及稳定块 |
| DELETE `/sources/{id}` | expected_version；立即撤销访问，返回deletion_id与清理状态 |
| GET `/deletions/{id}` | 返回revoked/cleaning/completed/failed及可展示原因 |

deletion是基础设施维护作业，不是第六类Agent工作流。创建清理作业后即可返回202，但不可把异步物理清理冒充已经完成。

## 4 长运行的通用输入输出

五种run_type：market_research、alignment、project_research、learning_path、assessment。每类使用各自创建入口，不开放任意图名或节点名调用。

通用可选字段：`budget_profile`（服务端已定义的配置名）、`limits`（用户可缩小或在账户上限内选择）、`retry_of_run_id`（可空）、`client_request_label`。API不能接受任意LLM系统提示词、工具白名单覆盖或预算无限值。

budget字段至少区分max_model_calls、max_tool_calls、max_input_tokens、max_output_tokens、max_duration_seconds与可选max_cost。max_duration_seconds是累计有效执行时长，排除queued/waiting_user，恢复不清零；单次外部调用超时单独配置。最终limits取用户请求与服务端上限的交集。首次真实运行前必须有非无限配置；没有供应商密钥时明确提示配置，FakeModel模式不得显示为真实模型效果。

统一202响应示例：

```json
{
  "data": {
    "run_id": "00000000-0000-4000-8000-000000000010",
    "run_type": "market_research",
    "status": "queued",
    "version": 1,
    "events_url": "/api/v1/runs/00000000-0000-4000-8000-000000000010/events"
  },
  "request_id": "req_example"
}
```

## 5 M1 M2 职业研究

### 5.1 创建

`POST /market-research-runs`：

```json
{
  "goal_version_id": "00000000-0000-4000-8000-000000000001",
  "source_snapshot_ids": ["00000000-0000-4000-8000-000000000002"],
  "retrieval_mode": "import_only",
  "source_queries": [],
  "budget_profile": "development_small"
}
```

R1只支持import_only，要求至少一份可读岗位正文。S09增加approved_sources，查询只能交给已启用adapter，读取结果作为获取阶段的不可变acquisition_manifest。搜索摘要不能进入有效JD分母。

### 5.2 结果

`GET /market-reports/{id}` 返回：

- goal_version_id、source_snapshot_ids、job_posting_version_ids；
- 原始数、可读数、提取完成数、去重后数、面经数、未决去重数；
- filters与统计口径；
- requirements：requirement_id、posting_version_id、requirement_type、origin、normalized_skill、原文citation；
- skill_statistics：skill_concept_id、numerator、denominator、ratio、提取覆盖；
- observations：文字结论、citation_ids、限制；
- unresolved_sources和warnings；
- generated_by_run_id、config_fingerprint。

`GET /market-reports`支持goal_version_id过滤。`POST /requirements/{id}/corrections`提交用户更正（expected_version、修改项、reason），形成新提取版本/修订引用并标记依赖待复核，不原地修改已冻结报告中的事实。

这里每个requirement_id标识一个不可变提取条目，更正返回新的requirement_id和supersedes_requirement_id；expected_version校验该条目所属RequirementCollection的revision，报告中的要求详情同时返回collection_id与collection_version。报告引用原条目ID，不跟随更正指针自动改值。更正必须仍有可定位的支持区间；主观学习目标用user_goal入口，不能伪装成JD原文要求。

GET `/duplicate-candidates`按goal_version_id或run_id列出疑似重复对、所涉岗位版本、理由、最新决定与version。POST `/duplicate-candidates/{id}/decisions`输入expected_version、decision（duplicate/distinct/unknown）、reason，追加DedupDecision并返回decision_id和version。服务端校验决定与已有重复组不矛盾；矛盾返回409并给出当前用户可读的冲突对。决定不删除岗位，不修改已发布报告；重新发起market_research可携带dedup_decision_ids，由报告冻结决定和dedup_policy。默认只合并已确定重复项，未决项单列并说明统计仍可能包含重复，不把当前口径声明为全量唯一职位。

## 6 M3 个人证据 目标与计划

### 6.1 证据

`POST /evidence`输入kind、title、description、attachment_ids、public_repository_snapshot_id（可空）、code_location（可空）、claimed_skills、declared_verification、environment。declared_verification只接受self_reported或attached_material等声明，不能写平台认证。

返回evidence_id、evidence_version_id、verification_scope和version。GET `/evidence`、GET `/evidence/{id}`用于列表/详情；POST `/evidence/{id}/versions`追加新版本（expected_version）；DELETE `/evidence/{id}`执行同来源删除规则。

### 6.2 能力对齐

`POST /alignment-runs`输入goal_version_id、market_report_id、personal_evidence_version_ids、previous_plan_version_id（可空）、assessment_ids（可空）、user_constraints。返回通用run。

GET `/alignment-reports/{id}`返回逐项alignment，包括requirement_ids、evidence_version_ids、category、reason、unknown_questions、objective_version_ids及plan_id/plan_version_id。

category只使用reusable/transfer_needed/depth_gap/prerequisite_gap/unknown。evidence为空并不等于prerequisite_gap；分类必须有支持或显式保留未知。

### 6.3 目标与条件对象

GET `/learning-objectives/{id}`返回稳定身份和版本；GET `/learning-objectives/{id}/versions/{version_id}`返回具体版本。

POST `/learning-objectives`提供手工入口：goal_version_id、title、target_behavior、scope、prerequisite_objective_ids、criteria、scope_confirmed=true；服务端将origin固定为user_goal，并在同一事务生成完整目标/条件版本，返回objective_id、objective_version_id、criterion_version_ids和version。缺少可观察条件时拒绝创建可执行目标。该入口支持用户直接进入实践，不要求先运行M1—M5。POST `/learning-objectives/{id}/versions`携带expected_version、完整新内容和change_reason；条件变化始终建立新版本，旧任务不自动改绑。

LearningObjectiveVersion的契约：

```json
{
  "objective_id": "00000000-0000-4000-8000-000000000020",
  "objective_version_id": "00000000-0000-4000-8000-000000000021",
  "title": "为一个有界后台任务实现恢复与防重复",
  "origin": "inferred_from_requirement",
  "requirement_ids": ["00000000-0000-4000-8000-000000000022"],
  "target_behavior": "在指定故障点后恢复，并保持业务任务不重复",
  "scope": "一个用户、固定输入版本、已约定的故障场景",
  "prerequisite_objective_ids": [],
  "effort_hours": {"min": 4, "max": 8, "is_estimate": true},
  "criterion_version_ids": ["00000000-0000-4000-8000-000000000023"]
}
```

示例工时仅展示结构，不作为用户真实任务估算。

AcceptanceCriterionVersion字段为criterion_version_id、objective_version_id、dimension（artifact/explanation/transfer）、scenario、expected_behavior、pass_rule、fail_rule、unknown_rule、critical、allowed_methods、required_artifact_types、limitations。criterion_version不可直接PATCH；条件改变需创建目标/条件新版本。

### 6.4 计划编辑与采纳

| 接口 | 语义 |
| --- | --- |
| GET `/plans/{id}` | 当前草稿/已采纳版本及成员 |
| GET `/plans/{id}/versions/{version_id}` | 固定版本的顺序、目标、时间和暂缓项 |
| POST `/plans/{id}/versions` | expected_version、base_plan_version_id、目标成员与顺序、调整理由；创建新草稿 |
| GET `/plans/{id}/diff` | from_version_id、to_version_id；按稳定objective_id生成保留/调整/移出 |
| POST `/plans/{id}/adopt` | 从源草稿及选中子集创建新的adopted计划版本与成员，更新稳定计划active指针 |

adopt请求：

```json
{
  "expected_version": 2,
  "plan_version_id": "00000000-0000-4000-8000-000000000030",
  "expected_goal_version_id": "00000000-0000-4000-8000-000000000001",
  "selected_objective_version_ids": ["00000000-0000-4000-8000-000000000021"]
}
```

请求中的plan_version_id表示源草稿。返回plan_id、source_plan_version_id、active_plan_version_id（新创建）、selected_objective_version_ids、version（稳定计划的新revision）。源草稿与已有版本均不可变；同一CareerGoal只有一个稳定LearningPlan。事务按CareerGoal→LearningPlan顺序加锁，验证expected_version与expected_goal_version_id，再创建最终版本和成员、更新active指针。目标详情的活跃计划字段由该指针派生，不单独更新一份副本。

必须验证依赖满足、选中集合无重复且属于源草稿、目标归属和当前goal版本；不可只采纳一个目标而遗漏它必需且尚未满足的前置项。草稿的kind不会因采纳被改写，新的版本创建为kind=adopted；是否为当前活跃版本通过稳定计划指针判断。

此接口不创建BuildTask，也不改变现有BuildTask.status。同键重试返回原新版本，不再次复制版本或成员；以新选择重新采纳则显式生成另一个版本，不自动复活已取消任务。

## 7 M4 仓库调研与选择

| 接口 | 输入或输出 |
| --- | --- |
| POST `/repositories` | github_url、requested_ref；登记候选身份，返回repository_id，不假装已经读取完整代码 |
| GET `/repositories` | 用户候选列表和快照状态 |
| GET `/repositories/{id}` | 仓库元数据、解析ref及已获取快照 |
| GET `/repository-snapshots/{id}` | commit、manifest、coverage、许可证状态和跳过原因 |
| POST `/project-research-runs` | objective_version_ids、candidate_repository_ids或candidate_snapshot_ids、search_mode |
| GET `/project-recommendations` | 按run_id/goal_version_id/objective_id筛选 |
| GET `/project-recommendations/{id}` | 相关条件、支持实现/仅文档声明、代码citation和未核实项 |
| POST `/project-selections` | recommendation_id、objective_version_ids、expected_goal_version_id |
| POST `/project-recommendations/{id}/feedback` | action=rejected、reason_type、reason；保存用户反馈，不删除旧推荐 |

search_mode在R1为curated；S09可增加bounded_search。候选ID和快照ID可按条目选择，但同一条目不同时指定两种输入。未解析ref在获取阶段固定commit，后续不重新读取main当前版本。

选择接口只接受已有效保存的推荐及其固定快照。结果为selection_id、repository_snapshot_id、objective_version_ids、selected_at。选择不同仓库是新的选择记录，旧学习单元仍指旧快照，不静默替换。

拒绝反馈的reason_type为difficulty/resources/time/interest/other。返回feedback_id与候选重选入口；再次调研仍显式创建project_research run，可携带feedback_ids作为新增输入，不在反馈接口暗中启动付费研究。

## 8 M5 项目学习

`POST /learning-path-runs`输入selection_id、objective_version_ids（R1一次聚焦一个）、criterion_version_ids、user_prior_knowledge（可空）、scope_constraints；返回run。服务端验证selection覆盖该目标，条件集合等于该ObjectiveVersion的完整条件集；范围需要缩小时先新建子目标和条件版本。

| 接口 | 用途 |
| --- | --- |
| GET `/learning-units` | 按目标/仓库/计划筛选 |
| GET `/learning-units/{id}` | 读取固定版本单元与证据 |
| POST `/learning-units/{id}/questions` | question、context_step_id；创建learning_path类型的有界追问run，仍受相同证据与预算约束 |
| PATCH `/learning-units/{id}/progress` | expected_version、completed_step_ids、actual_minutes、note；只更新学习进度 |
| POST `/learning-units/{id}/feedback` | kind=citation_error/explanation_error/scope_issue、detail、citation_ids；保存更正意见 |
| GET `/citations/{id}` | 返回引用来源、固定定位、availability与支持范围 |
| GET `/repository-snapshots/{id}/files` | path/符号过滤、cursor；按scope读取文件清单 |
| GET `/repository-snapshots/{id}/file-content` | path、start_line、end_line；路径正规化和总读取限制 |

LearningUnit输出：objective_version_id、repo_snapshot_id、why_this_unit、prerequisites、reading_steps、mechanism_explanations、understanding_questions、experiment、expected_observations、completion_conditions、task_drafts、unknowns、citations。

每个reading_step包括step_id、title、file/symbol范围、why、questions、citation_ids。task_draft包括task_draft_id、target_behavior、变更场景、deliverables、criterion_version_ids和effort范围。

勾选阅读完成只更新progress，不能新增Assessment或修改能力状态。追问输出若超出已读范围，应补读或返回未知，不编造文件与行号。

LearningUnit正文不可变，进度有独立revision。反馈返回feedback_id并在详情显示待复核；如需重新生成，通过新的learning_path run传入previous_learning_unit_id和feedback_ids，返回带supersedes_unit_id的新单元。原单元、任务草稿和已物化任务不被静默替换。

## 9 M6 自主构建 提交与验收

### 9.1 自主项目与任务

| 接口 | 输入或用途 |
| --- | --- |
| POST `/build-projects` | title、problem_statement、constraints、evidence_refs、public_repository_url（可空） |
| GET `/build-projects` | 当前用户项目 |
| GET `/build-projects/{id}` | 场景、当前任务与成果概况 |
| POST `/build-projects/{id}/tasks` | 从草稿物化或手动建立任务 |
| GET `/build-tasks` | project_id、status、objective_id过滤 |
| GET `/build-tasks/{id}` | 状态、任务版本、条件和提交 |
| POST `/build-tasks/{id}/versions` | expected_version、内容调整；产生新任务版本，旧提交不改绑 |
| POST `/build-tasks/{id}/transition` | expected_version、target_status=in_progress或cancelled、reason |

任务改版只允许todo/in_progress/submitted/needs_revision；accepted先撤销有效确认，cancelled不允许原地改版。新版本清空current_submission_id，保留历史提交；todo改版保留todo，其余可改版状态变为needs_revision。`GET /build-tasks/{id}`明确返回current_task_version_id、current_submission_id及version（revision），前端不得混用三个值。

创建任务请求至少含objective_version_id、criterion_version_ids、title、description、deliverables、prerequisite_task_ids；从学习单元物化还含learning_unit_id和task_draft_id。服务端验证目标属于已采纳计划或用户明确创建的当前目标，且任务草稿、LearningUnit、ObjectiveVersion和条件版本确实相互关联。

BuildTaskVersion保存由服务端读取的完整条件集合，客户端列表仅用于核对，必须集合相等且无重复。省略关键项、混入另一个目标的条件、用同owner的较容易条件替换均返回422 CRITERIA_SET_MISMATCH；资料已经换版导致前置版本失效返回409。想改变范围必须创建新的目标/条件及任务版本，不能通过请求子集降低验收标准。提交、审阅与确认继续使用这一冻结集合。

以`(build_project_id, task_draft_id)`保护草稿重复物化；同草稿发生内容冲突返回409并要求生成新草稿/版本，不能悄悄返回语义不同的旧任务。手动新任务靠Idempotency-Key防重，是否业务上重复可提示用户但不强行合并。

依赖任务未满足时不能进入正常开始状态；允许用户提交已有成果，但在最终accepted前仍检查验收条件是否依赖未满足的前置要求。状态accepted只通过验收确认事务产生。todo/in_progress/submitted/needs_revision可显式取消；accepted必须先撤销有效确认进入needs_revision，再取消。cancelled不隐式复活。

### 9.2 提交

`POST /build-tasks/{id}/submissions`请求：

```json
{
  "expected_version": 3,
  "task_version_id": "00000000-0000-4000-8000-000000000040",
  "criterion_version_ids": ["00000000-0000-4000-8000-000000000023"],
  "summary": "提交本次实现与实验材料",
  "code_commit": null,
  "attachment_ids": ["00000000-0000-4000-8000-000000000041"],
  "environment": {"os": "待填写", "python": "待填写"},
  "execution_records": [],
  "explanation_answers": [],
  "actual_minutes": null
}
```

execution_records包含command_text、reported_exit_code、output_attachment_id、environment_refs、reported_at、execution_provenance=user_reported。服务端不会执行command_text。用户不能指定可信runner身份。

criterion_version_ids必须等于服务器读取的task_version_id完整条件集；创建Submission时冻结该集合与代码/附件版本。不得用客户端缺省值或较短列表替代服务端条件。附件及其根绑定在事务中重新校验。

返回submission_id、submission_version、task_status=submitted、current_task_version_id、current_submission_id与新的任务revision。允许todo/in_progress/needs_revision直接提交；对accepted任务先撤销确认，或建立一个独立新任务。创建Submission同时更新current_submission_id，不能覆盖旧提交或旧验收。

GET `/submissions/{id}`读取固定提交；GET `/build-tasks/{id}/submissions`读取历史。

### 9.3 模型辅助审阅

`POST /submissions/{id}/assessment-runs`输入criterion_version_ids（与提交完整集合一致）、review_scope、budget_profile。返回assessment类型run。review_scope可聚焦解释细节，但不能删除条件；无法审阅的条件仍逐项返回unknown及缺失理由。服务端从Submission及其BuildTaskVersion加载完整集合，不以请求列表驱动少评几个条件。

GET `/assessments/{id}`输出：

- submission_id、criterion_version_ids、proposal_version、version（Assessment revision）；
- items：criterion_version_id、verdict、method、evidence_refs、rationale、limitations；
- artifact_evidence、explanation_evidence、transfer_evidence；
- suggested_task_outcome、missing_evidence、plan_delta_proposal；
- confirmation（未确认时null）、generated_by_run_id。

模型建议method为model_review；读到用户执行记录时需保留原execution_provenance，不能因此写为trusted_execution。

### 9.4 人工确认与撤销

`POST /assessments/{id}/confirm`输入：

| 字段 | 内容 |
| --- | --- |
| expected_version | Assessment当前revision |
| expected_task_version | BuildTask当前revision，防止审阅期间任务改变 |
| decision | accept / request_revision |
| criterion_decisions | 完整冻结条件集的逐项确认verdict、method、evidence_refs、reason；不允许子集或重复项 |
| supplementary_evidence_ids | 本次新增依据，可空 |
| scope_acknowledged | 确认本次结论只覆盖给定环境和版本 |

服务端根据实际登录用户记录assessor_id和self_review=true。V1 human_review只代表当前用户实际自验；用户上传他人意见仍是user_reported，不能写入他人身份冒充独立评审。

确认时服务端核对Assessment→Submission→BuildTaskVersion的真实关系，要求task_status=submitted、submission_id等于current_submission_id、提交内容版本仍是任务当前内容版本、expected_task_version仍等于稳定任务revision，并读取冻结的完整条件集校验criterion_decisions。同一用户的较容易条件也不可替换本次条件。返修或撤销后需形成新Submission再确认；旧提交的晚到评审只保留历史。

accept必须满足全部关键条件通过且材料要求满足；原unknown关键项不能只靠点击确认改为pass，须新增对应证据或可追踪的分项自验记录，另记新的分项审阅记录并保留原模型提案。对程序执行类条件只有静态模型审阅时仍unknown。request_revision可以包含fail/unknown并设置任务needs_revision。

确认事务追加不可变AssessmentReview及其完整分项决定，创建引用review_id的AssessmentConfirmation，并更新任务状态/revision、Assessment revision、相关证据和事件；原模型提案保持不变。返回assessment_id、review_id、confirmation_id、task_status、current_task_version_id、task_revision、assessment_revision和plan_delta_proposal_id（可空）。同键重试不会新建第二份review/confirmation。计划差异只是一份建议，不在确认时自动采纳计划。

`POST /assessments/{id}/revoke`输入expected_version、expected_task_version、reason；撤销本次仍有效确认后，关联accepted任务进入needs_revision，保留历史。已撤销请求可幂等返回；旧确认已被后续确认取代时拒绝错误回滚。

GET `/plan-deltas/{id}`读取base_plan_version_id、assessment_id、拟保留/调整/暂缓/新增目标、理由和引用。用户决定重新规划时，通过alignment-runs显式携带该assessment_id、previous_plan_version_id与当前目标版本，产生新草稿，再调用adopt。查看差异不自动改变当前计划。

## 10 运行控制与事件

### 10.1 运行详情

GET `/runs`提供工作台列表，支持goal_version_id、run_type、status（可多选）、cursor和limit（默认20、最大100）；按created_at、run_id倒序稳定分页。仅查询当前用户的运行，返回run_id、run_type、status、version、stage_summary、waiting_input摘要、output_refs、updated_at及next_cursor。最近运行与waiting_user待处理项都从服务端重新获取，不能依赖浏览器上次会话缓存的ID。

GET `/runs/{id}`返回run_type、status、version、goal_version_id、stage_summary、output_refs、waiting_input、usage、error_summary、created_at、updated_at、last_event_seq。普通前端无需拿到worker凭据、内部执行上下文或私人提示词。

waiting_input包括wait_request_id、question_version、question、response_schema、schema_hash、input_manifest_hash和expires_at（可空）。wait_request_id具有稳定业务身份，唯一键对应run_id、stage_key、input_fingerprint、question_version、wait_sequence；这些内部字段不允许客户端覆盖。过期不代表用户同意；输入无效时返回字段错误，不自动继续。

### 10.2 恢复

POST `/runs/{id}/resume`输入expected_version、wait_request_id、input_manifest_hash、response。首次接受决定仅支持waiting_user。服务端验证当前等待身份、问题schema、目标和输入可用，在一个事务保存以wait_request_id唯一的ResumeDecision、queued、run.resumed与outbox后返回202。相同幂等键重试复用首次受理结果；其他重复或过期等待返回409 STALE_WAIT/RESUME_CONFLICT，不产生第二个决定。

正常等待恢复与异常 Worker 接管均从冻结输入、有效 StageArtifact 和稳定 WaitRequest/ResumeDecision 继续；reaper 以 DB 时间和行锁撤销过期 epoch，客户端不能指定执行资格。API 接受回复后失联不重问，问题/输入/schema 变化不复用旧答案。保持 run_id 与累计预算。workflow_version 必须与注册的阶段定义兼容；不需要 thread_id、interrupt_id 或独立 checkpoint。

failed/budget_stopped/cancelled等终态不接受resume。用户重新发起对应类型的新run，可携带retry_of_run_id以关联旧运行，并明确新预算；不得暗中让旧终态继续计费。

### 10.3 取消

POST `/runs/{id}/cancel`输入expected_version、reason（可空）。对queued/running/waiting_user生效，服务端撤销执行资格并保存cancelled事件；重复取消返回原结果。已经成功结束的运行返回状态冲突，不把成功历史改成取消。

取消后仍允许记账晚到usage，但禁止发布晚到业务产物。响应说明已停止后续调度，可能有外部在途费用；不承诺取消瞬间停止供应商全部计费。

### 10.4 事件读取

GET `/runs/{id}/events?after_seq=0&limit=100`默认返回JSON，支持上限200和next_cursor。Accept为text/event-stream时进入SSE；先回放已保存事件，再等待新事件。Last-Event-ID采用`run_id:seq`，与query的after_seq同时出现必须一致。

事件示例：

```text
id: 00000000-0000-4000-8000-000000000010:7
event: run_event
data: {"run_id":"00000000-0000-4000-8000-000000000010","seq":7,"schema_version":1,"type":"stage.completed","created_at":"2026-10-12T02:00:00Z","payload":{"stage":"extract_requirements","output_refs":[],"coverage":{"processed":3,"total":4}}}

```

以上数字为结构示例，不是实际运行结果。

V1业务事件类型：run.created、run.started、stage.started、stage.completed、stage.failed、tool.summary、run.waiting_user、run.resumed、run.recovering、budget.updated、run.finished、run.cancelled、input.revoked。字段新增是兼容变更；含义变化需提高schema_version并维护兼容。

run.finished携带真实AgentRun终态和output_refs，不能简单转发模型的final消息。前端重连后依据持久化seq去重。格式错误、run_id不一致、query/header游标冲突或after_seq超出当前最大值返回400；已授权运行的所需历史已清理则返回410 EVENT_HISTORY_EXPIRED及详情重取指引，不伪造连续事件。SSE在发送响应头前完成初次游标校验；连接中发现历史缺口时发送不占业务seq的控制通知并关闭，客户端重新读取详情与可用事件边界。

## 11 引用与证据契约

SourceCitation：citation_id、source_snapshot_id、fragment_id、char_start、char_end、text_hash、availability。字符区间以规范文本Unicode code point计，左闭右开。

CodeCitation：citation_id、repository_snapshot_id、commit_sha、path、start_line、end_line、content_hash、availability。行号1起始、两端包含。

两类目标必须二选一。availability为available/revoked/missing/invalid；invalid不得显示为有效引用。support_level表示implementation_observed/documentation_only/inferred/unverified等证据层级，与引用能否打开分别呈现。

每次工具返回正文需带evidence_refs和truncation信息。读取不完整、tree被截断、文件超限时必须可见；工具不能返回未经检验的“全文已读”标记。

## 12 事件与状态契约测试清单

接口契约至少覆盖：同键同payload复用、同键异payload冲突、stale expected_version、所有ID归属、CSRF与登录失效、输入已撤销、非法状态转移、重复任务物化、旧验收不得覆盖新任务、resume双提交、SSE补读、JSON与SSE事件一致、未知事件类型容忍、引用区间规则和附件类型/大小限制。

从 OpenAPI 生成前端类型后，CI应检测接口变更；不能把mock接口一直保留为真实页面的数据来源。FakeModel和合成数据在界面/测试记录中明确标记，真实演示切换到实际材料和明确预算。

与 [产品需求](01_PRODUCT_REQUIREMENTS.md) 或 [技术设计](02_TECHNICAL_DESIGN.md) 有差异时，先记录决策并同步文档，再实现；接口与页面不能各自临时改语义。
