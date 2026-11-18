# CareerForge 1.6.0 交付报告

**2026-11-02｜仅 dot 工作空间｜开发和复审配置：Astra 6 / max**

已在保留 1.5.1 检查点的基础上补齐附件指定的可实施功能；这是可调用、可重现的独立后端/CLI版本，**不是原 Vue/Tauri 的完整原地升级，也不宣称全部产品或 Docker 验收完成**。

## 1. 末版统一门禁

| 检查 | 真实结果 | 权威证据（`validation/v16/final_verified/`） |
|---|---|---|
| 全量唯一用例对账 | **666 passed = 639 离线 + 27 真实原生基础设施，0 failed/error/skipped，0遗漏/重复** | `summary.json`、`accounting_assertions.log`、4份JUnit |
| PostgreSQL17.11 / pgvector0.8.0 / Redis7.0.15 | **通过**：真实SQL、向量与ACL、共享构建/预算/锁、隐私栅栏、大图游标、治理CAS | `native_main.xml`、`native_retrieval.xml`、`native_governance.xml`、native harness对应日志 |
| 实际 Celery/Outbox | **通过**：全新库迁移、独立真实worker、队列→PG；无模型收据 | `production/production.json` |
| Ruff lint / format、Python编译、Node语法、pip check | **全部通过** | `static_checks.json` |
| 项目默认mypy，禁增量缓存 | **Success: no issues found in 72 source files** | `mypy_full.log` |
| 实际Pi SDK1.3.0、JS/TS/TSX/Go语法库 | **通过工程测试**；Pi推理仍synthetic | 全量JUnit及对应测试源码 |
| Wheel构建/源码外独立目录安装 | **通过**；新库90表、30种子Topic、0 Publication | `wheel_build.log`、`wheel/wheel_install.json` |
| Docker Compose实际解析 | **通过** | `docker/` |
| Docker镜像/容器/隔离 | **BLOCKED / NOT_RUN**，不是通过 | `docker/docker.json`、历史rootless启动证据 |
| 真实模型/Gold/教学收益/生产负载与灾备 | **未验收**；付费模型调用0 | `docs/KNOWN_GAPS.md` |

**本版完整覆盖率未取得，不报百分比。** 首次覆盖率尝试在1200秒处超时，未输出完整JUnit/coverage；它还暴露了已修正的旧错误码断言。末版全量串行运行未开启覆盖率插桩。测试数不是规格覆盖或模型正确率。未把真实PG/Redis/Celery说成Docker；未把SDK/模型fixture说成真实推理质量。

最终统一冻结校验覆盖 164 个源码/测试/配置文件。1.6第一次覆盖率全量尝试在1200秒处超时，不能计算通过总数；运行中维护资金来源限制测试出现旧错误码断言：中央门禁更早正确返回 `FUNDING_SOURCE_SCOPE`，没有越权调用。只修正精确断言后，重新执行全量门禁；首次失败保存在 `validation/v16/final/`，不得混计为通过。其他中间失败/修复也保留，不覆写历史。原生首次混合模块共用库得到25通过/1失败：公共fixture产生多个默认快照，HNSW全域探针正确拒绝PROJECT_VERSION_AMBIGUOUS。未改产品或断言；改为主组23、检索3、治理1各自全新库后全部重跑。原25/1证据在`validation/v16/native_shared_fixture_attempt/`，不加入当前统计。

**验证器实际故障与处置**：并行覆盖率和串行无覆盖率尝试均曾Python退出 -11（SIGSEGV），无完整JUnit，不算通过。独立复现不导入CareerForge/pytest：相同SQLAlchemy ORM负载开启异步栈转储2/2崩溃，关闭2/2正常；纯Python/stdlib SQLite等12次对照正常。单业务用例计时转储也可复现。验证器因此将自动栈转储改为显式 `--traceback-after`，默认0；**保留独立硬超时、所有业务断言及全部用例**，新增5项工具合同回归后重新完整执行。不是修复或诊断了CPython精确C根因，不是归咎于覆盖率，也不是跳过失败用例。独立诊断合计34子进程，14个关闭计时器的控制全部通过、10次SIGSEGV均出现在定时转储中，见`diagnostic_fault/independent_probe_report.md`；这些诊断不加入产品测试计数。全部尝试及最小复现保存在`validation/v16/diagnostic_fault/`、`attempt_parallel_coverage_sigsegv/`、`attempt_serial_timer_sigsegv/`和`coverage_provenance.json`。

实际worker验证器原先使用旧版`source_ids=[]`探针，没有冻结分类字段，新Worker正确拒绝。**仅验收脚本**补上显式空taxonomy合同后，58项相关部署/验证器测试和真实worker重新通过；应用源码及666项测试文件均未改变。`code_freeze_before_probe_contract_fix.json`与`code_freeze.json`记录此最后delta；不是声称666项都在脚本改后重新跑了一遍。该探针只验证队列/worker交付与0模型调用，不冒充知识分析质量。失败首轮在`validation/v16/production_legacy_probe_attempt/`保留。

## 2. 本轮实质补齐

1. **分类治理**：有来源的提议/审批/CAS，版本化Topic/Facet、合并/拆分、有限结构类型、历史映射；冻结定义进入Analysis/Review及检索，不授予模型审批权。
2. **学习闭环**：有费用/外发授权的个性化Plan提议与精确采纳；活动/Rubric/独立尝试/帮助条件/提交观察/调整及复习建议。自报完成不写成掌握。
3. **自进化**：安全TeachingSkill模板，Teaching/Research/Router/Retrieval实际双臂服务回放、冻结语料、独立Gold认证/校准、硬门禁、CAS/Canary/回滚；未知查询或证据不足保持inconclusive。
4. **维护和公共共享任务**：固定来源Diff与A/B/C更新路线；默认禁用、明确启用的本地快照调度；固定公共源/系统预算的任务singleflight、订阅和取消结算。
5. **检索/渠道**：任务查询改写、批量embedding索引/重建、PR/Release、ETag、DB跨Worker限流；真实PG向量/权限及Redis发布栅栏。
6. **流式/大图/删除**：逐块生成→Guard→提交→下一块，恢复不重复付费、游标后撤销；>10k依赖图的流读与持久分页；个人导出/确认擦除、generation栅栏、跨人精确派生副本清理。
7. **兼容与工程修复**：原仓库固定基线只读核对，19表owner白名单导出/未审核归档；来源资金范围、模型完整实际正文依赖、隐私receipt见证、并发锁序、schema readiness与`cf160_features`迁移。

最后P1补丁明确包括可选Topic/Facet正文：不能仅以Publication阻断边代表真正外发内容；Tutor/Checker/Plan/Coding/Embedding绑定完整pack引用，外发前重验私源类别，已完成/在途模型正文都能被精确擦除或丢弃。

## 3. 保留差异，不归因于“只差Docker”

- **高兼容限制 G01**：未恢复原Vue/Tauri、全部旧REST/旧schema。已做有限只读归档桥，不可直接覆盖原工程。
- **中等范围 G02/G06**：不同定义只报unmapped；无通用单位转换。A仅机械Python结构事实，B仍新Analysis+Review；调度只扫描已导入快照，无远端自动轮询或通用跨语言语义继承。比较未逐项展示被排除旧Facet schema元数据属于低严重性展示差异。
- **G11**：server_snapshot/Python执行合同；无本机IDE同步、TS/Go执行镜像；Docker实际隔离未验。
- **高产品验收阻塞 G15**：真实模型、独立Gold、真实教学/检索收益及生产负载/灾备未建立；不能用合成评测分数代替。
- 删除治理为运营策略，不承诺召回第三方、下载或备份副本；企业IAM、通用法律判断、跨平台供应链认证不在已完成声明。

逐项严重性、是否未完成代码以及规格引用见 **`docs/KNOWN_GAPS.md`**、`docs/REQUIREMENTS_MATRIX.md`；分支报告仅辅助，末版结果以上述统一索引为准。

## 4. Docker实际阻塞

工作空间内官方Docker29.9.2/Compose5.6.1已运行；rootlesskit3.3.0实际启动退出1，`newuidmap`缺失；`NoNewPrivs=1`、能力边界0、无daemon/socket和cgroup委派。单UID unshare成功不等于Docker条件满足。未绕过安全隔离或使用用户电脑。

最后验证器退出2：Compose解析passed；daemon blocked；镜像、容器内测试与沙箱隔离not_run。有合格且授权的环境后执行 `python scripts/verify_docker.py --confirm-docker-test`，不把本次原生验证更名为容器验证。

## 5. 交付与重现

完整源码、666项测试、迁移、Dockerfile/Compose、测试脚本、1.6.0 wheel、依赖约束/锁、原规格及真实日志均在包内。原始输入和1.5.1检查点保留。未推送/部署/触发远程CI，未用用户电脑或付费API。

不包含运行数据库、venv、node_modules、真实环境/客户端凭据、系统二进制或旧wheel。SHA256清单不是签名。安装和验证见 `README.md`、`docs/VALIDATION.md`；现有库先备份、隔离演练，不能盲目stamp或继承旧审核资格。
