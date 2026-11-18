# 1.6.3：有界研究与任务覆盖修复

本文说明已实现的机制，不提供单题答案，也不宣称掌握了被导入项目。仅适用于新修复副本；原项目与封存的评测源码不属于修改范围。最终验收以项目根目录 [DELIVERY_REPORT.md](../DELIVERY_REPORT.md) 和 [RELEASE_STATUS.json](../RELEASE_STATUS.json) 中明确标注 1.6.3 的最终记录为准。旧版本记录不证明本版通过。

## 1. 保留架构，分开事实成立与任务完成

主链仍为 **Source → Analysis → 独立 Review → Claim/Fact 门禁 → Publication → Learning**。Analysis 提供候选与实际读取证据；Reviewer 先独立调查并冻结读取包，再看到 Analysis 的选定证据。原有逐 Claim 决议、事实绑定审核及发布门禁继续执行，不以任务覆盖评估绕过。

新构建另为根任务预留一次独立充分性审核。它只接收已通过审核并发布的事实包，以及服务端解析的来源、候选和 Claim 身份；不能自行添加发布物、改变事实支持或扩大授权。最终写入和 API 读取再次校验 typed publication pack，避免用失效审核支撑旧覆盖结论。

`task_coverage.complete` 检查原始问题、必需结构维度和每项要求；事实已发布不等于任务完成。未回答问题、未知项、深度不足或充分性审核无效会留下缺口。Analysis 可以没有可用单元，不必编造；也可成功产出有用事实而保留未回答问题。根任务有缺口时为 `partially_succeeded`，不因任务未完成而抹掉已经合法发布的事实。

源码：`build.py` 的 `review`、`_published_evidence`、`_review_task_sufficiency`、`_save_build_result`；`research_coverage.py` 的 `evaluate_task_coverage`；`application.py` 的根任务状态映射；`api.py` 的结果投影校验。

## 2. 有版本的私人研究合同

- `research-task-1.6.3` 冻结 `original_question`、研究范围、目标版本、逐项 requirement ID、原文引文、目标深度、所需解释维度和未解析要求。`research-coverage-1.6.3` 承载 Analysis、Review 与最终充分性审核的覆盖评估；旧基础输出模型没有被就地替换。
- 项目范围默认要求 `project_structure`、`module_responsibilities`、`call_flow`，追加维度不能删除这些默认要求。概念范围默认要求 `purpose`、`boundaries`；聚焦范围需显式选择。文件树、符号存在和 README 描述不能单独证明模块职责或调用关系。
- 要求深度按材料支持解释：了解要求 purpose；熟悉再要求 implementation、boundaries；掌握再要求 tradeoffs。这是材料充分性，不是用户能力认证，也不是读文件或调用次数的阈值。
- `known` 必须有可解析到实际已发布事实的锚点，并匹配同源、候选范围内的独立评估；要求深度及其各维度必须满足目标。Claim 的 `conditions` 和 `kind` 随证据保留。某项目 `not_applicable` 不能消除整个 JD 的要求，浅事实的数量不能补齐缺失的深度。
- 私人目标只能用于本人私域构建，不能混入公共构建。目标所有者、命名空间、原文及要求快照、撤销状态、依赖闭包和外发数据类别在相关阶段重新核验。覆盖与目标元数据保留在 owner-private 任务产物中，进入知识持久化前剥离；这不是对任意模型提示注入的完美语义防泄漏承诺。

源码：`contracts_research_v163.py`；`research_coverage.py` 的 `freeze_research_task`、`read_goal_version`、`validate_research_run`、`evaluate_task_coverage`；`build.py` 的 `prepare_in`、`_freeze_research_context`、`_research_data_classes`。

## 3. 多轮调查、预留预算与恢复

`bounded-investigation-1.6.3` 在入队前冻结每个调查会话的限额。充足预算下，Analysis 最多 4 轮、16 次工具尝试、12 次读取尝试；每个 Reviewer 会话最多 3 轮、12 次工具尝试、9 次读取尝试。每批最多 3 个工具，bootstrap 最多读 1 个实现窗口，优先代码而非预先消耗多次 README 读取。树、搜索、bootstrap、停止操作与失败尝试均受工具总额约束；这些是上限，不保证用满，也不等于有效知识量。

轮数不是无条件追加费用。`_plan_investigations` 在现有 `call_cap` 内选择可容纳的 Analysis/Reviewer 轮数，并保留 Analysis 最终输出、Reviewer 最终决议、事实覆盖审核与根充分性审核等固定开销。最低合同也无法容纳时，在提取 JD 和模型调用前返回 `BUILD_BUDGET`，不扩大用户额度，不跳过独立审核。

每次工具操作先持久化不可变 attempt reservation，再执行可能提交读取记录的操作。失败尝试也消耗工具/读取限额；超量批次整批不执行并按剩余额度计入尝试。可纠正参数错误作为有限观察返回，后续轮次可修正；权限、隐私、租约、哈希和回放失败不进入纠错循环。单行超限与读取额耗尽明确不能靠相同请求解决。

恢复必须匹配冻结合同、参数哈希、来源、角色和会话。若崩溃发生于读取已提交、工具结果未提交之间，按精确读取身份找回记录，不重复读取或扣额；更换键或实例不能重置持久计数。已记录的致命失败不能自动重试，已持久化的 finish 会阻止新工具操作。模型和工具收据用于重建相同顺序的观察，不重复追加。

源码：`build.py` 的 `DEFAULT_INVESTIGATION_CONTRACTS`、`_build_costs`、`_plan_investigations`；`investigation.py` 的 `_durable_usage`、`_reserve_tool`、`execute_tool`、`_reject_batch`、`run`；`sources.py` 的 `recover_read_span`。

## 4. 导航与整行证据

- 新树工具按代码优先、路径排序，以 offset 分页，每页最多 100 个文件并给出 `next_offset`。它只覆盖已经导入的快照。
- `find_symbol(exact=true)` 匹配完整符号名或其最后一段，区分大小写，不把文本子串当精确符号。短符号可有多个候选，仍须按路径和实际读取消歧。搜索与 references/test 候选只是导航线索，不是已读证据或已解析运行调用图。
- 新 `whole-line-pages-1.6.3` 读取每页最多 70 个完整原始行、2600 个字符，保留原始行文本、实际闭区间和哈希。`continuation` 指向原请求剩余范围，尚未读取时不可引用；不截断单行、不在证据里插入省略号。
- 首行超过 2600 字符时明确返回 `READ_LINE_LIMIT`，不创建虚假读取收据；越界请求也不能以截取文本冒充有效证据。原有未启用分页的 `read_span` 保持整段成功或报错的合同。引用仍须是本角色本会话实际读取文本中的连续、精确、唯一摘录。

源码：`investigation.py` 的 `_invoke_bounded`、`_bootstrap_bounded`；`sources.py` 的 `search`、`_read_window`、`_read_response`、`read_span`、`evidence_from_read`。

## 5. 旧目标与回放兼容

`iter_requirement_items` 支持带 requirements 的包装对象、旧列表、单条字典和 keyed 旧字典，不把字符串或包装元数据当要求。合法旧 key 优先作为身份，否则从稳定内容生成 ID。冲突 ID、缺失/无效深度或不可信引文保留为未解决项，不猜测补齐；部分旧字典仍可作为学习目标原文引用。

新 `learning-text-catalog-1.6.3` 文字引用带目标版本与 requirement 身份、目标深度，研究与学习共享识别方式。未标记的历史目录保留原位置 ID 和原请求内容；不改写已付费收据、既有输出或冻结哈希。学习读取已注册目标时同样核验撤销与要求快照，读取路径不会自动重新注册旧目标来绕过限制。

新研究合同只由新入队任务显式标记。未标记历史任务继续使用旧 Analysis/Review schema、调查路径和配额；未知研究版本拒绝重放。兼容不是保证任何旧任务均可执行：来源、资金、租约与已有安全门禁仍然适用。

源码：`research_coverage.py` 的 `requirement_identity`、`iter_requirement_items`、`normalize_requirements`；`learning_projection.py` 的 `TEXT_CATALOG_VERSION`、`text_catalog`；`learning_ext.py` 的 `_goal`；`build.py` 的 `prepare_in`、`_research_context`。

## 6. 有界快照不等于全仓或全 JD 掌握

保留原导入边界：最多 1000 个 UTF-8 文本文件、每文件 100000 字节、合计 12000000 字节；单行导入上限为 25000 字节，与证据页的 2600 字符上限不同。安全路径过滤仍排除私密、依赖及生成目录。GitHub 归档导入另受后缀白名单、符号链接/无效文本过滤、下载与解压限额约束，跳过计数随来源元数据保留。

因此，被排除或未导入的实现、未读分页、未解析语言和耗尽预算后的问题继续是缺口。符号/文件/读取/轮次数、静态测试源码及某次成功发布，都不能证明全仓实现被理解、测试实际运行、全部 JD 已满足或学习者已经掌握。源码调查不执行被导入项目。

源码：`util.py` 的 `safe_path`、`validate_files`；`adapters/github.py` 的 `snapshot`、`unpack`；`sources.py` 的解析与读取合同。

## 7. 当前证据与已确认限制

2026-11-04 的 [FINAL_INDEPENDENT_AUDIT.md](../../../evidence/independent_audit/FINAL_INDEPENDENT_AUDIT.md) 记录：**233 个唯一测试通过，11 个可选解析器测试跳过，包括 27 个独立编写的对抗案例**。这是冻结代码的有界离线机制/安全审计，不是完整仓库测试或真实模型质量结论。审计覆盖了已发布事实保留、目标撤销、读取崩溃恢复、发布包再验证、低预算预检、Claim 条件和私域外发范围等已复现问题。

完整离线回归及主A/B现已结束：1151通过、13跳过、4项缺依赖失败，另27项未运行；主B五仓0发布（三次DEADLINE、两次独立Review的AMBIGUOUS_QUOTE）。单独90分钟Mini与隔离导航/学习探针不能替换原主结果。详细实际结果以项目根交付报告及发布状态为准，不宣称多仓默认期限可用性或生产验收通过。当前环境缺少 Redis/Celery、可选 tree-sitter 等依赖，且没有 Docker/Podman、PostgreSQL/Redis 服务；不能把跳过或未运行的基础设施、分布式并发、生产 HTTP 提供商测试当成通过。本轮审计没有付费 API 调用或生产测试。

实施与独立审计使用 Astra 6/max；真实评测保留全新 gpt-6.1-sol/high，不通过更换评测模型掩盖机制变化。模型配置标签本身不等于独立后端身份认证。最终范围、通过/失败/跳过/未运行、真实 A/B 结果及环境限制须查根目录 [交付报告](../DELIVERY_REPORT.md) 与 [发布状态](../RELEASE_STATUS.json)；历史 1.6.2 及更早记录仅作历史证据。
