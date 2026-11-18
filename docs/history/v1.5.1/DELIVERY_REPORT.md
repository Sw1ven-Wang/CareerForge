# CareerForge 1.5.1 交付报告

**日期：2026-11-02｜范围：仅 dot 工作空间｜结论：安全/可靠性完善版已交付；不宣称全部产品或 Docker 验收完成。**

本轮实质代码修改与独立复审均使用配置为 **Astra 6 / max** 的代理。应用测试未使用真实付费模型；没有使用用户电脑、发布、推送或部署。

## 1. 验证结论

| 检查 | 最终结果 | 证据 |
|---|---|---|
| 全部收集用例 | **383 项全部对账：376 离线 + 7 真实基础设施，383 passed，0 failed/error/skipped** | `validation/current/summary.json`、`final_offline.xml`、`final_native_authorized.xml` |
| 实际 PostgreSQL/pgvector | **通过**：并发幂等、预算结算、锁序、迟到收据重放、FTS/向量SQL与权限/撤销 | `final_native_authorized_console.log`、native harness 对应日志 |
| 实际 Redis7/8 | **通过**：跨客户端 singleflight、过期持锁者写入 fencing、知识权限失效；最终组合使用Redis7 | `native-infrastructure/`、最终native JUnit |
| 实际 Celery/Outbox | **通过**：独立队列→真实Worker→PG；新库迁移、向量SQL、模型收据为0 | `final_production/production.json` |
| 实际 Pi SDK与解析器 | **通过**：Pi1.3.0实际会话/工具循环（合成推理）、JS/TS/TSX/Go实际语法库 | `tests/test_pi_runtime.py`、`tests/test_sources_real_parsers.py`及最终JUnit |
| Python/Node依赖 | 新建venv完整解析安装、clean npm ci、pip check **通过** | `dependency_install.log`、coding/npm验证日志、`pip_dependencies.log` |
| Ruff lint / format、Python编译、Node语法 | **全部通过** | `static_checks.json`及对应日志 |
| Wheel构建与隔离目录安装 | **通过**：CLI help、新SQLite库60表、30个Topic、0个Publication | `wheel_build.log`、`wheel_install.json` |
| mypy项目检查（默认模式） | **通过：41个源文件，0诊断**；无全局缺失导入忽略 | `mypy_full.log`、`static_checks.json` |
| Docker Compose静态配置 | 官方Compose实际解析 **通过** | `docker-attempt/compose-final.json`、`final_docker/` |
| Docker镜像/容器/沙箱运行 | **BLOCKED / NOT_RUN**，不是通过 | `final_docker/docker.json`、`docker-attempt/rootless-prerequisites.log` |
| 真实模型质量 / 人工Gold / 教学收益 / 生产负载与灾备 | **未验收** | `docs/KNOWN_GAPS.md` |

离线语句覆盖率 **5,454 / 6,516 = 83.70%**；不是分支覆盖、需求覆盖或模型准确率。376项离线测试显式排除7项production用例，后者在真实原生基础设施单独执行。程序已核对383个唯一节点，无遗漏和重复计数。原始83项用例保留并纳入整合。

## 2. 修复重点

1. **费用与恢复**：显式空授权不扩大；过期租约/撤销/旧worker不能提交成功或打断替代worker；同键并发只有一次资金预留；根/子任务锁序一致；失败/取消子任务产物不可发布。
2. **聊天闭环**：任务终止与Turn中断原子提交，避免SSE无限心跳和会话永久busy；终止事件幂等。CLI仅用GET恢复完整事件，不重发问题或重复付费；断流不冒充完成。
3. **可信知识**：独立审核证据建立阻断依赖；来源、读取、事实、审核与发布投影核对冻结身份；缓存、比较和历史SSE重验；固定版本与指定来源取交集；私有消费者不通过更新预览泄露。
4. **执行审批**：文件、版本、argv、镜像绑定完整批准摘要；执行前重验；旧回执不验证新版本；失效补丁拒绝应用。Pi模型上下文归Python服务控制，工具/消息/预算/结束状态严格校验。
5. **进化发布**：权限、模型端点、Gold与同意状态变化阻断晋升/灰度；迟到评测不能恢复撤回的候选。
6. **工程边界**：有界内存缓存与锁清理；Redis原子写入；多语言解析补全；终端控制字符过滤；凭证原子写入；未迁移数据库readiness返回503。
7. **部署与验证**：独立生产/测试Compose栈、迁移依赖顺序、密码安全编码、沙箱回执/输出/资源检查与自身资源清理；依赖版本约束、npm lock与CI流程。

独立复审发现并关闭四项集成问题：终止聊天Turn滞留、低权限新文件导出能力不足、审核阶段决议变量被覆盖、必需Facet投影缺失后仍可导出。相应回归已纳入最终用例。另以精确类型和缺失记录拒绝路径修复原135条mypy诊断；不是靠全局ignore压制。

## 3. Docker为什么没有标通过

已下载并实际使用官方 Docker29.9.2、Compose5.6.1、rootlesskit3.3.0，仅放在当前工作空间。Rootless启动实际退出1，原因是`newuidmap`缺失；环境同时为`NoNewPrivs=1`、能力边界0、无可用daemon/socket与cgroup委派。没有禁用隔离、修改系统安全配置或转移到用户电脑。

最终一键Docker脚本退出2：配置解析passed；daemon blocked；后端/沙箱镜像构建、容器内测试和隔离检查全部not_run。未来具备Docker条件后可运行：

```bash
python scripts/verify_docker.py --confirm-docker-test
```

当前真实PG/Redis/Celery来自官方签名和SHA256核验的用户态包，在同一dot工作空间的临时回环进程中执行，**不等于容器验证**。所有最终原生服务正常停止，自己创建的数据目录已清理。

## 4. 升级与交付内容

- 提供完整源码、383项测试、Dockerfile/Compose/验证脚本、Python约束与npm锁、1.5.1 wheel、原始规格、历史与本轮证据。
- 已由Alembic管理的本包v1.5库：备份后`alembic upgrade head`。新增批准摘要保持旧记录NULL，旧命令必须重新提议/批准。
- 未标记的旧开发库与原Vue/Tauri旧库不自动迁移，不能盲目stamp。历史知识不会自动补造新依赖；正式使用优先新库/重新构建，已有库须单独迁移审计。
- 交付包不包含venv、node_modules、下载的系统工具、运行数据库、`.env`或`client.json`。`MANIFEST.sha256`可检测文件改变，但不是数字签名。

详细重现命令见`README.md`、`docs/VALIDATION.md`、`docs/DOCKER_VALIDATION.md`。未解决的完整规格差异仍明确保留在`docs/KNOWN_GAPS.md`；不能把383个工程测试通过解释为全部产品完成。
