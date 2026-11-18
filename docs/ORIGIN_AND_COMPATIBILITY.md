# 1.6.0 来源与兼容

## 固定基线只读核对

本包增量修改的是用户上传的独立 v1.5 实现包，不是原工程完整检出。两份原始附件及引用的 v1.2.1 原文保存在 `docs/specifications/`，未为适配实现降低验收要求。

已通过 GitHub 只读 connector 核对指定原仓库 `Sw1ven-Wang/CareerForge@90e32f187ba44f9273cd3e2c7c12af2aeaba5e93`：完整树返回507条且 `truncated=false`；实际读取 README、MIT LICENSE、旧数据库models/study_models、OpenAPI、前端API客户端、Tauri本地命令。下载字节长度与Git blob SHA1核对；清单在 `validation/v16/baseline/inspection.json`。没有执行旧代码、访问用户旧数据库或修改仓库。

旧API是cookie/CSRF及data封装；本包是Bearer与新合同。旧OpenAPI123个path不能因为少数同名就视为兼容。原Vue/Tauri及全部旧表/旧回归未移植。不要覆盖原目录或把本包migration连接到旧原库。

## 已实现的兼容桥

`careerforge legacy-export --help`：仅对显式owner UUID执行白名单内容表只读导出。SQLite采用 `mode=ro/query_only`，PostgreSQL采用 `REPEATABLE READ, READ ONLY`；数据库端数量/字节预检和128行流读，超出10000条/8MB完整失败，不静默截断。要求明确 `--confirm-read-only-export`；DSN从指定环境变量取得，不进入命令参数/通用错误正文。输出新建0600文件，拒绝覆盖。

`careerforge legacy-import --help`：输入hash校验的归档，仅进入本人私人 `LegacyImportBatch/Record` 未审核区，幂等/有界分页。**不导入账号凭据、会话、付款权限、旧审核资格或已发布知识**。另有显式Source导入入口，只允许固定仓库/Commit与文件、许可/权利确认；仍需新Analysis/独立Review。公共共享任务不能隐式采纳私人归档。

白名单表定义以 `contracts_compat.LEGACY_TABLES` 为准；其他结构拒绝猜测。源正文可能本来含秘密，导出文件须由本人保管，不是自动脱密器。测试用合成旧库证明只读、owner隔离、NaN拒绝和超大行预检；没有访问真实用户数据。

## 本包自身数据库升级

- `cf160_features` 在 `cf151_command_binding` 之上增加新表并为已存在principal建立generation=0隐私状态；不补造旧知识审核/授权。
- 本包新库创建并标记确切head；非空未标记库、错误/多个head、缺表/缺列均拒绝自动认领，readiness不报健康。
- 仅对确认来源且已有正确迁移历史的本包数据库，在备份和隔离副本演练后 `alembic upgrade head`。不提供破坏性降级。
- 原上传v1.5包和完整1.5.1检查点保留，不覆盖原件。旧证据在 `validation/current/`，1.6证据在 `validation/v16/`。

## 来源保留与版本含义

`coding-agent/runner.mjs`基于原固定提交Pi JSONL运行器调整，原blob `f3b5d2d8e72d68bd1d90a042cdca061e2baa0f38`；保留MIT License（Copyright 2026 Sw1ven-Wang）。Pi1.3.0声明依赖，SDK/node_modules不随包捆绑。未冒称安装或集成GPT Researcher、DeerFlow、DeepWiki、ACE、GEPA、SkillOpt。

1.6.0是本独立实现的增量版本，不表示原仓库完全升级或生产验收通过。以 `RELEASE_STATUS.json`、`DELIVERY_REPORT.md` 和 `KNOWN_GAPS.md` 为准。
