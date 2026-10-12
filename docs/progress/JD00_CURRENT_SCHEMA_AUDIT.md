# JD-00 现状审计 · 基线 01abdf5

| 现有代码/实体 | 目标 | 增量映射 |
|---|---|---|
| source_service / Source / SourceSnapshot | L0/L1 | 保留旧快照，追加有类型的 JobContractArtifact；raw bytes 与 canonical hash 分离 |
| import_service / JobPostingVersion | 发布身份 | 复用源关系；新协议用 provider/board/真实发布 ID 或页面身份，手动无 URL 独立 |
| domain/jobs / RequirementCollection / Requirement | L2 原子事实 | 新 AST 与精确区段引用；旧词法要求只作历史兼容，不升级成已确认事实 |
| market_report_service / MarketReport | 固定统计 | 新报告附带 DemandReport 协议，独立计算 OR/AND、资格和分母；旧报告保留 |
| planning_service / alignment workflow | L3 | 新输入服务端冻结 PlanningContext；旧计划原有版本与编辑接口保留 |
| maintenance_service / Source.revoked_at | 失效 | 新协议读取递归检查源、父对象、固定证据；扩展受控擦除，不启用开发库批量清理 |

协议对象共用追加式、owner 复合外键的版本表，kind 区分 capture/snapshot/fact/report/context；不按参考文档强制复制五套表名。raw 受控存于同库，不接受客户端 blob 路径。新版本不覆盖历史事实。按用户最新要求自主构建已下线，不恢复 M6。参考文档内要求的全量回归与真实模型对照不自动执行，遵守用户只做必要验证及离线范围。
