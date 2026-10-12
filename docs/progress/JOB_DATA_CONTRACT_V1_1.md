# 岗位数据标准化 V1.1

基线：01abdf5。迁移：af4ebdc49e86（仅增加 job_contract_artifacts 及索引；没有重建或清空旧表）。

## 实现与使用

来源页导入 JD：选择 manual_import / greenhouse / lever / ashby / json_ld。平台只处理用户粘贴的已保存单岗位对象，不联网抓取。ATS 输入不构成平台官方采集证明。来源详情的“岗位标准化”展示原始内容、标准字段、规范正文、提取候选及核对入口。人工确认需填写理由，修订会追加版本，保留旧版本及审核身份和时间。

L0 保存原始 UTF-8 字节、SHA256、来源、读取状态和使用策略；L1 保存稳定 posting_id、独立 job_snapshot_id、规范文本哈希和 Unicode 字符区间。无 URL 的手工岗位有独立身份，不因文字相同被自动合并。Greenhouse id 与 internal_job_id 分开，更新时间不作为首发；Ashby 不补造外部 ID、发布日期采用 last_published；JSON-LD 独立标为 json_ld。无时区日期不猜测 UTC，原值保留在 capture.provider_metadata.original_date_fields 中。

L2 保留 concept / constraint / all_of / any_of 树；规则覆盖固定字典，未识别的文字仍在规范正文中，不声称完整抽取。自动提取不等于确认。确认明确原文后，报告按固定岗位快照、事实版本、目标范围、去重决定和字典版本生成；独立必需、必需可选项、优先和职责分开计数，资格条件单列。跨来源疑似重复先排除，人工裁决后重跑；同一来源范围的不同外部岗位 ID 不按相同正文自动折叠。报告保留分母、排除原因和证据。

现有“生成离线草案”在标准报告上先由服务端构造 PlanningContext，再沿用现有后台队列。新上下文固定报告、个人材料版本、目标哈希；修订过事实的报告不能用于新上下文，但旧报告仍可查看。OR 选择一个分支，AND 独立项保留；默认选择原文首个分支并标为草稿待确认，通过上下文 API 可以提供语言偏好。学历、出勤等只形成待本人确认的资格检查，不生成学习任务。静态个人材料不证明本人贡献或实际运行。新计划仍在已有计划页面编辑，不恢复自主构建功能。

## API

- POST /api/v1/job-data/import
- GET /api/v1/job-data/source-snapshots/{source_snapshot_id}
- POST /api/v1/job-data/source-snapshots/{source_snapshot_id}/normalize
- GET /api/v1/job-data/artifacts/{id}（capture 原文仅所属用户可读）
- POST /api/v1/job-data/facts/{id}/versions
- POST /api/v1/job-data/planning-contexts
- POST /api/v1/job-data/planning-contexts/{id}/alignment-runs

仍保留 /market-research-runs、/market-reports 和 /alignment-runs 的外部入口。新报告通过 job_contract 字段返回新协议，不改变历史报告的 payload。

## 兼容、回填与回滚

历史来源缺失 raw 时，只做带 partial_text/partial 标签的兼容快照，注明 original_raw_recoverable=false，不能进入正式分母；不会虚构抓取字节或日期。来源页可逐条回填，也可执行脚本，默认只读预览：

```
uv run --project backend python scripts/backfill_job_contract.py --owner-id <UUID>
uv run --project backend python scripts/backfill_job_contract.py --owner-id <UUID> --apply --batch-size 50
```

回填按快照 ID 分批提交，重复运行跳过已生成版本，源撤销优先，失败可重跑。没有对开发库自动批量回填。现有 mock 报告和计划不会被覆盖；要将历史 JD 作为正式标准统计输入，请重新导入完整原文并逐条确认。

CAREERFORGE_JOB_CONTRACT_WRITE=true 控制新的标准化写入与入口；CAREERFORGE_JOB_CONTRACT_READ=on 控制新市场报告切换。回退新任务：WRITE=false、READ=off，重启 api/worker/dispatcher；保留新表供历史标准报告授权读取，不执行 downgrade 删除新版本。shadow 值保留旧报告路径，同时可写入标准快照；它不表示已完成全量统计影子对照。

撤销原来源后，capture/fact/report/context/派生目标和幂等重放都会重新校验来源权限。清理程序已扩展新产物及 raw_bytes；自动维护仍关闭，本轮未在开发库执行物理清理。

## 必要验证

新隔离库 careerforge_contract_validation_20261012 从既有迁移升级至新 head。参考协议 10 项、离线适配/逻辑/Unicode 8 项、端到端 2 项；仅运行这些相关用例及前端构建。端到端覆盖二选一统计、资格未知、上下文指纹、不可变修订、跨账号隔离、来源撤销、人工去重裁决与过期报告阻止新规划。真实模型调用/费用未测；不跑全量回归，不宣称完成联网适配器或生产质量评估。

## 本地交付记录

已部署到 http://127.0.0.1:8080，来源页面 HTTP 200，开发库迁移 head=af4ebdc49e86。迁移前备份保存在忽略的 private/db_backups/before_job_contract_v1_1_20261012.dump（SHA256：4fd8114f4c6cacb358720e9629ae3c0c1cc6dbf89a0a83f25cc039a94b3e41f4）；未执行物理清理或覆盖现有 mock 计划。
